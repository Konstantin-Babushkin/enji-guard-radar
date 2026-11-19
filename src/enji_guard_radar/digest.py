"""The weekly digest (DIG-01 to DIG-04): everything open, worst first, capped, as Telegram text.

Pure: the caller gathers the inputs from a successful poll and sends the result. Only counts,
scores, dates, audit types and names appear; the input types hold no finding titles, ids, paths
or evidence, so none can be rendered.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from enji_guard_radar.ledger import ResolvedItem, SeriesKey
from enji_guard_radar.messages import (
    DEFAULT_MAX_CHARS,
    MIN_MAX_CHARS,
    TELEGRAM_MAX_CHARS,
    clean_name,
    days_text,
    escape,
    high_findings_text,
    high_where,
    pack_items,
    telegram_length,
)
from enji_guard_radar.model import Alert, AlertKind, Audience, OutboundMessage
from enji_guard_radar.score_rules import LowScore
from enji_guard_radar.texts import DEFAULT_LANGUAGE, Language, counted, noun, text
from enji_guard_radar.tooling_rules import ToolingGap

logger = logging.getLogger(__name__)

DIGEST_TITLE = text("en", "digest.title")  # the English title; the catalog has every language
RESOLVED_LINE_CAP = 5


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
    last_poll_at: AwareDatetime
    # The language its lines were built in; `render_digest` writes the rest of the text in it.
    language: Language = DEFAULT_LANGUAGE


def _day(moment: datetime) -> str:
    return moment.astimezone(UTC).date().isoformat()


def _name(name: str, language: Language) -> str:
    return escape(clean_name(name, language=language))


def _high_item(
    alert: Alert, recurring: Mapping[SeriesKey, int], language: Language
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
    what = text(
        language,
        "alert.high.count",
        findings=high_findings_text(alert.count, language=language),
        where=high_where(alert),
    )
    seen = text(
        language,
        "alert.high.seen",
        since=_day(alert.seen_since),
        days=days_text(alert.age_days, language=language),
    )
    line = text(
        language,
        "digest.item.high",
        name=_name(alert.project_name, language),
        what=what,
        seen=seen,
    )
    series = (alert.key.project_id, alert.key.repo_id, alert.key.action_key)
    runs = recurring.get(series)
    if runs is not None:
        line += " " + text(language, "digest.item.recurring", runs=runs)
    order = (
        -alert.count,
        -alert.age_days,
        clean_name(alert.project_name).casefold(),
        alert.key.as_text(),
    )
    return order, line


def _stale_item(alert: Alert, language: Language) -> tuple[tuple[object, ...], str]:
    name = _name(alert.project_name, language)
    if alert.kind is AlertKind.STALE:
        if alert.age_days is None or alert.last_run_at is None:
            message = "a stale digest alert needs age_days and last_run_at"
            raise ValueError(message)
        line = text(
            language,
            "digest.item.stale",
            name=name,
            days=days_text(alert.age_days, language=language),
            last=_day(alert.last_run_at),
        )
        age, never = alert.age_days, 0
    elif alert.kind is AlertKind.NEVER_RAN:
        line = text(language, "digest.item.stale_never", name=name)
        age, never = 0, 1
    else:
        message = "digest stale alerts must be STALE or NEVER_RAN"
        raise ValueError(message)
    order = (never, -age, clean_name(alert.project_name).casefold(), alert.key.as_text())
    return order, line


def _low_score_item(item: LowScore, language: Language) -> tuple[tuple[object, ...], str]:
    line = text(
        language,
        "digest.item.low_score",
        name=_name(item.project_name, language),
        audit=escape(item.action_key),
        score=item.score,
        repo=escape(clean_name(item.repo_name, language=language)),
        day=_day(item.completed_at),
    )
    order = (
        item.completed_at.astimezone(UTC),
        item.score,
        clean_name(item.project_name).casefold(),
        item.repo_id,
        item.action_key,
    )
    return order, line


def _gap_item(item: ToolingGap, language: Language) -> tuple[tuple[object, ...], str]:
    if item.last_completed_at is None:
        tail = text(language, "digest.gap.none")
    else:
        tail = text(language, "digest.gap.last", day=_day(item.last_completed_at))
    line = text(
        language,
        "digest.item.gap",
        name=_name(item.project_name, language),
        audit=escape(item.action_key),
        days=days_text(item.window_days, language=language),
        repo=escape(clean_name(item.repo_name, language=language)),
        tail=tail,
    )
    moment = datetime.min.replace(tzinfo=UTC)
    if item.last_completed_at is not None:
        moment = item.last_completed_at.astimezone(UTC)
    order = (
        0 if item.last_completed_at is None else 1,
        moment,
        clean_name(item.project_name).casefold(),
        clean_name(item.repo_name).casefold(),
        item.action_key,
    )
    return order, line


def _sorted_lines(rows: Sequence[tuple[tuple[object, ...], str]]) -> list[str]:
    # Orders hold only ints, strings and datetimes in matching positions, so they always compare.
    return [line for _, line in sorted(rows, key=lambda row: row[0])]


def _resolved_line(item: ResolvedItem, names: Mapping[str, str], language: Language) -> str:
    name = _name(names.get(item.key.project_id, ""), language)
    level = item.key.level
    if level == "high":
        return text(language, "digest.resolved.high", name=name, audit=escape(item.key.action_key))
    if level == "stale":
        return text(language, "digest.resolved.stale", name=name)
    if level == "never_ran":
        return text(language, "digest.resolved.never_ran", name=name)
    return text(language, "digest.resolved.other", name=name)


def build_digest(inp: DigestInput, *, language: Language = DEFAULT_LANGUAGE) -> Digest:
    """Order the open items by tier, keep one line per key, cap at `max_items`."""
    high = {a.key: _high_item(a, inp.recurring_runs, language) for a in inp.high}
    stale = {a.key: _stale_item(a, language) for a in inp.stale}
    low = {
        (s.project_id, s.repo_id, s.action_key): _low_score_item(s, language)
        for s in inp.low_scores
    }
    gaps = {
        (g.project_id, g.repo_id, g.action_key): _gap_item(g, language) for g in inp.tooling_gaps
    }
    by_tier = {
        DigestTier.HIGH: _sorted_lines(list(high.values())),
        DigestTier.STALE: _sorted_lines(list(stale.values())),
        DigestTier.LOW_SCORE: _sorted_lines(list(low.values())),
        DigestTier.TOOLING_GAP: _sorted_lines(list(gaps.values())),
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
    return Digest(
        items=tuple(shown),
        hidden_items=len(everything) - len(shown),
        total_items=len(everything),
        resolved_lines=lines,
        resolved_hidden=len(resolved) - len(lines),
        resolved_total=len(resolved),
        unread_projects=inp.unread_projects,
        last_poll_at=inp.last_poll_at,
        language=language,
    )


def _poll_text(digest: Digest, language: Language) -> str:
    stamp = digest.last_poll_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    return text(language, "digest.poll", stamp=stamp)


def _unread_line(count: int, language: Language) -> str:
    return text(language, "digest.unread", projects=counted(language, "noun.project", count))


def _more_line(hidden: int, language: Language) -> str:
    return text(
        language, "digest.more_items", hidden=hidden, noun=noun(language, "noun.item", hidden)
    )


def _body(digest: Digest, language: Language) -> list[str]:
    lines: list[str] = []
    if digest.total_items:
        lines.append(text(language, "digest.open_items", total=digest.total_items))
    if digest.unread_projects:
        lines.append(_unread_line(digest.unread_projects, language))
    lines.extend(item.line for item in digest.items)
    if digest.hidden_items:
        lines.append(_more_line(digest.hidden_items, language))
    if digest.resolved_total:
        lines.append(text(language, "digest.resolved_head", total=digest.resolved_total))
        lines.extend(digest.resolved_lines)
        if digest.resolved_hidden:
            lines.append(text(language, "digest.resolved_more", hidden=digest.resolved_hidden))
    return lines


def _header(digest: Digest, index: int, count: int, language: Language) -> str:
    suffix = f" ({index}/{count})" if count > 1 else ""
    title = f"<b>{text(language, 'digest.title')}{suffix}</b>"
    if digest.total_items:
        return f"{title}\n{_poll_text(digest, language)}"
    key = "digest.nothing_open_read" if digest.unread_projects else "digest.nothing_open"
    return f"{title}: {text(language, key)}. {_poll_text(digest, language)}"


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
        "digest built: %d open, %d shown, %d resolved, %d message(s)",
        digest.total_items,
        len(digest.items),
        digest.resolved_total,
        len(messages),
    )
    return messages
