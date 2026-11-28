"""Alert text for Telegram (HTML parse mode). One escaping point for dynamic text."""

import html
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from enji_guard_radar.model import SEVERITY_ORDER, Alert, AlertKey, AlertKind, OutboundMessage
from enji_guard_radar.texts import DEFAULT_LANGUAGE, Language, counted, text
from enji_guard_radar.triage import ItemTriage

logger = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4096
# Headroom under the Telegram limit; also the default size of one summary chunk.
DEFAULT_MAX_CHARS = 4000
# A cleaned name is at most 80 characters, each escaping to at most 5 UTF-16 units (400), plus
# about 150 of fixed text (Russian is the longer language): one alert or one summary item stays
# near 550 units, far below this.
MIN_MAX_CHARS = 1000
MAX_NAME_CHARS = 80
# An alert line names its worst audit only while the whole line stays at most this long.
ALERT_LINE_FIT_CHARS = 120
# The English texts other modules and tests name; the catalog holds them for every language.
UNNAMED_PROJECT = text("en", "name.unnamed")


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def clean_name(name: str, *, language: Language = DEFAULT_LANGUAGE) -> str:
    """Guard text becomes one short line. Not escaped: `escape` is the single escaping point.

    Only the placeholder for an empty name depends on `language`; sort keys call this without it,
    so the order of alerts never depends on the language.
    """
    spaced = "".join(
        " "
        if unicodedata.category(ch).startswith("C") or unicodedata.category(ch) in {"Zl", "Zp"}
        else ch
        for ch in name
    )
    collapsed = re.sub(r"\s+", " ", spaced).strip()
    if not collapsed:
        return text(language, "name.unnamed")
    if len(collapsed) > MAX_NAME_CHARS:
        return collapsed[: MAX_NAME_CHARS - 3] + "..."
    return collapsed


def telegram_length(text: str) -> int:
    """Telegram counts UTF-16 code units; the final HTML text is never shorter than that count."""
    return len(text.encode("utf-16-le")) // 2


def days_text(count: int, *, language: Language = DEFAULT_LANGUAGE) -> str:
    return counted(language, "noun.day", count)


def marker(kind: AlertKind, language: Language = DEFAULT_LANGUAGE) -> str:
    """The colour marker, chosen by code from the alert kind only."""
    key = "marker.high" if kind is AlertKind.HIGH_FINDINGS else "marker.other"
    return text(language, key)


def audit_short(action_key: str) -> str:
    """The audit type without its `audit.` prefix, escaped."""
    return escape(action_key.removeprefix("audit."))


def age_text(days: int, language: Language = DEFAULT_LANGUAGE) -> str:
    """`today` for 0, else the counted days: every number carries its word."""
    if days == 0:
        return text(language, "alert.age.today")
    return days_text(days, language=language)


def held_text(days: int, language: Language = DEFAULT_LANGUAGE) -> str:
    """How long a problem has been open: `today` for 0, else `open for 3 days`."""
    if days == 0:
        return age_text(0, language)
    return text(language, "alert.age.held", days=days_text(days, language=language))


def project_groups(alerts: Sequence[Alert]) -> list[list[Alert]]:
    """The High alerts grouped by project, in the order each project first appears."""
    groups: dict[str, list[Alert]] = {}
    for alert in alerts:
        groups.setdefault(alert.key.project_id, []).append(alert)
    return list(groups.values())


def _worst_audit(totals: Mapping[str, int]) -> str:
    return min(totals, key=lambda action_key: (-totals[action_key], action_key))


def _where(totals: Mapping[str, int], language: Language, *, name_worst: bool) -> str:
    if len(totals) == 1:
        return text(language, "alert.where.one", audit=audit_short(next(iter(totals))))
    audits = counted(language, "noun.audit_in", len(totals))
    if name_worst:
        audit = audit_short(_worst_audit(totals))
        return text(language, "alert.where.worst", audits=audits, audit=audit)
    return text(language, "alert.where.many", audits=audits)


