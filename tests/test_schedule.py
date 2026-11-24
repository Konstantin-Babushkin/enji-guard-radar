"""Slot math: table tests of the pure functions."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from enji_guard_radar.schedule import (
    catch_up_due,
    format_slot,
    latest_slot,
    next_slot,
    skipped_slots,
)


def t(hour: int = 0, minute: int = 0, second: int = 0, micro: int = 0) -> datetime:
    return datetime(2026, 11, 23, hour, minute, second, micro, tzinfo=UTC)


@pytest.mark.parametrize(
    ("now", "every", "latest", "following"),
    [
        (t(3, 0, 0), 180, t(3), t(6)),
        (t(2, 59, 59), 180, t(0), t(3)),
        (t(3, 0, 1), 180, t(3), t(6)),
        (t(2, 59, 59, micro=999999), 180, t(0), t(3)),
        (t(0, 0, 0), 180, t(0), t(3)),
        (t(23, 59), 180, t(21), t(23, 59).replace(hour=0, minute=0) + timedelta(days=1)),
        (t(12, 34, 56), 1, t(12, 34), t(12, 35)),
        (t(12, 34, 56), 60, t(12), t(13)),
        (t(12, 34, 56), 720, t(12), t(12) + timedelta(hours=12)),
        (t(12, 34, 56), 1440, t(0), t(0) + timedelta(days=1)),
    ],
)
def test_latest_and_next_slot(
    now: datetime, every: int, latest: datetime, following: datetime
) -> None:
    assert latest_slot(now, every) == latest
    assert next_slot(now, every) == following


def test_non_utc_input_gives_the_same_slots() -> None:
    plus_three = timezone(timedelta(hours=3))
    now = datetime(2026, 11, 23, 5, 10, tzinfo=plus_three)  # 02:10 UTC
    assert latest_slot(now, 180) == t(0)
    assert next_slot(now, 180) == t(3)
    assert latest_slot(now, 180).tzinfo is UTC


def test_acceptance_check_lines() -> None:
    assert next_slot(t(2, 59, 59), 180).isoformat() == "2026-11-23T03:00:00+00:00"
    assert next_slot(t(3, 0, 0), 180).isoformat() == "2026-11-23T06:00:00+00:00"
    assert latest_slot(t(3, 0, 0), 180).isoformat() == "2026-11-23T03:00:00+00:00"
    assert latest_slot(t(2, 59, 59), 180).isoformat() == "2026-11-23T00:00:00+00:00"
    assert next_slot(t(23, 59), 180).isoformat() == "2026-11-24T00:00:00+00:00"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (t(3, 0), t(10, 30), (t(6), t(9))),
        (t(3, 1), t(5, 59), ()),
        (t(3, 1), t(6, 0), (t(6),)),  # end on a slot is included
        (t(3, 0), t(3, 0), ()),  # start on a slot is excluded
        (t(3, 0), t(6, 0), (t(6),)),
        (t(4, 10), t(4, 5), ()),  # end before start
        (t(0, 0), t(23, 0), (t(3), t(6), t(9), t(12), t(15), t(18), t(21))),
    ],
)
def test_skipped_slots(start: datetime, end: datetime, expected: tuple[datetime, ...]) -> None:
    assert skipped_slots(start, end, 180) == expected


@pytest.mark.parametrize(
    ("now", "last", "due"),
    [
        (t(4, 10), t(2, 50), True),
        (t(4, 10), t(3, 5), False),
        (t(4, 10), None, True),
        (t(3, 0), t(3, 0), False),  # exactly on the slot
        (t(3, 0), t(2, 59, 59), True),
        (t(4, 10), t(5, 0), False),  # a last poll in the future is not due
    ],
)
def test_catch_up_due(now: datetime, last: datetime | None, due: bool) -> None:
    assert catch_up_due(now, last, 180) is due


def test_naive_datetimes_are_rejected() -> None:
    naive = datetime(2026, 11, 23, 4, 0)  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        latest_slot(naive, 180)
    with pytest.raises(ValueError, match="timezone-aware"):
        next_slot(naive, 180)
    with pytest.raises(ValueError, match="timezone-aware"):
        skipped_slots(naive, t(5), 180)
    with pytest.raises(ValueError, match="timezone-aware"):
        skipped_slots(t(5), naive, 180)
    with pytest.raises(ValueError, match="timezone-aware"):
        catch_up_due(naive, None, 180)
    with pytest.raises(ValueError, match="timezone-aware"):
        catch_up_due(t(5), naive, 180)
    with pytest.raises(ValueError, match="timezone-aware"):
        format_slot(naive)


def test_format_slot() -> None:
    assert format_slot(t(6)) == "2026-11-23T06:00:00Z"
    plus_three = timezone(timedelta(hours=3))
    assert format_slot(datetime(2026, 11, 23, 9, 0, 5, 999, tzinfo=plus_three)) == (
        "2026-11-23T06:00:05Z"
    )
