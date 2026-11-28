"""The weekly digest (DIG-01 to DIG-04): one line per project, worst first, as Telegram text.

Pure: the caller gathers the inputs from a successful poll and sends the result. Only counts,
dates, audit types and names appear; the input types hold no finding titles, ids, paths or
evidence, so none can be rendered.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from enji_guard_radar.high_rules import HIGH_LEVEL, LowSeries
from enji_guard_radar.ledger import ResolvedItem
from enji_guard_radar.messages import (
    DEFAULT_MAX_CHARS,
    MIN_MAX_CHARS,
    TELEGRAM_MAX_CHARS,
    age_text,
    clean_name,
    days_text,
    escape,
    pack_items,
    telegram_length,
)
from enji_guard_radar.model import Alert, AlertKind, Audience, OutboundMessage
from enji_guard_radar.texts import DEFAULT_LANGUAGE, Language, audit_name, counted, noun, text

logger = logging.getLogger(__name__)

AUDITS_PER_LINE = 2


class DigestInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    last_poll_at: AwareDatetime
    # How many clean projects get their own line; the rest become one "and N more" line.
    max_items: StrictInt = Field(ge=1)
    # Open High alerts of high-priority audit types.
    high: tuple[Alert, ...] = ()
    # Open High series of low-priority audit types.
    low_priority: tuple[LowSeries, ...] = ()
    stale: tuple[Alert, ...] = ()
    # Only their number reaches the text.
    resolved: tuple[ResolvedItem, ...] = ()
    project_names: Mapping[str, str] = Field(default_factory=dict)
    unread_projects: StrictInt = Field(default=0, ge=0)
    # Ids of the clean projects (evaluated, not stale, no open High of a high-priority type);
    # their names come from `project_names`.
    clean_projects: tuple[str, ...] = ()


class Digest(BaseModel):
    """The ordered project lines (escaped) and the counts the text needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    open_high: int
    projects_with_high: int
    resolved_high: int
    # Projects with something open, worst first; empty when nothing is open.
    project_lines: tuple[str, ...]
    clean_lines: tuple[str, ...]
    clean_hidden: int
    unread_projects: int
    last_poll_at: AwareDatetime
    # The language its lines were built in; `render_digest` writes the rest of the text in it.
    language: Language = DEFAULT_LANGUAGE


@dataclass
class _Project:
    """What one project has open: High per audit type (high / low priority) and its stale alert."""

    project_id: str
    name: str
    high: dict[str, int] = field(default_factory=dict)
    high_ages: list[int] = field(default_factory=list)
    low: dict[str, int] = field(default_factory=dict)
    low_ages: list[int] = field(default_factory=list)
    stale: Alert | None = None


def _add(counts: dict[str, int], action_key: str, count: int) -> None:
    counts[action_key] = counts.get(action_key, 0) + count


def _collect(inp: DigestInput) -> dict[str, _Project]:
    projects: dict[str, _Project] = {}

    def project(project_id: str, name: str) -> _Project:
        return projects.setdefault(project_id, _Project(project_id, name))

    for alert in {a.key: a for a in inp.high}.values():
        if alert.kind is not AlertKind.HIGH_FINDINGS or not alert.count or alert.age_days is None:
            message = "digest high alerts need count and age_days"
            raise ValueError(message)
        entry = project(alert.key.project_id, alert.project_name)
        _add(entry.high, alert.key.action_key, alert.count)
        entry.high_ages.append(alert.age_days)
    open_low = {
        (s.project_id, s.repo_id, s.action_key): s for s in inp.low_priority if s.high_now > 0
    }
    for series in open_low.values():
        entry = project(series.project_id, series.project_name)
        _add(entry.low, series.action_key, series.high_now)
        entry.low_ages.append(max((inp.last_poll_at - series.opened_at).days, 0))
    for alert in {a.key: a for a in inp.stale}.values():
        if alert.kind is AlertKind.STALE and (alert.age_days is None or alert.last_run_at is None):
            message = "a stale digest alert needs age_days and last_run_at"
            raise ValueError(message)
        if alert.kind not in (AlertKind.STALE, AlertKind.NEVER_RAN):
            message = "digest stale alerts must be STALE or NEVER_RAN"
            raise ValueError(message)
        project(alert.key.project_id, alert.project_name).stale = alert
    return projects


def _name(name: str, language: Language) -> str:
    return escape(clean_name(name, language=language))


def _top_audits(counts: Mapping[str, int], language: Language) -> str:
    ranked = sorted(counts, key=lambda action_key: (-counts[action_key], action_key))
    return ", ".join(escape(audit_name(language, key)) for key in ranked[:AUDITS_PER_LINE])


def _age(ages: list[int], language: Language) -> str:
    age = age_text(max(ages), language)
    return text(language, "digest.age.oldest", age=age) if len(ages) > 1 else age


def _stale_state(alert: Alert, language: Language) -> str:
    if alert.kind is AlertKind.NEVER_RAN:
        return text(language, "digest.project.never_ran")
    return text(
        language, "digest.project.stale", days=days_text(alert.age_days or 0, language=language)
    )


def _problems(count: int, language: Language) -> str:
    return counted(language, "noun.high_problem", count)


