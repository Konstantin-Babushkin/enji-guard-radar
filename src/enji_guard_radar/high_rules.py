"""RULE-02 / RULE-03: one (repo, audit type) High series becomes one counts-only alert.

Pure: the series facts and the clock come in as arguments (AGENTS.md). Age and count never
change the alert key, so an aged series is never a new alert (ALERT-01).
"""

import logging
from collections.abc import Collection
from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from enji_guard_radar.guard_report import OVERVIEW_ACTION_KEYS
from enji_guard_radar.model import Alert, AlertKey, AlertKind, Severity

logger = logging.getLogger(__name__)

HIGH_LEVEL = "high"


class HighSeries(BaseModel):
    """Facts about one open High series; no finding titles, ids, paths or evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    project_name: str
    repo_id: str
    repo_name: str
    action_key: str
    high_now: StrictInt = Field(ge=0)
    previous_high: StrictInt | None = Field(ge=0)
    # completed_at of the first High run the radar observed for the series (D-02).
    opened_at: AwareDatetime
    repo_count: StrictInt = Field(ge=0)


def in_scope(action_key: str, audit_types: Collection[str] | None) -> bool:
    """RULE-03 scope: the overview audit never has High findings; None tracks every other type."""
    if action_key in OVERVIEW_ACTION_KEYS:
        return False
    return audit_types is None or action_key in audit_types


def high_alert(series: HighSeries, *, aged_after_days: int, now: datetime) -> Alert:
    if now.tzinfo is None or now.utcoffset() is None:
        message = "now must be timezone-aware"
        raise ValueError(message)
    age = now - series.opened_at
    aged = age > timedelta(days=aged_after_days)
    age_days = max(age.days, 0)
    if aged:
        logger.info(
            "high series %s/%s/%s is long-standing: %d days (threshold %d)",
            series.project_id,
            series.repo_id,
            series.action_key,
            age_days,
            aged_after_days,
        )
    return Alert(
        key=AlertKey(
            project_id=series.project_id,
            repo_id=series.repo_id,
            action_key=series.action_key,
            level=HIGH_LEVEL,
        ),
        kind=AlertKind.HIGH_FINDINGS,
        project_name=series.project_name,
        severity=Severity.HIGH,
        age_days=age_days,
        last_run_at=None,
        repo_count=series.repo_count,
        count=series.high_now,
        repo_name=series.repo_name,
        previous_count=series.previous_high,
        seen_since=series.opened_at,
        aged=aged,
    )
