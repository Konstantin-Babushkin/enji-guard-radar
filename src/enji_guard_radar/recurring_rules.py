"""RULE-08: a High series that has not reached zero across N consecutive completed runs.

Pure: reads the stored High history, which counts runs by Guard task_id (D-09), never by poll.
"""

import logging

from enji_guard_radar.ledger import SeriesHistory

logger = logging.getLogger(__name__)

MIN_AFTER_RUNS = 2


def trailing_high_runs(history: SeriesHistory | None) -> int:
    """How many of the newest stored runs, in a row, have High findings."""
    if history is None:
        return 0
    count = 0
    for point in reversed(history.points):
        if point.high <= 0:
            break
        count += 1
    return count


def is_recurring(history: SeriesHistory | None, *, after_runs: int) -> bool:
    """An open series whose last `after_runs` stored runs all have High findings."""
    if after_runs < MIN_AFTER_RUNS:
        message = f"after_runs must be at least {MIN_AFTER_RUNS}"
        raise ValueError(message)
    if history is None or not history.open:
        return False
    count = trailing_high_runs(history)
    logger.debug(
        "recurring check %s/%s/%s: %d trailing High runs, need %d",
        history.project_id,
        history.repo_id,
        history.action_key,
        count,
        after_runs,
    )
    return count >= after_runs