def _project_line(entry: _Project, language: Language) -> str:
    name = _name(entry.name, language)
    if entry.high:
        line = text(
            language,
            "digest.project.line",
            marker=text(language, "marker.high"),
            name=name,
            problems=_problems(sum(entry.high.values()), language),
            audits=_top_audits(entry.high, language),
            age=_age(entry.high_ages, language),
        )
        extras = []
        if entry.low:
            extras.append(text(language, "digest.project.low_extra", count=sum(entry.low.values())))
    elif entry.low:
        line = text(
            language,
            "digest.project.line_low",
            marker=text(language, "marker.other"),
            name=name,
            problems=_problems(sum(entry.low.values()), language),
            audits=_top_audits(entry.low, language),
            age=_age(entry.low_ages, language),
        )
        extras = []
    else:
        stale = entry.stale
        if stale is None:
            message = "a project line needs open High or a stale alert"
            raise ValueError(message)
        return text(
            language,
            "digest.project.line_state",
            marker=text(language, "marker.other"),
            name=name,
            state=_stale_state(stale, language),
        )
    if entry.stale is not None:
        extras.append(_stale_state(entry.stale, language))
    return "; ".join([line, *extras])


def _order(entry: _Project) -> tuple[object, ...]:
    """Red before low-priority-only before stale-only, then most High, oldest, name."""
    if entry.high:
        rank, count, ages = 0, sum(entry.high.values()), entry.high_ages
    elif entry.low:
        rank, count, ages = 1, sum(entry.low.values()), entry.low_ages
    else:
        rank, count, ages = 2, 0, [entry.stale.age_days or 0] if entry.stale else []
    oldest = max(ages, default=0)
    return (rank, -count, -oldest, clean_name(entry.name).casefold(), entry.project_id)


def _clean_line(name: str, language: Language) -> str:
    return text(
        language,
        "digest.project.line_state",
        marker=text(language, "marker.green"),
        name=_name(name, language),
        state=text(language, "digest.project.clean_state"),
    )


def build_digest(inp: DigestInput, *, language: Language = DEFAULT_LANGUAGE) -> Digest:
    """One line per project with something open, worst first; clean projects after them."""
    projects = _collect(inp)
    ordered = sorted(projects.values(), key=_order)
    names = inp.project_names
    clean_ids = sorted(
        set(inp.clean_projects) - projects.keys(),
        key=lambda pid: (clean_name(names.get(pid, "")).casefold(), pid),
    )
    shown = clean_ids[: inp.max_items]
    resolved_high = {r.key for r in inp.resolved if r.key.level == HIGH_LEVEL}
    open_high = sum(sum(p.high.values()) + sum(p.low.values()) for p in projects.values())
    with_high = sum(1 for p in projects.values() if p.high or p.low)
    logger.info(
        "digest: %d open High in %d project(s), %d line(s), %d clean, %d resolved",
        open_high,
        with_high,
        len(ordered),
        len(clean_ids),
        len(resolved_high),
    )
    return Digest(
        open_high=open_high,
        projects_with_high=with_high,
        resolved_high=len(resolved_high),
        project_lines=tuple(_project_line(p, language) for p in ordered),
        clean_lines=tuple(_clean_line(names.get(pid, ""), language) for pid in shown),
        clean_hidden=len(clean_ids) - len(shown),
        unread_projects=inp.unread_projects,
        last_poll_at=inp.last_poll_at,
        language=language,
    )


def _stamp(digest: Digest) -> str:
    return digest.last_poll_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def _unread_projects(digest: Digest, language: Language) -> str:
    return counted(language, "noun.project", digest.unread_projects)


def _footer(digest: Digest, language: Language) -> list[str]:
    """The unread warning and the poll time; a clear digest has the time in its one line."""
    has_open = bool(digest.project_lines)
    if digest.unread_projects:
        key = "digest.unread_poll" if has_open else "digest.unread"
        return [
            text(
                language,
                key,
                projects=_unread_projects(digest, language),
                stamp=_stamp(digest),
            )
        ]
    return [text(language, "digest.poll", stamp=_stamp(digest))] if has_open else []


def _body(digest: Digest, language: Language) -> list[str]:
    lines = [*digest.project_lines, *digest.clean_lines]
    if not digest.project_lines and digest.resolved_high:
        # The one-line all-clear has no overview, so the closed count gets its own line.
        lines.insert(0, _overview(digest, language))
    if digest.clean_hidden:
        lines.append(
            text(
                language,
                "digest.clean_more",
                count=digest.clean_hidden,
                noun=noun(language, "noun.project", digest.clean_hidden),
            )
        )
    return [*lines, *_footer(digest, language)]


def _overview(digest: Digest, language: Language) -> str:
    if not digest.open_high:
        return text(language, "digest.overview_none", resolved=digest.resolved_high)
    return text(
        language,
        "digest.overview",
        count=digest.open_high,
        projects=counted(language, "noun.project_in", digest.projects_with_high),
        resolved=digest.resolved_high,
    )


def _header(digest: Digest, index: int, count: int, language: Language) -> str:
    title = text(language, "digest.title")
    if not digest.project_lines:
        # One line, never numbered: the clean lines after it may still split.
        key = "digest.nothing_open_read" if digest.unread_projects else "digest.nothing_open"
        return text(
            language,
            "digest.short_clear",
            title=title,
            what=text(language, key),
            stamp=_stamp(digest),
        )
    suffix = f" ({index}/{count})" if count > 1 else ""
    return f"<b>{title}{suffix}</b>\n{_overview(digest, language)}"


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
        "digest built: %d project line(s), %d clean line(s), %d message(s)",
        len(digest.project_lines),
        len(digest.clean_lines),
        len(messages),
    )
    return messages
