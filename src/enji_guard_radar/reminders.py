"""Reminders for open High series (STATE-02, D-11): which are due, and the one message for them.

Pure: the caller passes `now` and the clocks; marking a reminder as sent is the pipeline's job.
"""

import logging
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from enji_guard_radar.ledger import SeriesKey
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
from enji_guard_radar.model import Alert, AlertKey, AlertKind, OutboundMessage

logger = logging.getLogger(__name__)

REMINDER_TITLE = "Reminder: High findings are still open"


def _series(key: AlertKey) -> SeriesKey:
    return (key.project_id, key.repo_id, key.action_key)


def _order(alert: Alert) -> tuple[int, int, int, str, str]:
    age = alert.age_days
    return (
        -(alert.count or 0),
        1 if age is None else 0,
        -(age or 0),
        clean_name(alert.project_name).casefold(),
        alert.key.as_text(),
    )


def reminders_due(
    alerts: Sequence[Alert],
    *,
    alerted_at: Mapping[AlertKey, datetime],
    reminded_at: Mapping[SeriesKey, datetime],
    planned: Collection[AlertKey],
    every_days: int,
    now: datetime,
) -> tuple[Alert, ...]:
    """Open High series whose last alert or reminder is at least `every_days` old, worst first.

    A series with no `alerted_at` was never alerted and is never reminded; a series being
    alerted in this run (`planned`) is not reminded. There is no cap: the same series is due
    again every interval while it stays open.
    """
    if now.tzinfo is None:
        message = "now must be timezone-aware"
        raise ValueError(message)
    if every_days < 1:
        message = "every_days must be at least 1"
        raise ValueError(message)
    interval = timedelta(days=every_days)
    due: dict[AlertKey, Alert] = {}
    for alert in alerts:
        if alert.kind is not AlertKind.HIGH_FINDINGS or not alert.count or alert.key in planned:
            continue
        if alert.key in due:
            continue
        alerted = alerted_at.get(alert.key)
        if alerted is None:
            logger.debug("reminder skipped for %s: never alerted", alert.key.as_text())
            continue
        reminded = reminded_at.get(_series(alert.key))
        clock = alerted if reminded is None else max(alerted, reminded)
        elapsed = now - clock
        if elapsed >= interval:
            due[alert.key] = alert
            logger.debug(
                "reminder due for %s: clock %d days old", alert.key.as_text(), elapsed.days
            )
        else:
            logger.debug(
                "reminder not due for %s: clock %d days old", alert.key.as_text(), elapsed.days
            )
    return tuple(sorted(due.values(), key=_order))


def _title(index: int, count: int) -> str:
    suffix = f" ({index}/{count})" if count > 1 else ""
    return f"<b>{REMINDER_TITLE}{suffix}</b>"


def _item(alert: Alert) -> str:
    if alert.count is None or alert.seen_since is None or alert.age_days is None:
        message = "a reminder needs count, seen_since and age_days"
        raise ValueError(message)
    name = escape(clean_name(alert.project_name))
    since = alert.seen_since.astimezone(UTC).date().isoformat()
    what = f"{high_findings_text(alert.count)} in {high_where(alert)}"
    return (
        f"- <b>{name}</b>: {what}. Seen by the radar since {since} ({days_text(alert.age_days)})."
    )


def render_reminders(
    alerts: Sequence[Alert], *, max_chars: int = DEFAULT_MAX_CHARS
) -> list[OutboundMessage]:
    """One reminder message for all due series, in the given order; split only when too long."""
    if not MIN_MAX_CHARS <= max_chars <= TELEGRAM_MAX_CHARS:
        message = f"max_chars must be between {MIN_MAX_CHARS} and {TELEGRAM_MAX_CHARS}"
        raise ValueError(message)
    if not alerts:
        return []
    items = [_item(alert) for alert in alerts]
    chunks = pack_items(items, telegram_length(_title(1, 1)), max_chars)
    if len(chunks) > 1:
        # Reserve room for the " (k/n)" suffix; more chunks widen it, so repack until stable.
        count = len(chunks)
        while True:
            reserve = telegram_length(f" ({count}/{count})")
            chunks = pack_items(items, telegram_length(_title(1, 1)) + reserve, max_chars)
            if len(chunks) <= count:
                break
            count = len(chunks)
    messages = []
    for number, indexes in enumerate(chunks, start=1):
        text = "\n".join([_title(number, len(chunks)), *(items[i] for i in indexes)])
        messages.append(OutboundMessage(text=text, keys=tuple(alerts[i].key for i in indexes)))
    logger.info("reminders: %d series in %d message(s)", len(alerts), len(messages))
    return messages
