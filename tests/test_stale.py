from datetime import UTC, datetime, timedelta

import pytest

from enji_guard_radar.guard import (
    AuditRun,
    GuardProject,
    GuardRepository,
    ProjectOk,
    ProjectUnknown,
    ReportStatus,
    RepoRuns,
    RunStatus,
)
from enji_guard_radar.stale import StaleVerdict, alert_for, evaluate_stale

NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
PROJECT = GuardProject(id="p1", name="Project One")


def run(completed_at: datetime | None, *, ready: bool = True) -> AuditRun:
    return AuditRun(
        task_id="t",
        action_key="security",
        status=RunStatus.COMPLETED if completed_at is not None else RunStatus.UNKNOWN,
        status_raw="completed",
        report_status=ReportStatus.READY if ready else ReportStatus.PENDING,
        report_status_raw="ready",
        created_at=NOW - timedelta(days=100),
        linked_at=NOW - timedelta(days=100),
        completed_at=completed_at,
    )


def repo(repo_id: str, *runs: AuditRun) -> RepoRuns:
    return RepoRuns(
        repository=GuardRepository(
            id=repo_id, project_id="p1", name=repo_id, provider="github", connected=True
        ),
        runs=runs,
    )


def project(*repos: RepoRuns) -> ProjectOk:
    return ProjectOk(project=PROJECT, repositories=repos)


def ago(delta: timedelta) -> ProjectOk:
    return project(repo("r1", run(NOW - delta)))


@pytest.mark.parametrize(
    ("delta", "threshold", "verdict", "age"),
    [
        (timedelta(days=13, hours=23, minutes=59, seconds=59), 14, StaleVerdict.FRESH, 13),
        (timedelta(days=14), 14, StaleVerdict.STALE, 14),
        (timedelta(days=14, seconds=1), 14, StaleVerdict.STALE, 14),
        (timedelta(days=20), 30, StaleVerdict.FRESH, 20),
        (timedelta(days=20), 7, StaleVerdict.STALE, 20),
        (timedelta(0), 1, StaleVerdict.FRESH, 0),
        (timedelta(days=1), 1, StaleVerdict.STALE, 1),
    ],
)
def test_boundary_of_the_threshold(
    delta: timedelta, threshold: int, verdict: StaleVerdict, age: int
) -> None:
    result = evaluate_stale(ago(delta), threshold, NOW)
    assert (result.verdict, result.age_days) == (verdict, age)
    assert result.threshold_days == threshold
    assert not result.future_run


def test_future_run_is_fresh_with_age_zero_and_flagged() -> None:
    result = evaluate_stale(ago(timedelta(hours=-1)), 14, NOW)
    assert result.verdict is StaleVerdict.FRESH
    assert result.age_days == 0
    assert result.future_run
    assert alert_for(result) is None


def test_never_ran_without_repositories() -> None:
    result = evaluate_stale(project(), 14, NOW)
    assert result.verdict is StaleVerdict.NEVER_RAN
    assert result.repo_count == 0
    alert = alert_for(result)
    assert alert is not None
    assert alert.key.level == "never_ran"


def test_never_ran_with_only_non_counting_runs() -> None:
    data = project(
        repo("r1", run(NOW - timedelta(days=90), ready=False)),
        repo("r2", run(None)),
    )
    result = evaluate_stale(data, 14, NOW)
    assert result.verdict is StaleVerdict.NEVER_RAN
    assert result.repo_count == 2
    assert result.age_days is None


def test_unknown_data_is_unknown_and_never_alerts() -> None:
    result = evaluate_stale(ProjectUnknown(project=PROJECT, reason="outage"), 14, NOW)
    assert result.verdict is StaleVerdict.UNKNOWN
    assert result.reason == "outage"
    assert alert_for(result) is None


def test_newest_run_is_the_max_over_all_repositories() -> None:
    newest = NOW - timedelta(days=3)
    data = project(
        repo("r1", run(NOW - timedelta(days=30))),
        repo("r2", run(newest), run(NOW - timedelta(days=50))),
    )
    result = evaluate_stale(data, 14, NOW)
    assert result.newest_run_at == newest
    assert result.verdict is StaleVerdict.FRESH


def test_identical_newest_instants_give_that_instant() -> None:
    instant = NOW - timedelta(days=20)
    data = project(repo("r1", run(instant)), repo("r2", run(instant)))
    result = evaluate_stale(data, 14, NOW)
    assert result.newest_run_at == instant
    assert result.verdict is StaleVerdict.STALE


def test_stale_alert_carries_age_and_last_run() -> None:
    alert = alert_for(evaluate_stale(ago(timedelta(days=27)), 14, NOW))
    assert alert is not None
    assert alert.age_days == 27
    assert alert.last_run_at == NOW - timedelta(days=27)


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_stale(ago(timedelta(days=1)), 14, NOW.replace(tzinfo=None))
