"""Table tests of the High history fold: open, debounce, idempotence, late runs, window."""

import logging
import random
from datetime import UTC, datetime, timedelta

import pytest

from enji_guard_radar.high_history import (
    HISTORY_WINDOW,
    Known,
    Observation,
    Unknown,
    advance_series,
    has_task,
    latest_high,
    previous_high,
)
from enji_guard_radar.ledger import SeriesHistory

KEY = ("p1", "r1", "audit.security")
T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)


def at(day: int) -> datetime:
    return T0 + timedelta(days=day)


def fold(
    runs: list[tuple[str, datetime, int]],
    *,
    clear_after: int = 2,
    start: SeriesHistory | None = None,
) -> SeriesHistory | None:
    history = start
    for task_id, completed_at, high in runs:
        history = advance_series(
            history,
            Known(task_id=task_id, completed_at=completed_at, high=high),
            key=KEY,
            clear_after=clear_after,
        )
    return history


def known(task_id: str, day: int, high: int) -> Known:
    return Known(task_id=task_id, completed_at=at(day), high=high)


def test_first_high_run_opens_an_episode_at_its_completed_at() -> None:
    history = fold([("t1", at(1), 2)])
    assert history is not None
    assert history.key() == KEY
    assert history.open is True
    assert history.opened_at == at(1)
    assert history.clean_runs == 0
    assert [p.task_id for p in history.points] == ["t1"]


def test_first_zero_high_run_only_appends() -> None:
    history = fold([("t1", at(1), 0)])
    assert history is not None
    assert history.open is False
    assert history.opened_at is None
    assert len(history.points) == 1


def test_unknown_without_history_is_none_and_with_history_changes_nothing() -> None:
    assert advance_series(None, Unknown(), key=KEY, clear_after=2) is None
    history = fold([("t1", at(1), 3)])
    assert advance_series(history, Unknown(), key=KEY, clear_after=2) == history


def test_duplicate_task_id_changes_nothing() -> None:
    history = fold([("t1", at(1), 3), ("t2", at(2), 0)])
    again = advance_series(history, known("t2", 2, 0), key=KEY, clear_after=2)
    assert again == history
    assert again is not None
    assert again.clean_runs == 1
    assert fold([("t1", at(1), 3), ("t1", at(1), 3)]) == fold([("t1", at(1), 3)])


@pytest.mark.parametrize(
    ("clear_after", "highs", "is_open", "clean_runs", "points"),
    [
        (2, [2, 0], True, 1, 2),
        (2, [2, 0, 0], False, 0, 3),
        (1, [2, 0], False, 0, 2),
        (3, [2, 0, 0], True, 2, 3),
        (3, [2, 0, 0, 0], False, 0, 4),
        (2, [2, 0, 3], True, 0, 3),
        (2, [0, 0, 0], False, 0, 3),
    ],
    ids=[
        "clean-1-of-2-stays-open",
        "clean-2-of-2-closes",
        "clean-1-of-1-closes",
        "clean-2-of-3-stays-open",
        "clean-3-of-3-closes",
        "clean-run-reset-by-high",
        "zero-while-closed-only-appends",
    ],
)
def test_clean_run_debounce(
    clear_after: int, highs: list[int], is_open: bool, clean_runs: int, points: int
) -> None:
    runs = [(f"t{i}", at(i), high) for i, high in enumerate(highs, start=1)]
    history = fold(runs, clear_after=clear_after)
    assert history is not None
    assert history.open is is_open
    assert history.clean_runs == clean_runs
    assert len(history.points) == points
    assert (history.opened_at is not None) is is_open


def test_flapping_keeps_one_episode_with_the_first_opened_at() -> None:
    history = fold([("t1", at(1), 2), ("t2", at(2), 0), ("t3", at(3), 2), ("t4", at(4), 0)])
    assert history is not None
    assert history.open is True
    assert history.clean_runs == 1
    assert history.opened_at == at(1)


def test_closed_series_reopens_with_a_new_opened_at() -> None:
    history = fold([("t1", at(1), 2), ("t2", at(2), 0), ("t3", at(3), 0), ("t4", at(4), 5)])
    assert history is not None
    assert history.open is True
    assert history.opened_at == at(4)
    assert history.clean_runs == 0


def test_late_run_is_ignored_and_logged_without_report_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    history = fold([("t1", at(5), 1)])
    with caplog.at_level(logging.WARNING):
        again = advance_series(history, known("t0", 2, 9), key=KEY, clear_after=2)
    assert again == history
    assert "late run ignored for p1|r1|audit.security: task_id=t0" in caplog.text


def test_equal_completed_at_is_ordered_by_task_id() -> None:
    history = fold([("t2", at(1), 1)])
    later_id = advance_series(history, known("t3", 1, 0), key=KEY, clear_after=5)
    assert later_id is not None
    assert [p.task_id for p in later_id.points] == ["t2", "t3"]
    smaller_id = advance_series(history, known("t1", 1, 0), key=KEY, clear_after=5)
    assert smaller_id == history


def test_history_window_keeps_the_newest_points_in_order() -> None:
    history = fold([(f"t{i:02d}", at(i), i % 2) for i in range(25)])
    assert history is not None
    assert len(history.points) == HISTORY_WINDOW == 20
    assert [p.task_id for p in history.points] == [f"t{i:02d}" for i in range(5, 25)]


def test_helpers() -> None:
    assert latest_high(None) is None
    assert previous_high(None) is None
    assert has_task(None, "t1") is False
    one = fold([("t1", at(1), 4)])
    assert latest_high(one) == 4
    assert previous_high(one) is None
    two = fold([("t1", at(1), 4), ("t2", at(2), 1)])
    assert latest_high(two) == 1
    assert previous_high(two) == 4
    assert has_task(two, "t1") is True
    assert has_task(two, "nope") is False


@pytest.mark.parametrize("clear_after", [0, -1])
def test_clear_after_below_one_is_rejected(clear_after: int) -> None:
    observation: Observation = known("t1", 1, 1)
    with pytest.raises(ValueError, match="clear_after"):
        advance_series(None, observation, key=KEY, clear_after=clear_after)


def test_folding_the_same_runs_twice_is_idempotent() -> None:
    runs = [("t1", at(1), 2), ("t2", at(2), 0), ("t3", at(3), 0), ("t4", at(4), 1)]
    once = fold(runs)
    assert fold(runs, start=once) == once


def test_shuffled_input_only_counts_runs_that_arrive_in_order() -> None:
    runs = [(f"t{i}", at(i), i % 3) for i in range(1, 9)]
    shuffled = runs[:]
    random.Random(7).shuffle(shuffled)
    history = fold(shuffled)
    assert history is not None
    kept = [p.task_id for p in history.points]
    arrived = [r[0] for r in shuffled]
    expected: list[str] = []
    newest = -1
    for task_id in arrived:
        day = int(task_id[1:])
        if day > newest:
            expected.append(task_id)
            newest = day
    assert kept == expected
    assert kept == sorted(kept, key=lambda t: int(t[1:]))