def project_high_line(group: Sequence[Alert], language: Language = DEFAULT_LANGUAGE) -> str:
    """One line for all High alerts of one project: count (and rise), audits, how long it lasts.

    The worst audit is named only when the line still fits `ALERT_LINE_FIT_CHARS`.
    """
    first = group[0]
    ages: list[int] = []
    totals: dict[str, int] = {}
    total = previous = 0
    for alert in group:
        if alert.age_days is None or alert.count is None:
            message = "a High alert needs age_days and count"
            raise ValueError(message)
        ages.append(alert.age_days)
        totals[alert.key.action_key] = totals.get(alert.key.action_key, 0) + alert.count
        total += alert.count
        previous += alert.previous_count or 0
    problems = text(language, "alert.problems", count=total)
    if previous > 0:
        problems += text(language, "alert.rise", previous=previous)
    age = held_text(max(ages), language)

    def build(*, name_worst: bool) -> str:
        return text(
            language,
            "alert.project.line",
            marker=marker(first.kind, language),
            name=escape(clean_name(first.project_name, language=language)),
            problems=problems,
            where=_where(totals, language, name_worst=name_worst),
            age=age,
        )

    line = build(name_worst=True)
    if len(totals) > 1 and len(line) > ALERT_LINE_FIT_CHARS:
        return build(name_worst=False)
    return line


def render_alert(alert: Alert, *, language: Language = DEFAULT_LANGUAGE) -> str:
    """Exactly one line."""
    name = escape(clean_name(alert.project_name, language=language))
    if alert.kind is AlertKind.STALE:
        if alert.age_days is None or alert.last_run_at is None:
            message = "a stale alert needs age_days and last_run_at"
            raise ValueError(message)
        return text(
            language,
            "alert.stale.line",
            marker=marker(alert.kind, language),
            name=name,
            days=days_text(alert.age_days, language=language),
        )
    if alert.kind is AlertKind.NEVER_RAN:
        return text(
            language, "alert.no_scan_yet.line", marker=marker(alert.kind, language), name=name
        )
    if alert.kind is AlertKind.HIGH_FINDINGS:
        if alert.seen_since is None:
            message = "a High alert needs seen_since"
            raise ValueError(message)
        return project_high_line([alert], language)
    message = f"unsupported alert kind {alert.kind}"  # type: ignore[unreachable]
    raise ValueError(message)


def triage_line(item: ItemTriage, *, language: Language = DEFAULT_LANGUAGE) -> str:
    """The Claude line under an item: `   P1 · <cause>`. The cause is escaped here."""
    return text(
        language,
        "alert.triage_line",
        priority=item.priority.upper(),
        cause=escape(item.cause),
    )


def summary_header(
    projects: int, index: int, count: int, *, language: Language = DEFAULT_LANGUAGE
) -> str:
    header = text(
        language, "summary.short_header", projects=counted(language, "noun.project", projects)
    )
    return f"{header} ({index}/{count})" if count > 1 else header


def _sort_key(alert: Alert) -> tuple[int, int, int, str, str]:
    age = alert.age_days
    return (
        SEVERITY_ORDER.index(alert.severity),
        1 if age is None else 0,
        -(age or 0),
        clean_name(alert.project_name).casefold(),
        alert.key.as_text(),
    )


def pack_items(items: Sequence[str], header_units: int, max_chars: int) -> list[list[int]]:
    """Greedy: indexes of the items of each chunk, in order, each chunk within max_chars."""
    chunks: list[list[int]] = []
    used = 0
    for index, item in enumerate(items):
        cost = 1 + telegram_length(item)  # the joining newline
        if header_units + cost > max_chars:
            message = "a single summary item does not fit in max_chars"
            raise ValueError(message)
        if chunks and used + cost <= max_chars:
            chunks[-1].append(index)
            used += cost
        else:
            chunks.append([index])
            used = header_units + cost
    return chunks


_PRIORITY_RANK = {"p1": 0, "p2": 1, "p3": 2}


@dataclass(frozen=True)
class _Line:
    """One line of the group message (plus its Claude line) and the alert keys it stands for."""

    project_id: str
    text: str
    keys: tuple[AlertKey, ...]


def _best_triage(
    group: Sequence[Alert], triage: Mapping[AlertKey, ItemTriage]
) -> ItemTriage | None:
    """The most urgent entry of a project; on a tie the first alert of the group (the oldest)."""
    found = [triage[alert.key] for alert in group if alert.key in triage]
    if not found:
        return None
    if len(found) > 1:
        logger.info("kept 1 of %d triage lines for project %s", len(found), group[0].key.project_id)
    return min(enumerate(found), key=lambda pair: (_PRIORITY_RANK[pair[1].priority], pair[0]))[1]


