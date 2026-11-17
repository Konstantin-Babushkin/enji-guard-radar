"""RULE-01: a project whose newest completed Guard run is too old is stale."""

import logging
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from enji_guard_radar.guard import ProjectData, ProjectUnknown
from enji_guard_radar.model import Alert, AlertKey, AlertKind, Severity

logger = logging.getLogger(__name__)

STALE_SEVERITY = Severity.MEDIUM


class StaleVerdict(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    NEVER_RAN = "never_ran"
    UNKNOWN = "unknown"


class StaleResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    project_name: str
    verdict: StaleVerdict
    threshold_days: int
    age_days: int | None
    newest_run_at: AwareDatetime | None
    repo_count: int
    reason: str | None
    # The newest run is later than `now` (clock skew): judged fresh, the caller warns.
    future_run: bool = False


def evaluate_stale(data: ProjectData, threshold_days: int, now: datetime) -> StaleResult:
    """Judge one project against the threshold.

    The boundary is `age >= threshold_days` as written in plan 01-02; REQUIREMENTS RULE-01 says
    "older than N days". The operator may switch to a strict comparison later.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        message = "now must be timezone-aware"
        raise ValueError(message)
    project = data.project
    common = {
        "project_id": project.id,
        "project_name": project.name,
        "threshold_days": threshold_days,
    }
    if isinstance(data, ProjectUnknown):
        logger.info("stale check of %s: unknown (%s)", project.id, data.reason)
        return StaleResult(
            **common,
            verdict=StaleVerdict.UNKNOWN,
            age_days=None,
            newest_run_at=None,
            repo_count=0,
            reason=data.reason,
        )
    repo_count = len(data.repositories)
    newest = data.newest_counting_run_at
    if newest is None:
        logger.info("stale check of %s: never ran (%d repositories)", project.id, repo_count)
        return StaleResult(
            **common,
            verdict=StaleVerdict.NEVER_RAN,
            age_days=None,
            newest_run_at=None,
            repo_count=repo_count,
            reason=None,
        )
    age = now - newest
    if age >= timedelta(days=threshold_days):
        verdict, age_days = StaleVerdict.STALE, age.days
        logger.info("stale check of %s: stale, %d days", project.id, age_days)
    else:
        verdict, age_days = StaleVerdict.FRESH, max(age.days, 0)
    return StaleResult(
        **common,
        verdict=verdict,
        age_days=age_days,
        newest_run_at=newest,
        repo_count=repo_count,
        reason=None,
        future_run=newest > now,
    )


def alert_for(result: StaleResult) -> Alert | None:
    """Unknown Guard data never produces an alert."""
    if result.verdict is StaleVerdict.STALE:
        kind, level = AlertKind.STALE, "stale"
    elif result.verdict is StaleVerdict.NEVER_RAN:
        kind, level = AlertKind.NEVER_RAN, "never_ran"
    else:
        return None
    return Alert(
        key=AlertKey(project_id=result.project_id, level=level),
        kind=kind,
        project_name=result.project_name,
        severity=STALE_SEVERITY,
        age_days=result.age_days,
        last_run_at=result.newest_run_at,
        repo_count=result.repo_count,
        count=None,
    )
