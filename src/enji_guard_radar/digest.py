"""The weekly digest (DIG-01 to DIG-04): everything open, worst first, capped, as Telegram text.

Pure: the caller gathers the inputs from a successful poll and sends the result. Only counts,
scores, dates, audit types and names appear; the input types hold no finding titles, ids, paths
or evidence, so none can be rendered.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from enji_guard_radar.high_rules import LowSeries
from enji_guard_radar.ledger import ResolvedItem, SeriesKey
from enji_guard_radar.messages import (
    DEFAULT_MAX_CHARS,
    MIN_MAX_CHARS,
    TELEGRAM_MAX_CHARS,
    age_text,
    ambiguous_series,
    audit_short,
    clean_name,
    escape,
    high_line,
    pack_items,
    render_alert,
    telegram_length,
)
from enji_guard_radar.model import Alert, AlertKind, Audience, OutboundMessage
from enji_guard_radar.score_rules import LowScore
from enji_guard_radar.texts import DEFAULT_LANGUAGE, Language, counted, noun, text
from enji_guard_radar.tooling_rules import ToolingGap

logger = logging.getLogger(__name__)

RESOLVED_LINE_CAP = 5
LOW_LINE_CAP = 5


class DigestTier(StrEnum):
    HIGH = "high"
    STALE = "stale"
    LOW_SCORE = "low_score"
    TOOLING_GAP = "tooling_gap"


# Worst first. A recurring series is an open High series, so it lives in the High tier (D-09).
TIER_ORDER = (DigestTier.HIGH, DigestTier.STALE, DigestTier.LOW_SCORE, DigestTier.TOOLING_GAP)


class DigestInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    last_poll_at: AwareDatetime
    max_items: StrictInt = Field(ge=1)
    high: tuple[Alert, ...] = ()
    # Open High series of low-priority audit types: their own short block after the High items.
    low_priority: tuple[LowSeries, ...] = ()
    recurring_runs: Mapping[SeriesKey, int] = Field(default_factory=dict)
    stale: tuple[Alert, ...] = ()
    low_scores: tuple[LowScore, ...] = ()
    tooling_gaps: tuple[ToolingGap, ...] = ()
    resolved: tuple[ResolvedItem, ...] = ()
    project_names: Mapping[str, str] = Field(default_factory=dict)
    unread_projects: StrictInt = Field(default=0, ge=0)


class DigestItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: DigestTier
    line: str


class Digest(BaseModel):
    """The ordered, capped items with their escaped lines, and the counts the text needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[DigestItem, ...]
    hidden_items: int
    total_items: int
    resolved_lines: tuple[str, ...]
    resolved_hidden: int
    resolved_total: int
    unread_projects: int
    # The low-priority block: its lines, how many did not fit and how many there are in all.
    # It is outside `items` and the `max_items` cap, but its count is part of the title number.
    low_lines: tuple[str, ...]
    low_hidden: int
    low_total: int
    # A High or low-priority line is shown: the digest then ends with the footer about how ages
    # are counted.
    has_high: bool
    last_poll_at: AwareDatetime
    # The language its lines were built in; `render_digest` writes the rest of the text in it.
    language: Language = DEFAULT_LANGUAGE


def _day(moment: datetime) -> str:
    return moment.astimezone(UTC).date().isoformat()


def _name(name: str, language: Language) -> str:
    return escape(clean_name(name, language=language))


def _line(language: Language, name: str, *parts: str) -> str:
    """`<marker> <b>name</b> · part · part`: every non-High item line has this shape."""
    marker = text(language, "marker.other")
    return " · ".join([f"{marker} <b>{_name(name, language)}</b>", *parts])


