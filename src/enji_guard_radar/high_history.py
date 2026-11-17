"""Pure fold of completed Guard runs into per-(project, repo, audit type) High history.

Runs are counted by Guard task_id, never by poll (D-01): the same run seen twice changes
nothing, and an unknown observation never opens, closes or changes a series (RULE-09).
"""

import logging
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from enji_guard_radar.ledger import RunPoint, SeriesHistory, SeriesKey

logger = logging.getLogger(__name__)

# Bounds the state size only. Must stay at least the clear-after M (config allows up to 10).
HISTORY_WINDOW = 20


class Known(BaseModel):
    """A completed run whose report was read: its High count is a fact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    completed_at: AwareDatetime
    high: StrictInt = Field(ge=0)


class Unknown(BaseModel):
    """The newest report could not be read: nothing is learned, nothing changes."""

    model_config = ConfigDict(frozen=True, extra="forbid")


Observation = Known | Unknown


def _order(task_id: str, completed_at: datetime) -> tuple[datetime, str]:
    return (completed_at, task_id)


def advance_series(
    history: SeriesHistory | None,
    observation: Observation,
    *,
    key: SeriesKey,
    clear_after: int,
) -> SeriesHistory | None:
    """Fold one observation into a series. An episode opens at the first run with High > 0
    and closes after `clear_after` consecutive completed runs with zero High."""
    if clear_after < 1:
        message = "clear_after must be at least 1"
        raise ValueError(message)
    if isinstance(observation, Unknown):
        return history
    if history is not None and has_task(history, observation.task_id):
        return history
    if history is not None and history.points:
        newest = history.points[-1]
        if _order(observation.task_id, observation.completed_at) < _order(
            newest.task_id, newest.completed_at
        ):
            logger.warning(
                "late run ignored for %s: task_id=%s", "|".join(key), observation.task_id
            )
            return history

    current = history or SeriesHistory(project_id=key[0], repo_id=key[1], action_key=key[2])
    is_open, opened_at, clean_runs = current.open, current.opened_at, current.clean_runs
    if observation.high > 0:
        if not is_open:
            is_open, opened_at = True, observation.completed_at
            logger.debug("episode opened for %s: task_id=%s", "|".join(key), observation.task_id)
        clean_runs = 0
    elif is_open:
        clean_runs += 1
        if clean_runs >= clear_after:
            is_open, opened_at, clean_runs = False, None, 0
            logger.debug("episode closed for %s: task_id=%s", "|".join(key), observation.task_id)

    point = RunPoint(
        task_id=observation.task_id, completed_at=observation.completed_at, high=observation.high
    )
    return current.model_copy(
        update={
            "points": (*current.points, point)[-HISTORY_WINDOW:],
            "open": is_open,
            "opened_at": opened_at,
            "clean_runs": clean_runs,
        }
    )


def latest_high(history: SeriesHistory | None) -> int | None:
    """High count of the newest stored run, or None without history."""
    if history is None or not history.points:
        return None
    return history.points[-1].high


def previous_high(history: SeriesHistory | None) -> int | None:
    """High count of the second-newest stored run, or None."""
    if history is None or len(history.points) < 2:
        return None
    return history.points[-2].high


def has_task(history: SeriesHistory | None, task_id: str) -> bool:
    return history is not None and any(p.task_id == task_id for p in history.points)
