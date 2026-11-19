"""Alert text for Telegram (HTML parse mode). One escaping point for dynamic text."""

import html
import logging
import re
import unicodedata
from collections.abc import Sequence

from enji_guard_radar.model import SEVERITY_ORDER, Alert, AlertKind, OutboundMessage
from enji_guard_radar.texts import DEFAULT_LANGUAGE, Language, counted, severity_word, text

logger = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4096
# Headroom under the Telegram limit; also the default size of one summary chunk.
DEFAULT_MAX_CHARS = 4000
# A cleaned name is at most 80 characters, each escaping to at most 5 UTF-16 units (400), plus
# about 150 of fixed text (Russian is the longer language): one alert or one summary item stays
# near 550 units, far below this.
MIN_MAX_CHARS = 1000
MAX_NAME_CHARS = 80
# The English texts other modules and tests name; the catalog holds them for every language.
UNNAMED_PROJECT = text("en", "name.unnamed")
LONG_STANDING_PREFIX = "Long-standing: "


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


def high_where(alert: Alert) -> str:
    """The shared "<audit type> (<repo>)" part; a High alert is meaningless without its repo."""
    if alert.repo_name is None:
        message = "a High alert needs repo_name"
        raise ValueError(message)
    return f"{escape(alert.key.action_key)} ({escape(clean_name(alert.repo_name))})"


def _high_count(alert: Alert) -> int:
    if alert.count is None:
        message = "a High alert needs count"
        raise ValueError(message)
    return alert.count


def _is_rise(alert: Alert) -> bool:
    return alert.previous_count is not None and _high_count(alert) > alert.previous_count


def high_findings_text(count: int, *, language: Language = DEFAULT_LANGUAGE) -> str:
    return counted(language, "noun.finding", count)


def marker(kind: AlertKind, language: Language = DEFAULT_LANGUAGE) -> str:
    """The colour marker, chosen by code from the alert kind only."""
    key = "marker.high" if kind is AlertKind.HIGH_FINDINGS else "marker.other"
    return text(language, key)


def audit_short(action_key: str) -> str:
    """The audit type without its `audit.` prefix, escaped."""
    return escape(action_key.removeprefix("audit."))


def age_text(days: int, language: Language = DEFAULT_LANGUAGE) -> str:
    return text(language, "alert.age", days=days)


def ambiguous_series(alerts: Sequence[Alert]) -> frozenset[tuple[str, str]]:
    """(project_id, action_key) pairs that more than one repo holds in this set of alerts."""
    repos: dict[tuple[str, str], set[str]] = {}
    for alert in alerts:
        if alert.kind is AlertKind.HIGH_FINDINGS:
            repos.setdefault((alert.key.project_id, alert.key.action_key), set()).add(
                alert.key.repo_id
            )
    return frozenset(pair for pair, ids in repos.items() if len(ids) > 1)


def high_line(
    alert: Alert, *, language: Language = DEFAULT_LANGUAGE, show_repo: bool, arrow: bool
) -> str:
    """One High line. `arrow` shows `High <previous or 0>→<count>`, else `High <count>`."""
    if alert.age_days is None:
        message = "a High alert needs age_days"
        raise ValueError(message)
    if alert.repo_name is None:
        message = "a High alert needs repo_name"
        raise ValueError(message)
    count = _high_count(alert)
    audit = audit_short(alert.key.action_key)
    if show_repo:
        repo = escape(clean_name(alert.repo_name))
        audit = text(language, "alert.where_repo", audit=audit, repo=repo)
    params = {
        "marker": marker(alert.kind, language),
        "name": escape(clean_name(alert.project_name, language=language)),
        "audit": audit,
        "count": count,
        "age": age_text(alert.age_days, language),
    }
    if arrow:
        previous = alert.previous_count or 0
        return text(language, "alert.high.line", previous=previous, **params)
    return text(language, "alert.high.line_count", **params)


def render_alert(
    alert: Alert, *, language: Language = DEFAULT_LANGUAGE, show_repo: bool = False
) -> str:
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
            age=age_text(alert.age_days, language),
        )
    if alert.kind is AlertKind.NEVER_RAN:
        return text(
            language, "alert.never_ran.line", marker=marker(alert.kind, language), name=name
        )
    if alert.kind is AlertKind.HIGH_FINDINGS:
        if alert.seen_since is None:
            message = "a High alert needs seen_since"
            raise ValueError(message)
        return high_line(alert, language=language, show_repo=show_repo, arrow=True)
    message = f"unsupported alert kind {alert.kind}"  # type: ignore[unreachable]
    raise ValueError(message)


def render_summary_item(alert: Alert, *, language: Language = DEFAULT_LANGUAGE) -> str:
    name = escape(clean_name(alert.project_name, language=language))
    severity = severity_word(language, alert.severity)
    if alert.kind is AlertKind.STALE:
        if alert.age_days is None:
            message = "a stale alert needs age_days"
            raise ValueError(message)
        days = days_text(alert.age_days, language=language)
        return text(language, "summary.item.stale", name=name, days=days, severity=severity)
    if alert.kind is AlertKind.HIGH_FINDINGS:
        where = high_where(alert)
        count = _high_count(alert)
        if _is_rise(alert):
            return text(
                language,
                "summary.item.high_rise",
                name=name,
                previous=alert.previous_count,
                count=count,
                where=where,
                severity=severity,
            )
        return text(
            language, "summary.item.high", name=name, count=count, where=where, severity=severity
        )
    return text(language, "summary.item.never_ran", name=name, severity=severity)


def summary_header(
    total: int, index: int, count: int, *, language: Language = DEFAULT_LANGUAGE
) -> str:
    problems = counted(language, "noun.problem", total)
    header = text(language, "summary.header", problems=problems)
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


def _shows_repo(alert: Alert, ambiguous: frozenset[tuple[str, str]]) -> bool:
    return (alert.key.project_id, alert.key.action_key) in ambiguous


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


def _summary_messages(
    ordered: Sequence[Alert], max_chars: int, language: Language
) -> list[OutboundMessage]:
    total = len(ordered)
    items = [render_summary_item(alert, language=language) for alert in ordered]
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
        text = "\n".join([header, *(items[i] for i in indexes)])
        messages.append(OutboundMessage(text=text, keys=tuple(ordered[i].key for i in indexes)))
    return messages


def render_messages(
    alerts: Sequence[Alert],
    *,
    collapse_above: int,
    max_chars: int = DEFAULT_MAX_CHARS,
    language: Language = DEFAULT_LANGUAGE,
) -> list[OutboundMessage]:
    """Worst first. Up to `collapse_above` alerts go one by one, more become one summary that is
    split between items into numbered chunks of at most `max_chars` UTF-16 units."""
    if not MIN_MAX_CHARS <= max_chars <= TELEGRAM_MAX_CHARS:
        message = f"max_chars must be between {MIN_MAX_CHARS} and {TELEGRAM_MAX_CHARS}"
        raise ValueError(message)
    ordered = sorted(alerts, key=_sort_key)
    ambiguous = ambiguous_series(ordered)
    if len(ordered) <= collapse_above:
        return [
            OutboundMessage(
                text=render_alert(
                    alert, language=language, show_repo=_shows_repo(alert, ambiguous)
                ),
                keys=(alert.key,),
            )
            for alert in ordered
        ]
    messages = _summary_messages(ordered, max_chars, language)
    logger.info("collapsed %d alerts into %d summary message(s)", len(ordered), len(messages))
    return messages