def _high_item(
    alert: Alert,
    recurring: Mapping[SeriesKey, int],
    ambiguous: frozenset[tuple[str, str]],
    language: Language,
) -> tuple[tuple[object, ...], str]:
    if (
        alert.kind is not AlertKind.HIGH_FINDINGS
        or not alert.count
        or alert.repo_name is None
        or alert.seen_since is None
        or alert.age_days is None
    ):
        message = "digest high alerts need count, repo_name, seen_since and age_days"
        raise ValueError(message)
    show_repo = (alert.key.project_id, alert.key.action_key) in ambiguous
    line = high_line(alert, language=language, show_repo=show_repo, arrow=False)
    series = (alert.key.project_id, alert.key.repo_id, alert.key.action_key)
    runs = recurring.get(series)
    if runs is not None:
        line += " · " + text(language, "digest.short_recurring", runs=runs)
    order = (
        -alert.count,
        -alert.age_days,
        clean_name(alert.project_name).casefold(),
        alert.key.as_text(),
    )
    return order, line


def _ambiguous_low(series: Sequence[LowSeries]) -> frozenset[tuple[str, str]]:
    """(project_id, action_key) pairs that more than one repo holds among the low series."""
    repos: dict[tuple[str, str], set[str]] = {}
    for item in series:
        repos.setdefault((item.project_id, item.action_key), set()).add(item.repo_id)
    return frozenset(pair for pair, ids in repos.items() if len(ids) > 1)


def _low_priority_item(
    item: LowSeries, poll_at: datetime, ambiguous: frozenset[tuple[str, str]], language: Language
) -> tuple[tuple[object, ...], str]:
    audit = audit_short(item.action_key)
    if (item.project_id, item.action_key) in ambiguous:
        repo = escape(clean_name(item.repo_name, language=language))
        audit = text(language, "alert.where_repo", audit=audit, repo=repo)
    age = max((poll_at - item.opened_at).days, 0)
    line = _line(
        language,
        item.project_name,
        text(language, "digest.low_item", audit=audit, count=item.high_now),
        age_text(age, language),
    )
    order = (
        -item.high_now,
        -age,
        clean_name(item.project_name).casefold(),
        item.repo_id,
        item.action_key,
    )
    return order, line


def _stale_item(alert: Alert, language: Language) -> tuple[tuple[object, ...], str]:
    if alert.kind is AlertKind.STALE:
        if alert.age_days is None or alert.last_run_at is None:
            message = "a stale digest alert needs age_days and last_run_at"
            raise ValueError(message)
        age, never = alert.age_days, 0
    elif alert.kind is AlertKind.NEVER_RAN:
        age, never = 0, 1
    else:
        message = "digest stale alerts must be STALE or NEVER_RAN"
        raise ValueError(message)
    order = (never, -age, clean_name(alert.project_name).casefold(), alert.key.as_text())
    return order, render_alert(alert, language=language)


def _ambiguous_scores(scores: Sequence[LowScore]) -> frozenset[tuple[str, str]]:
    """(project_id, action_key) pairs that more than one repo holds among the low scores."""
    repos: dict[tuple[str, str], set[str]] = {}
    for item in scores:
        repos.setdefault((item.project_id, item.action_key), set()).add(item.repo_id)
    return frozenset(pair for pair, ids in repos.items() if len(ids) > 1)


def _low_score_item(
    item: LowScore, poll_day: date, ambiguous: frozenset[tuple[str, str]], language: Language
) -> tuple[tuple[object, ...], str]:
    audit = audit_short(item.action_key)
    if (item.project_id, item.action_key) in ambiguous:
        repo = escape(clean_name(item.repo_name, language=language))
        audit = text(language, "alert.where_repo", audit=audit, repo=repo)
    age = max((poll_day - item.completed_at.astimezone(UTC).date()).days, 0)
    line = _line(
        language,
        item.project_name,
        text(language, "digest.short_score", audit=audit, score=item.score),
        age_text(age, language),
    )
    order = (
        item.completed_at.astimezone(UTC),
        item.score,
        clean_name(item.project_name).casefold(),
        item.repo_id,
        item.action_key,
    )
    return order, line


def _gap_order(item: ToolingGap) -> tuple[object, ...]:
    moment = datetime.min.replace(tzinfo=UTC)
    if item.last_completed_at is not None:
        moment = item.last_completed_at.astimezone(UTC)
    return (
        0 if item.last_completed_at is None else 1,
        moment,
        clean_name(item.project_name).casefold(),
        clean_name(item.repo_name).casefold(),
        item.action_key,
    )


