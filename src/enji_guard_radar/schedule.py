"""Slot math for `serve`: when is the next poll due, and was the last slot missed?

Pure: no I/O and no clock reads (callers pass `now`). Slots are multiples of `every_minutes`
counted from 00:00 UTC of each day, so `every_minutes` must divide 1440 (config enforces it).
"""

from datetime import UTC, datetime, timedelta

MINUTES_PER_DAY = 1440


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        message = "moment must be timezone-aware"
        raise ValueError(message)
    return moment.astimezone(UTC)


def latest_slot(now: datetime, every_minutes: int) -> datetime:
    """The greatest slot at or before `now`."""
    utc_now = _utc(now)
    midnight = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
    step = timedelta(minutes=every_minutes)
    return midnight + ((utc_now - midnight) // step) * step


def next_slot(after: datetime, every_minutes: int) -> datetime:
    """The smallest slot strictly after `after`."""
    return latest_slot(after, every_minutes) + timedelta(minutes=every_minutes)


def skipped_slots(start: datetime, end: datetime, every_minutes: int) -> tuple[datetime, ...]:
    """Slots `s` with `start < s <= end`: the ones a run from `start` to `end` ran over."""
    end_utc = _utc(end)
    slots: list[datetime] = []
    slot = next_slot(start, every_minutes)
    while slot <= end_utc:
        slots.append(slot)
        slot += timedelta(minutes=every_minutes)
    return tuple(slots)


def catch_up_due(now: datetime, last_poll: datetime | None, every_minutes: int) -> bool:
    """True when no poll happened since the latest slot (or ever)."""
    latest = latest_slot(now, every_minutes)
    return last_poll is None or _utc(last_poll) < latest


def format_slot(moment: datetime) -> str:
    return _utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")
