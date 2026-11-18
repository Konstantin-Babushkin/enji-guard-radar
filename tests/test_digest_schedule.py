"""Digest due decision (D-01 to D-03): the default timeline, boundaries and sweeps."""

import itertools
from datetime import UTC, datetime, timedelta, timezone

import pytest

from enji_guard_radar.digest_schedule import (
    Deferred,
    DigestDecision,
    Due,
    NotDue,
    Skipped,
    decide_digest,
    planned_moment,
)

GRACE = timedelta(hours=24)
WEDNESDAY = 2
HOUR = 5


def at(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


PREV_WED = at(2026, 11, 11, 5)
THIS_WED = at(2026, 11, 18, 5)
NEXT_WED = at(2026, 11, 25, 5)


def decide(
    now: datetime,
    handled_for: datetime | None = None,
    *,
    poll_ok: bool = True,
    weekday: int = WEDNESDAY,
    hour_utc: int = HOUR,
    grace: timedelta = GRACE,
) -> DigestDecision:
    return decide_digest(
        now,
        handled_for=handled_for,
        weekday=weekday,
        hour_utc=hour_utc,
        grace=grace,
        poll_ok=poll_ok,
    )


@pytest.mark.parametrize(
    ("now", "handled_for", "poll_ok", "expected"),
    [
        (at(2026, 11, 17, 12), PREV_WED, True, NotDue("handled")),
        (at(2026, 11, 18, 4, 59), PREV_WED, True, NotDue("handled")),
        (at(2026, 11, 18, 5), PREV_WED, True, Due(THIS_WED)),
        (at(2026, 11, 18, 6), PREV_WED, True, Due(THIS_WED)),
        (at(2026, 11, 18, 6), PREV_WED, False, Deferred(THIS_WED)),
        (at(2026, 11, 19, 4, 59), PREV_WED, True, Due(THIS_WED)),
        (at(2026, 11, 19, 4, 59), THIS_WED, True, NotDue("handled")),
        (at(2026, 11, 19, 5), PREV_WED, True, Skipped(THIS_WED)),
        (at(2026, 11, 19, 5), None, True, NotDue("no_history")),
        (at(2026, 11, 20, 12), PREV_WED, True, Skipped(THIS_WED)),
        (at(2026, 11, 25, 4, 59), THIS_WED, True, NotDue("handled")),
        (at(2026, 11, 25, 5), THIS_WED, True, Due(NEXT_WED)),
    ],
)
def test_default_weekly_timeline(
    now: datetime, handled_for: datetime | None, poll_ok: bool, expected: DigestDecision
) -> None:
    assert decide(now, handled_for, poll_ok=poll_ok) == expected


def test_fresh_install_inside_window_is_due() -> None:
    assert decide(at(2026, 11, 18, 6), None) == Due(THIS_WED)


@pytest.mark.parametrize(
    ("handled_for", "poll_ok", "expected_at_grace", "expected_before"),
    [
        (PREV_WED, True, Skipped(THIS_WED), Due(THIS_WED)),
        (PREV_WED, False, Skipped(THIS_WED), Deferred(THIS_WED)),
        (None, True, NotDue("no_history"), Due(THIS_WED)),
        (None, False, NotDue("no_history"), Deferred(THIS_WED)),
    ],
)
def test_grace_boundary(
    handled_for: datetime | None,
    poll_ok: bool,
    expected_at_grace: DigestDecision,
    expected_before: DigestDecision,
) -> None:
    edge = THIS_WED + GRACE
    assert decide(edge, handled_for, poll_ok=poll_ok) == expected_at_grace
    one_second_earlier = edge - timedelta(seconds=1)
    assert decide(one_second_earlier, handled_for, poll_ok=poll_ok) == expected_before


def test_schedules_sunday_midnight() -> None:
    sunday = at(2026, 11, 22)

    def sun(now: datetime, handled_for: datetime) -> DigestDecision:
        return decide(now, handled_for, weekday=6, hour_utc=0)

    last = at(2026, 11, 15)
    assert sun(at(2026, 11, 21, 23, 59), last) == NotDue("handled")
    assert sun(sunday, last) == Due(sunday)
    assert sun(sunday + GRACE, last) == Skipped(sunday)


def test_schedules_monday_late_evening() -> None:
    monday = at(2026, 11, 16, 23)

    def mon(now: datetime, handled_for: datetime) -> DigestDecision:
        return decide(now, handled_for, weekday=0, hour_utc=23)

    last = at(2026, 11, 9, 23)
    assert mon(at(2026, 11, 16, 22, 59), last) == NotDue("handled")
    assert mon(monday, last) == Due(monday)
    assert mon(at(2026, 11, 17, 23), last) == Skipped(monday)


def test_schedules_one_hour_grace() -> None:
    grace = timedelta(hours=1)
    assert decide(at(2026, 11, 18, 5, 59, 59), PREV_WED, grace=grace) == Due(THIS_WED)
    assert decide(at(2026, 11, 18, 6), PREV_WED, grace=grace) == Skipped(THIS_WED)


def test_schedules_week_long_grace_never_skips() -> None:
    grace = timedelta(hours=168)
    just_before_next = NEXT_WED - timedelta(seconds=1)
    assert decide(just_before_next, PREV_WED, grace=grace) == Due(THIS_WED)
    assert decide(just_before_next, PREV_WED, poll_ok=False, grace=grace) == Deferred(THIS_WED)
    # At the next planned moment the later moment wins.
    assert decide(NEXT_WED, PREV_WED, grace=grace) == Due(NEXT_WED)
    assert decide(NEXT_WED, THIS_WED, grace=grace) == Due(NEXT_WED)


def test_offset_now_is_converted_to_utc() -> None:
    plus_six = timezone(timedelta(hours=6))
    now = datetime(2026, 11, 18, 11, 0, tzinfo=plus_six)
    assert decide(now, PREV_WED) == Due(THIS_WED)
    assert planned_moment(now, weekday=WEDNESDAY, hour_utc=HOUR) == THIS_WED
    assert planned_moment(now, weekday=WEDNESDAY, hour_utc=HOUR).tzinfo == UTC


def test_offset_day_rolls_over_in_utc() -> None:
    # 2026-11-19 02:00+06:00 is 2026-11-18 20:00 UTC, still Wednesday's window.
    plus_six = timezone(timedelta(hours=6))
    now = datetime(2026, 11, 19, 2, 0, tzinfo=plus_six)
    assert decide(now, PREV_WED) == Due(THIS_WED)


def test_year_end_window() -> None:
    wed = at(2026, 12, 30, 5)
    assert planned_moment(at(2026, 12, 30, 5), weekday=WEDNESDAY, hour_utc=HOUR) == wed
    assert decide(at(2026, 12, 30, 5), at(2026, 12, 23, 5)) == Due(wed)
    assert decide(at(2026, 12, 31, 4, 59), at(2026, 12, 23, 5)) == Due(wed)
    assert decide(at(2027, 1, 1, 0), wed) == NotDue("handled")
    assert decide(at(2027, 1, 1, 0), at(2026, 12, 23, 5)) == Skipped(wed)
    assert planned_moment(at(2027, 1, 1, 0), weekday=WEDNESDAY, hour_utc=HOUR) == wed


def test_year_leap_day() -> None:
    assert planned_moment(at(2028, 3, 1, 3), weekday=1, hour_utc=5) == at(2028, 2, 29, 5)


@pytest.mark.parametrize("now", [at(2026, 11, 18, 5), at(2026, 11, 18, 6), at(2026, 11, 19, 6)])
def test_backwards_clock_is_not_due(now: datetime) -> None:
    future = at(2026, 11, 25, 5)
    assert decide(now, future) == NotDue("handled")
    assert decide(now, future, poll_ok=False) == NotDue("handled")


SWEEP_SCHEDULES = [(2, 5), (0, 0), (6, 23)]
SWEEP_START = at(2026, 11, 1)
SWEEP_STEPS = 3 * 7 * 24 * 4


@pytest.mark.parametrize(("weekday", "hour_utc"), SWEEP_SCHEDULES)
def test_sweep_planned_moment_properties(weekday: int, hour_utc: int) -> None:
    for step in range(SWEEP_STEPS):
        now = SWEEP_START + timedelta(minutes=15 * step)
        moment = planned_moment(now, weekday=weekday, hour_utc=hour_utc)
        assert moment <= now
        assert now - moment < timedelta(days=7)
        assert moment.weekday() == weekday
        assert (moment.hour, moment.minute, moment.second, moment.microsecond) == (
            hour_utc,
            0,
            0,
            0,
        )
        if now.weekday() == weekday and (now.hour, now.minute) == (hour_utc, 0):
            assert moment == now


@pytest.mark.parametrize(("weekday", "hour_utc"), SWEEP_SCHEDULES)
def test_sweep_due_only_inside_a_window(weekday: int, hour_utc: int) -> None:
    due_steps = 0
    for step in range(SWEEP_STEPS):
        now = SWEEP_START + timedelta(minutes=15 * step)
        decision = decide(now, None, weekday=weekday, hour_utc=hour_utc)
        moment = planned_moment(now, weekday=weekday, hour_utc=hour_utc)
        inside = now - moment < GRACE
        assert isinstance(decision, Due) == inside
        if isinstance(decision, Due):
            due_steps += 1
            assert decision.planned_for == moment
        else:
            assert decision == NotDue("no_history")
    # Three weeks, at most four windows (a partial one at each end), 24 hours of 15 minutes each.
    assert 3 * 96 <= due_steps <= 4 * 96


@pytest.mark.parametrize(("weekday", "hour_utc"), SWEEP_SCHEDULES)
def test_sweep_handled_moment_is_never_due_again(weekday: int, hour_utc: int) -> None:
    handled: datetime | None = None
    sent: list[datetime] = []
    for step in range(SWEEP_STEPS):
        now = SWEEP_START + timedelta(minutes=15 * step)
        decision = decide(now, handled, weekday=weekday, hour_utc=hour_utc)
        if isinstance(decision, Due):
            sent.append(decision.planned_for)
            handled = decision.planned_for
    assert len(sent) == len(set(sent))
    assert all(b - a == timedelta(days=7) for a, b in itertools.pairwise(sent))


def test_naive_now_is_rejected() -> None:
    naive = datetime(2026, 11, 18, 6)  # noqa: DTZ001 - the point of the test
    with pytest.raises(ValueError, match="timezone-aware"):
        planned_moment(naive, weekday=WEDNESDAY, hour_utc=HOUR)
    with pytest.raises(ValueError, match="timezone-aware"):
        decide(naive, None)