def _gap_items(
    gaps: Sequence[ToolingGap], language: Language
) -> list[tuple[tuple[object, ...], str]]:
    """One item per (project, repo): its audit names sorted, ordered by its oldest member."""
    unique = {(g.project_id, g.repo_id, g.action_key): g for g in gaps}
    groups: dict[tuple[str, str], list[ToolingGap]] = {}
    for gap in unique.values():
        groups.setdefault((gap.project_id, gap.repo_id), []).append(gap)
    items = []
    for members in groups.values():
        first = members[0]
        audits = ", ".join(
            escape(n) for n in sorted(g.action_key.removeprefix("audit.") for g in members)
        )
        window = min(g.window_days for g in members)
        line = _line(
            language,
            first.project_name,
            escape(clean_name(first.repo_name, language=language)),
            text(language, "digest.short_gap", age=age_text(window, language), audits=audits),
        )
        items.append((min(_gap_order(g) for g in members), line))
    logger.info("tooling gaps: %d gap(s) in %d repo line(s)", len(unique), len(items))
    return items


def _sorted_lines(rows: Sequence[tuple[tuple[object, ...], str]]) -> list[str]:
    # Orders hold only ints, strings and datetimes in matching positions, so they always compare.
    return [line for _, line in sorted(rows, key=lambda row: row[0])]


def _resolved_line(item: ResolvedItem, names: Mapping[str, str], language: Language) -> str:
    name = _name(names.get(item.key.project_id, ""), language)
    level = item.key.level
    if level == "high":
        what = audit_short(item.key.action_key)
    elif level in ("stale", "never_ran"):
        what = text(language, f"digest.short_what.{level}")
    else:
        what = text(language, "digest.short_what.other")
    return text(language, "digest.short_resolved", name=name, what=what)


def build_digest(inp: DigestInput, *, language: Language = DEFAULT_LANGUAGE) -> Digest:
    """Order the open items by tier, keep one line per key, cap at `max_items`."""
    ambiguous_high = ambiguous_series(inp.high)
    high = {a.key: _high_item(a, inp.recurring_runs, ambiguous_high, language) for a in inp.high}
    stale = {a.key: _stale_item(a, language) for a in inp.stale}
    poll_day = inp.last_poll_at.astimezone(UTC).date()
    ambiguous_low = _ambiguous_scores(inp.low_scores)
    low = {
        (s.project_id, s.repo_id, s.action_key): _low_score_item(
            s, poll_day, ambiguous_low, language
        )
        for s in inp.low_scores
    }
    by_tier = {
        DigestTier.HIGH: _sorted_lines(list(high.values())),
        DigestTier.STALE: _sorted_lines(list(stale.values())),
        DigestTier.LOW_SCORE: _sorted_lines(list(low.values())),
        DigestTier.TOOLING_GAP: _sorted_lines(_gap_items(inp.tooling_gaps, language)),
    }
    everything = [DigestItem(tier=t, line=line) for t in TIER_ORDER for line in by_tier[t]]
    shown = everything[: inp.max_items]
    # Same-key duplicates in the input collapse above; a duplicate resolved key does too.
    resolved = sorted(
        {r.key: r for r in inp.resolved}.values(),
        key=lambda r: (r.resolved_at, r.key.as_text()),
    )
    lines = tuple(
        _resolved_line(r, inp.project_names, language) for r in resolved[:RESOLVED_LINE_CAP]
    )
    open_low = {
        (s.project_id, s.repo_id, s.action_key): s for s in inp.low_priority if s.high_now > 0
    }
    ambiguous_low_series = _ambiguous_low(list(open_low.values()))
    low_lines = _sorted_lines(
        [
            _low_priority_item(s, inp.last_poll_at, ambiguous_low_series, language)
            for s in open_low.values()
        ]
    )
    logger.info("low-priority digest block: %d open series", len(low_lines))
    return Digest(
        items=tuple(shown),
        hidden_items=len(everything) - len(shown),
        total_items=len(everything),
        resolved_lines=lines,
        resolved_hidden=len(resolved) - len(lines),
        resolved_total=len(resolved),
        unread_projects=inp.unread_projects,
        low_lines=tuple(low_lines[:LOW_LINE_CAP]),
        low_hidden=max(len(low_lines) - LOW_LINE_CAP, 0),
        low_total=len(low_lines),
        has_high=any(item.tier is DigestTier.HIGH for item in shown) or bool(low_lines),
        last_poll_at=inp.last_poll_at,
        language=language,
    )


