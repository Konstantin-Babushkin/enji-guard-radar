"""Reminders for open High series (STATE-02, D-11): which are due, and the one message for them.

Pure: the caller passes `now` and the clocks; marking a reminder as sent is the pipeline's job.
"""

import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from enji_guard_radar.high_rules import HIGH_LEVEL
from enji_guard_radar.ledger import ReminderClock, SeriesKey, State
from enji_guard_radar.messages import (
    DEFAULT_MAX_CHARS,
    MIN_MAX_CHARS,
    TELEGRAM_MAX_CHARS,
    clean_name,
    pack_items,
    project_groups,
    project_high_line,
    telegram_length,
)
from enji_guard_radar.model import Alert, AlertKey, AlertKind, OutboundMessage
from enji_guard_radar.texts import DEFAULT_LANGUAGE, Language, counted, text

logger = logging.getLogger(__name__)


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
    every_days_by_project: Mapping[str, int] | None = None,
    now: datetime,
) -> tuple[Alert, ...]:
    """Open High series whose last alert or reminder is at least `every_days` old, worst first.

    A series with no `alerted_at` was never alerted and is never reminded; a series being
    alerted in this run (`planned`) is not reminded. There is no cap: the same series is due
    again every interval while it stays open. `every_days_by_project` overrides `every_days` for
    the projects it names.
    """
    if now.tzinfo is None:
        message = "now must be timezone-aware"
        raise ValueError(message)
    if every_days < 1:
        message = "every_days must be at least 1"
        raise ValueError(message)
    by_project = every_days_by_project or {}
    if any(days < 1 for days in by_project.values()):
        message = "every_days must be at least 1"
        raise ValueError(message)
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
        if elapsed >= timedelta(days=by_project.get(alert.key.project_id, every_days)):
            due[alert.key] = alert
            logger.debug(
                "reminder due for %s: clock %d days old", alert.key.as_text(), elapsed.days
            )
        else:
            logger.debug(
                "reminder not due for %s: clock %d days old", alert.key.as_text(), elapsed.days
            )
    return tuple(sorted(due.values(), key=_order))


def _title(index: int, count: int, projects: int, language: Language) -> str:
    suffix = f" ({index}/{count})" if count > 1 else ""
    counted_projects = counted(language, "noun.project", projects)
    return f"<b>{text(language, 'reminder.short_title', projects=counted_projects)}{suffix}</b>"


def render_reminders(
    alerts: Sequence[Alert],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    language: Language = DEFAULT_LANGUAGE,
) -> list[OutboundMessage]:
    """One line per project for all due series, worst first; split only when too long."""
    if not MIN_MAX_CHARS <= max_chars <= TELEGRAM_MAX_CHARS:
        message = f"max_chars must be between {MIN_MAX_CHARS} and {TELEGRAM_MAX_CHARS}"
        raise ValueError(message)
    if not alerts:
        return []
    groups = project_groups(alerts)
    items = [project_high_line(group, language) for group in groups]
    total = len(groups)
    head_units = telegram_length(_title(1, 1, total, language))
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
        title = _title(number, len(chunks), total, language)
        text_ = "\n".join([title, *(items[i] for i in indexes)])
        keys = tuple(alert.key for i in indexes for alert in groups[i])
        messages.append(OutboundMessage(text=text_, keys=keys))
    logger.info(
        "reminders: %d series of %d project(s) in %d message(s)", len(alerts), total, len(messages)
    )
    return messages


def mark_reminded(state: State, keys: Iterable[AlertKey], now: datetime) -> State:
    """Write the reminder clock of every High series in `keys`; call it only after an ack."""
    if now.tzinfo is None:
        message = "now must be timezone-aware"
        raise ValueError(message)
    clocks = {clock.key(): clock for clock in state.reminders}
    for key in keys:
        if key.level != HIGH_LEVEL:
            continue
        series = _series(key)
        clocks[series] = ReminderClock(
            project_id=key.project_id,
            repo_id=key.repo_id,
            action_key=key.action_key,
            last_reminded_at=now,
        )
    ordered = tuple(sorted(clocks.values(), key=lambda clock: clock.key()))
    return state.model_copy(update={"reminders": ordered})
