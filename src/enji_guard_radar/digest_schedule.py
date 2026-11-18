"""Is the weekly digest due on this run? (DIG-02, DIG-03; Phase 3 D-01 to D-03)

Pure: no I/O and no clock reads (callers pass `now`). The radar is a one-shot run on a timer,
so "weekly at a given time" is decided on every run from the clock, the planned moment the
digest was last handled for (sent or skipped) and whether this run's Guard poll worked.

The planned moment is a weekday and hour in UTC. The digest is due on the first run at or after
it. A digest not handled within `grace` of its planned moment is skipped for that week and not
made up later. While the poll failed a due digest is deferred, never sent. The caller owns the
INFO and WARNING log lines and the handled marker (written only after Telegram acknowledged).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

_DAYS_PER_WEEK = 7


@dataclass(frozen=True)
class NotDue:
    reason: str


@dataclass(frozen=True)
class Due:
    planned_for: datetime


@dataclass(frozen=True)
class Deferred:
    planned_for: datetime


@dataclass(frozen=True)
class Skipped:
    planned_for: datetime


DigestDecision = NotDue | Due | Deferred | Skipped


def planned_moment(now: datetime, *, weekday: int, hour_utc: int) -> datetime:
    """The latest instant at or before `now` on `weekday` (Monday 0) at `hour_utc`:00 UTC."""
    if now.tzinfo is None or now.utcoffset() is None:
        message = "now must be timezone-aware"
        raise ValueError(message)
    utc_now = now.astimezone(UTC)
    candidate = utc_now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    candidate -= timedelta(days=(candidate.weekday() - weekday) % _DAYS_PER_WEEK)
    if candidate > utc_now:
        candidate -= timedelta(days=_DAYS_PER_WEEK)
    return candidate


def decide_digest(
    now: datetime,
    *,
    handled_for: datetime | None,
    weekday: int,
    hour_utc: int,
    grace: timedelta,
    poll_ok: bool,
) -> DigestDecision:
    """Decide what this run does about the weekly digest.

    `handled_for` is the planned moment the digest was last sent or skipped for (None: never).
    With m the latest planned moment and elapsed = now - m:
    - `handled_for >= m`: NotDue (handled already; also a clock that moved backwards).
    - elapsed < grace: Due, or Deferred while the poll failed.
    - elapsed >= grace: Skipped, or NotDue when there is no history (a fresh install).
    """
    moment = planned_moment(now, weekday=weekday, hour_utc=hour_utc)
    if handled_for is not None and handled_for >= moment:
        logger.debug("digest for %s already handled (handled_for %s)", moment, handled_for)
        return NotDue(reason="handled")
    if now - moment < grace:
        if poll_ok:
            logger.debug("digest for %s is due", moment)
            return Due(planned_for=moment)
        logger.debug("digest for %s deferred: the Guard poll failed", moment)
        return Deferred(planned_for=moment)
    if handled_for is None:
        logger.debug("digest for %s outside its window and no history: not due", moment)
        return NotDue(reason="no_history")
    logger.debug("digest for %s outside its window: skipped", moment)
    return Skipped(planned_for=moment)