def _stamp(digest: Digest) -> str:
    return digest.last_poll_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def _unread_line(count: int, language: Language) -> str:
    return text(language, "digest.unread", projects=counted(language, "noun.project", count))


def _more_line(hidden: int, language: Language) -> str:
    return text(
        language, "digest.more_items", hidden=hidden, noun=noun(language, "noun.item", hidden)
    )


def _low_block(digest: Digest, language: Language) -> list[str]:
    if not digest.low_total:
        return []
    block = [text(language, "digest.low_head", count=digest.low_total), *digest.low_lines]
    if digest.low_hidden:
        block.append(text(language, "digest.low_more", hidden=digest.low_hidden))
    return block


def _body(digest: Digest, language: Language) -> list[str]:
    lines: list[str] = []
    if digest.unread_projects:
        lines.append(_unread_line(digest.unread_projects, language))
    # Items are ordered by tier, so the High ones come first; the low-priority block follows them.
    lines.extend(item.line for item in digest.items if item.tier is DigestTier.HIGH)
    lines.extend(_low_block(digest, language))
    lines.extend(item.line for item in digest.items if item.tier is not DigestTier.HIGH)
    if digest.hidden_items:
        lines.append(_more_line(digest.hidden_items, language))
    if digest.resolved_total:
        lines.append(text(language, "digest.short_resolved_head", total=digest.resolved_total))
        lines.extend(digest.resolved_lines)
        if digest.resolved_hidden:
            lines.append(text(language, "digest.resolved_more", hidden=digest.resolved_hidden))
    if digest.has_high:
        lines.append(text(language, "digest.footer_age"))
    return lines


def _header(digest: Digest, index: int, count: int, language: Language) -> str:
    if not digest.total_items and not digest.low_total:
        # One line, never numbered: with at most 7 resolved lines it always fits one message.
        key = "digest.nothing_open_read" if digest.unread_projects else "digest.nothing_open"
        return text(language, "digest.short_clear", what=text(language, key), stamp=_stamp(digest))
    suffix = f" ({index}/{count})" if count > 1 else ""
    count_open = digest.total_items + digest.low_total
    title = f"<b>{text(language, 'digest.short_title', count=count_open)}{suffix}</b>"
    return f"{title}\n{text(language, 'digest.short_poll', stamp=_stamp(digest))}"


def render_digest(
    digest: Digest, *, max_chars: int = DEFAULT_MAX_CHARS, language: Language | None = None
) -> list[OutboundMessage]:
    """The digest as group messages: one when it fits, else split between lines into (k/n).

    The text is in the language the digest was built in unless `language` says otherwise.
    """
    language = digest.language if language is None else language
    if not MIN_MAX_CHARS <= max_chars <= TELEGRAM_MAX_CHARS:
        message = f"max_chars must be between {MIN_MAX_CHARS} and {TELEGRAM_MAX_CHARS}"
        raise ValueError(message)
    body = _body(digest, language)
    head_units = telegram_length(_header(digest, 1, 1, language))
    chunks = pack_items(body, head_units, max_chars) if body else [[]]
    if len(chunks) > 1:
        # Reserve room for the " (k/n)" suffix; more chunks widen it, so repack until stable.
        count = len(chunks)
        while True:
            reserve = telegram_length(f" ({count}/{count})")
            chunks = pack_items(body, head_units + reserve, max_chars)
            if len(chunks) <= count:
                break
            count = len(chunks)
    messages = [
        OutboundMessage(
            text="\n".join(
                [_header(digest, number, len(chunks), language), *(body[i] for i in indexes)]
            ),
            keys=(),
            audience=Audience.GROUP,
        )
        for number, indexes in enumerate(chunks, start=1)
    ]
    logger.info(
        "digest built: %d open, %d shown, %d low priority, %d resolved, %d message(s)",
        digest.total_items,
        len(digest.items),
        digest.low_total,
        digest.resolved_total,
        len(messages),
    )
    return messages
