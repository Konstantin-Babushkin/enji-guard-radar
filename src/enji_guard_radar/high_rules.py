"""RULE-02 / RULE-03: one (repo, audit type) High series becomes one counts-only alert.

Pure: the series facts and the clock come in as arguments (AGENTS.md). Age and count never
change the alert key, so an aged series is never a new alert (ALERT-01).
"""

import logging
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from enji_guard_radar.guard import ProjectOk, SkipReport, newest_counting_runs_by_action_key
from enji_guard_radar.guard_report import OVERVIEW_ACTION_KEYS, AuditReport
from enji_guard_radar.high_history import (
    Known,
    Observation,
    Unknown,
    advance_series,
    has_task,
    latest_high,
    previous_high,
)
from enji_guard_radar.ledger import ScoreRecord, SeriesHistory, SeriesKey
from enji_guard_radar.model import Alert, AlertKey, AlertKind, Severity
from enji_guard_radar.score_rules import SCORE_ACTION_KEYS, score_known

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


def skip_report_for(
    project_id: str,
    histories: Mapping[SeriesKey, SeriesHistory],
    audit_types: Collection[str] | None,
    scores: Mapping[SeriesKey, ScoreRecord] | None = None,
) -> SkipReport:
    """Reports that need no read: untracked audit types and runs already counted.

    RULE-09 counts by task_id, so only a task_id already in the stored history is skipped; a run
    whose report failed or was never read is not in history and is requested again. With `scores`
    given, a score audit type is also read until its task_id is recorded as a score, whatever the
    High scope says; without it the rule is the High one only.
    """

    def skip(repo_id: str, action_key: str, task_id: str) -> bool:
        key = (project_id, repo_id, action_key)
        if (
            scores is not None
            and action_key in SCORE_ACTION_KEYS
            and not score_known(scores.get(key), task_id)
        ):
            return False
        if not in_scope(action_key, audit_types):
            return True
        return has_task(histories.get(key), task_id)

    return skip


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


class HighEvaluation(BaseModel):
    """One project's High histories after this poll, the alerts of its open series and counters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    histories: tuple[SeriesHistory, ...]
    alerts: tuple[Alert, ...]
    observed: StrictInt = Field(ge=0)
    unknown_reports: StrictInt = Field(ge=0)
    dropped: tuple[SeriesKey, ...]


def evaluate_high(
    project: ProjectOk,
    histories: Mapping[SeriesKey, SeriesHistory],
    *,
    clear_after: int,
    audit_types: Collection[str] | None,
    aged_after_days: int,
    now: datetime,
) -> HighEvaluation:
    """Fold the project's newest completed runs into its stored histories; alert every open one.

    Unknown reports and runs missing from this poll change nothing (RULE-09). Every open series
    is emitted on every evaluated poll, with its stored counts, so reconcile neither resolves nor
    re-alerts it; whether to send is reconcile's decision, never made here.
    """
    pid = project.project.id
    mine = {key: history for key, history in histories.items() if key[0] == pid}
    repo_names = {repo.repository.id: repo.repository.name for repo in project.repositories}

    dropped = sorted(
        key for key in mine if key[1] not in repo_names or not in_scope(key[2], audit_types)
    )
    for key in dropped:
        logger.info("high series dropped (repository removed or out of scope): %s", "|".join(key))
        del mine[key]

    observed = unknown_reports = 0
    for repo in project.repositories:
        reports = {report.task_id: report for report in repo.reports}
        for run in newest_counting_runs_by_action_key(repo.runs):
            if not in_scope(run.action_key, audit_types):
                continue
            key = (pid, repo.repository.id, run.action_key)
            if has_task(mine.get(key), run.task_id):
                continue
            report = reports.get(run.task_id)
            observation: Observation
            if (
                isinstance(report, AuditReport)
                and report.layout == "scored"
                and run.completed_at is not None
            ):
                observation = Known(
                    task_id=run.task_id, completed_at=run.completed_at, high=report.high_count
                )
                observed += 1
            else:
                observation = Unknown()
                unknown_reports += 1
                logger.debug("report of run %s is unknown; series %s unchanged", run.task_id, key)
            advanced = advance_series(mine.get(key), observation, key=key, clear_after=clear_after)
            if advanced is not None:
                mine[key] = advanced

    alerts: list[Alert] = []
    for key in sorted(mine):
        history = mine[key]
        high_now = latest_high(history)
        if not history.open or history.opened_at is None or high_now is None:
            continue
        series = HighSeries(
            project_id=pid,
            project_name=project.project.name,
            repo_id=key[1],
            repo_name=repo_names[key[1]],
            action_key=key[2],
            high_now=high_now,
            previous_high=previous_high(history),
            opened_at=history.opened_at,
            repo_count=len(project.repositories),
        )
        alerts.append(high_alert(series, aged_after_days=aged_after_days, now=now))
    return HighEvaluation(
        histories=tuple(mine[key] for key in sorted(mine)),
        alerts=tuple(alerts),
        observed=observed,
        unknown_reports=unknown_reports,
        dropped=tuple(dropped),
    )
