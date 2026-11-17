"""Alert text for Telegram (HTML parse mode). One escaping point for dynamic text."""

import html
import logging
import re
import unicodedata
from collections.abc import Sequence
from datetime import UTC

from enji_guard_radar.model import SEVERITY_ORDER, Alert, AlertKind, OutboundMessage

logger = logging.getLogger(__name__)

TELEGRAM_MAX_CHARS = 4096
# Headroom under the Telegram limit; also the default size of one summary chunk.
DEFAULT_MAX_CHARS = 4000
# A cleaned name is at most 80 characters, each escaping to at most 5 UTF-16 units (400), plus
# about 120 of fixed text: one alert or one summary item stays near 520 units, far below this.
MIN_MAX_CHARS = 1000
MAX_NAME_CHARS = 80
UNNAMED_PROJECT = "(unnamed project)"


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def clean_name(name: str) -> str:
    """Guard text becomes one short line. Not escaped: `escape` is the single escaping point."""
    spaced = "".join(
        " "
        if unicodedata.category(ch).startswith("C") or unicodedata.category(ch) in {"Zl", "Zp"}
        else ch
        for ch in name
    )
    collapsed = re.sub(r"\s+", " ", spaced).strip()
    if not collapsed:
        return UNNAMED_PROJECT
    if len(collapsed) > MAX_NAME_CHARS:
        return collapsed[: MAX_NAME_CHARS - 3] + "..."
    return collapsed


def telegram_length(text: str) -> int:
    """Telegram counts UTF-16 code units; the final HTML text is never shorter than that count."""
    return len(text.encode("utf-16-le")) // 2


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


def render_alert(alert: Alert) -> str:
    name = escape(clean_name(alert.project_name))
    severity = f"Severity: {alert.severity.value}."
    if alert.kind is AlertKind.STALE:
        if alert.age_days is None or alert.last_run_at is None:
            message = "a stale alert needs age_days and last_run_at"
            raise ValueError(message)
        last = alert.last_run_at.astimezone(UTC).date().isoformat()
        lines = [
            f"Stale Guard scans: <b>{name}</b>",
            f"No completed Guard run for {_days(alert.age_days)} (last: {last}).",
            severity,
        ]
    else:
        if alert.repo_count == 0:
            detail = "It has no repositories in Guard."
        elif alert.repo_count == 1:
            detail = "No completed Guard run found in its 1 repository."
        else:
            detail = f"No completed Guard run found in its {alert.repo_count} repositories."
        lines = [f"No Guard runs: <b>{name}</b>", detail, severity]
    return "\n".join(lines)


def render_summary_item(alert: Alert) -> str:
    name = escape(clean_name(alert.project_name))
    severity = alert.severity.value
    if alert.kind is AlertKind.STALE:
        if alert.age_days is None:
            message = "a stale alert needs age_days"
            raise ValueError(message)
        return f"- <b>{name}</b>: no completed Guard run for {_days(alert.age_days)} ({severity})"
    return f"- <b>{name}</b>: no completed Guard run found ({severity})"


def summary_header(total: int, index: int, count: int) -> str:
    header = f"Guard radar: {total} problems need attention"
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


def _pack(items: Sequence[str], header_units: int, max_chars: int) -> list[list[int]]:
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


def _summary_messages(ordered: Sequence[Alert], max_chars: int) -> list[OutboundMessage]:
    total = len(ordered)
    items = [render_summary_item(alert) for alert in ordered]
    chunks = _pack(items, telegram_length(summary_header(total, 1, 1)), max_chars)
    if len(chunks) > 1:
        # Reserve room for the " (k/n)" suffix; more chunks widen it, so repack until stable.
        count = len(chunks)
        while True:
            reserve = telegram_length(f" ({count}/{count})")
            chunks = _pack(items, telegram_length(summary_header(total, 1, 1)) + reserve, max_chars)
            if len(chunks) <= count:
                break
            count = len(chunks)
    messages = []
    for number, indexes in enumerate(chunks, start=1):
        header = summary_header(total, number, len(chunks))
        text = "\n".join([header, *(items[i] for i in indexes)])
        messages.append(OutboundMessage(text=text, keys=tuple(ordered[i].key for i in indexes)))
    return messages


def render_messages(
    alerts: Sequence[Alert], *, collapse_above: int, max_chars: int = DEFAULT_MAX_CHARS
) -> list[OutboundMessage]:
    """Worst first. Up to `collapse_above` alerts go one by one, more become one summary that is
    split between items into numbered chunks of at most `max_chars` UTF-16 units."""
    if not MIN_MAX_CHARS <= max_chars <= TELEGRAM_MAX_CHARS:
        message = f"max_chars must be between {MIN_MAX_CHARS} and {TELEGRAM_MAX_CHARS}"
        raise ValueError(message)
    ordered = sorted(alerts, key=_sort_key)
    if len(ordered) <= collapse_above:
        return [OutboundMessage(text=render_alert(alert), keys=(alert.key,)) for alert in ordered]
    messages = _summary_messages(ordered, max_chars)
    logger.info("collapsed %d alerts into %d summary message(s)", len(ordered), len(messages))
    return messages