def _triage_in_set(
    ordered: Sequence[Alert], triage: Mapping[AlertKey, ItemTriage] | None
) -> Mapping[AlertKey, ItemTriage]:
    """Entries for keys that are alerts here; the others are only counted."""
    if not triage:
        return {}
    present = {alert.key for alert in ordered}
    stray = len(triage.keys() - present)
    if stray:
        logger.warning("ignored %d triage entries for alerts that are not in this set", stray)
    return {key: entry for key, entry in triage.items() if key in present}


def _lines(
    ordered: Sequence[Alert], language: Language, triage: Mapping[AlertKey, ItemTriage]
) -> list[_Line]:
    """One line per project's High alerts, one per other alert; a High line sits where the
    project's first (oldest, worst) alert sorts."""
    lines: list[_Line | None] = []
    groups: dict[str, list[Alert]] = {}
    slots: dict[str, int] = {}
    for alert in ordered:
        if alert.kind is not AlertKind.HIGH_FINDINGS:
            line = render_alert(alert, language=language)
            lines.append(_Line(alert.key.project_id, line, (alert.key,)))
            continue
        project_id = alert.key.project_id
        if project_id not in groups:
            slots[project_id] = len(lines)
            lines.append(None)
        groups.setdefault(project_id, []).append(alert)
    for project_id, group in groups.items():
        line = project_high_line(group, language)
        best = _best_triage(group, triage)
        if best is not None:
            line = f"{line}\n{triage_line(best, language=language)}"
        lines[slots[project_id]] = _Line(project_id, line, tuple(a.key for a in group))
    result = [line for line in lines if line is not None]
    if len(result) < len(ordered):
        logger.info("grouped %d alerts into %d lines", len(ordered), len(result))
    return result


def _summary_messages(
    lines: Sequence[_Line], max_chars: int, language: Language
) -> list[OutboundMessage]:
    total = len({line.project_id for line in lines})
    items = [line.text for line in lines]
    head_units = telegram_length(summary_header(total, 1, 1, language=language))
    chunks = pack_items(items, head_units, max_chars)
    if len(chunks) > 1:
        # Reserve room for the " (k/n)" suffix; more chunks widen it, so repack until stable.
        count = len(chunks)
        while True:
            reserve = telegram_length(f" ({count}/{count})")
            chunks = pack_items(items, head_units + reserve, max_chars)
            if len(chunks) <= count:
                break
            count = len(chunks)
    messages = []
    for number, indexes in enumerate(chunks, start=1):
        header = summary_header(total, number, len(chunks), language=language)
        body = "\n".join([header, *(items[i] for i in indexes)])
        keys = tuple(key for i in indexes for key in lines[i].keys)
        messages.append(OutboundMessage(text=body, keys=keys))
    return messages


def render_messages(
    alerts: Sequence[Alert],
    *,
    collapse_above: int,
    max_chars: int = DEFAULT_MAX_CHARS,
    language: Language = DEFAULT_LANGUAGE,
    triage: Mapping[AlertKey, ItemTriage] | None = None,
) -> list[OutboundMessage]:
    """One line per project's High alerts and one per stale / never-ran alert, worst first.
    Up to `collapse_above` lines go one by one, more become one summary that is split between
    lines into numbered chunks of at most `max_chars` UTF-16 units.

    `triage` only adds a Claude line under a project line (at most one: the most urgent entry).
    Order, the collapse decision, keys and every rule-text part are settled before the map is
    read, so it cannot change which alerts go out."""
    if not MIN_MAX_CHARS <= max_chars <= TELEGRAM_MAX_CHARS:
        message = f"max_chars must be between {MIN_MAX_CHARS} and {TELEGRAM_MAX_CHARS}"
        raise ValueError(message)
    ordered = sorted(alerts, key=_sort_key)
    lines = _lines(ordered, language, _triage_in_set(ordered, triage))
    if len(lines) <= collapse_above:
        return [OutboundMessage(text=line.text, keys=line.keys) for line in lines]
    messages = _summary_messages(lines, max_chars, language)
    logger.info("collapsed %d lines into %d summary message(s)", len(lines), len(messages))
    return messages
