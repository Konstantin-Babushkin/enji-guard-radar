"""Recurring rule (RULE-08): N consecutive completed runs with High findings, by task_id."""

import logging
from datetime import UTC, datetime, timedelta

import pytest

from enji_guard_radar.high_history import HISTORY_WINDOW, Known, advance_series
from enji_guard_radar.ledger import RunPoint, SeriesHistory
from enji_guard_radar.recurring_rules import is_recurring, trailing_high_runs

KEY = ("p1", "r1", "audit.security")
T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)


def at(day: int) -> datetime:
    return T0 + timedelta(days=day)


def series(highs: list[int], *, open_: bool = True) -> SeriesHistory:
    """A stored history built directly: oldest run first, one run a day."""
    return SeriesHistory(
        project_id=KEY[0],
        repo_id=KEY[1],
        action_key=KEY[2],
        points=tuple(
            RunPoint(task_id=f"t{day}", completed_at=at(day), high=high)
            for day, high in enumerate(highs)
        ),
        open=open_,
        opened_at=at(0) if open_ else None,
    )


def fold(highs: list[int], *, clear_after: int = 2) -> SeriesHistory | None:
    """The same runs folded through the real history fold."""
    history: SeriesHistory | None = None
    for day, high in enumerate(highs):
        history = advance_series(
            history,
            Known(task_id=f"t{day}", completed_at=at(day), high=high),
            key=KEY,
            clear_after=clear_after,
        )
    return history


@pytest.mark.parametrize(
    ("highs", "expected"),
    [([], 0), ([2, 1, 3], 3), ([2, 0, 3], 1), ([0, 4], 1), ([3, 3, 0], 0), ([0, 0], 0)],
)
def test_trailing_high_runs_counts_the_newest_unbroken_tail(
    highs: list[int], expected: int
) -> None:
    assert trailing_high_runs(series(highs)) == expected


def test_trailing_high_runs_without_history_is_zero() -> None:
    assert trailing_high_runs(None) == 0


@pytest.mark.parametrize(
    ("highs", "after_runs", "expected"),
    [
        ([1, 1, 1], 3, True),
        ([1, 1, 1], 4, False),
        ([1, 0, 1, 1], 3, False),
        ([1, 0, 1, 1], 2, True),
        ([5, 1], 2, True),
        ([1], 2, False),
    ],
)
def test_recurring_needs_the_whole_window_of_high_runs(
    highs: list[int], after_runs: int, expected: bool
) -> None:
    assert is_recurring(series(highs), after_runs=after_runs) is expected


def test_recurring_is_false_for_a_closed_series_and_for_none() -> None:
    assert is_recurring(series([1, 1, 1], open_=False), after_runs=3) is False
    assert is_recurring(None, after_runs=2) is False


def test_recurring_window_of_twenty_is_satisfiable_only_with_a_full_history() -> None:
    full = fold([1] * HISTORY_WINDOW)
    short = fold([1] * (HISTORY_WINDOW - 1))
    assert full is not None
    assert short is not None
    assert len(full.points) == HISTORY_WINDOW
    assert is_recurring(full, after_runs=HISTORY_WINDOW) is True
    assert is_recurring(short, after_runs=HISTORY_WINDOW) is False
    # Older runs fall out of the stored window; the tail still counts.
    longer = fold([1] * (HISTORY_WINDOW + 5))
    assert is_recurring(longer, after_runs=HISTORY_WINDOW) is True


def test_recurring_logs_the_series_ids_and_count(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="enji_guard_radar.recurring_rules"):
        is_recurring(series([1, 1]), after_runs=2)
    assert "p1/r1/audit.security: 2 trailing High runs, need 2" in caplog.text


@pytest.mark.parametrize("after_runs", [1, 0, -3])
def test_invalid_after_runs_below_two_raises(after_runs: int) -> None:
    with pytest.raises(ValueError, match="at least 2"):
        is_recurring(series([1, 1, 1]), after_runs=after_runs)
    with pytest.raises(ValueError, match="at least 2"):
        is_recurring(None, after_runs=after_runs)


def test_invalid_boundary_two_is_accepted() -> None:
    assert is_recurring(series([1, 1]), after_runs=2) is True


def test_task_id_same_run_seen_three_times_is_one_point() -> None:
    history: SeriesHistory | None = None
    for _ in range(3):
        history = advance_series(
            history, Known(task_id="t0", completed_at=at(0), high=2), key=KEY, clear_after=2
        )
    assert history is not None
    assert len(history.points) == 1
    assert is_recurring(history, after_runs=2) is False


def test_task_id_late_older_run_does_not_change_the_answer() -> None:
    history = fold([1, 1])
    assert history is not None
    assert is_recurring(history, after_runs=3) is False
    late = advance_series(
        history, Known(task_id="old", completed_at=at(-5), high=4), key=KEY, clear_after=2
    )
    assert late == history
    assert is_recurring(late, after_runs=3) is False


def test_debounce_open_series_with_a_clean_latest_run_is_not_recurring() -> None:
    history = fold([1, 1, 0], clear_after=2)
    assert history is not None
    assert (history.open, history.clean_runs) == (True, 1)
    assert trailing_high_runs(history) == 0
    assert is_recurring(history, after_runs=2) is False
