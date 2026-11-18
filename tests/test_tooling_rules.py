"""RULE-07 / D-10: tooling gaps decided only with evidence, through the real reader and tables."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from guard_fakes import InMemoryGuardTools, run_item, world

from enji_guard_radar.guard import (
    AuditRun,
    GuardProject,
    GuardRepository,
    ProjectOk,
    ReportStatus,
    RepoRuns,
    RunStatus,
    fetch_project,
)
from enji_guard_radar.ledger import ToolingRecord
from enji_guard_radar.tooling_rules import ToolingEvaluation, evaluate_tooling

PROJECT = GuardProject(id="p1", name="Project A")
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
DEAD_CODE = "audit.dead-code"
UNIFICATION = "audit.implementation-unification"
SECURITY = "audit.security"
DEPENDENCY = "audit.dependency-hygiene"
DEFAULT_TYPES = (DEAD_CODE, UNIFICATION, SECURITY, DEPENDENCY)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def test_missing_tooling_runs_are_gaps_when_the_run_list_is_complete() -> None:
    ten_days_ago = _stamp(NOW - timedelta(days=10))
    responses = world(
        [("p1", "Project A")],
        {"p1": [("r1", "service-a")]},
        {
            "r1": [
                run_item("t1", "audit.tests", ten_days_ago),
                run_item("t2", SECURITY, ten_days_ago),
            ]
        },
        {},
    )
    result = fetch_project(
        InMemoryGuardTools(responses),
        PROJECT,
        skip_report=lambda *_: True,
    )
    assert isinstance(result, ProjectOk)
    assert result.repositories[0].runs_complete is True

    evaluation = evaluate_tooling(result, {}, audit_types=DEFAULT_TYPES, window_days=60, now=NOW)

    assert [gap.action_key for gap in evaluation.gaps] == [DEAD_CODE, DEPENDENCY, UNIFICATION]
    assert all(gap.last_completed_at is None and gap.age_days is None for gap in evaluation.gaps)
    assert len(evaluation.records) == 4
    assert evaluation.undecided == 0


# --- tables ------------------------------------------------------------------------------

WINDOW = timedelta(days=60)


def _run(
    action_key: str,
    completed: datetime | None,
    *,
    counts: bool = True,
    task_id: str | None = None,
) -> AuditRun:
    stamp = completed or NOW
    return AuditRun(
        task_id=task_id or f"{action_key}-{stamp.isoformat()}",
        action_key=action_key,
        status=RunStatus.COMPLETED if counts else RunStatus.UNKNOWN,
        status_raw="completed" if counts else "running",
        report_status=ReportStatus.READY if counts else ReportStatus.PENDING,
        report_status_raw="ready" if counts else "pending",
        created_at=stamp,
        linked_at=stamp,
        completed_at=completed,
    )


def _repo(
    repo_id: str = "r1",
    name: str = "service-a",
    *runs: AuditRun,
    complete: bool = True,
) -> RepoRuns:
    repository = GuardRepository(
        id=repo_id, project_id="p1", name=name, provider="git", connected=True
    )
    return RepoRuns(repository=repository, runs=runs, runs_complete=complete)


def _project(*repos: RepoRuns, name: str = "Project A") -> ProjectOk:
    return ProjectOk(project=GuardProject(id="p1", name=name), repositories=repos)


def _stored(
    action_key: str,
    *,
    observed: datetime,
    last: datetime | None = None,
    project_id: str = "p1",
    repo_id: str = "r1",
) -> dict[tuple[str, str, str], ToolingRecord]:
    record = ToolingRecord(
        project_id=project_id,
        repo_id=repo_id,
        action_key=action_key,
        observed_since=observed,
        last_completed_at=last,
    )
    return {record.key(): record}


def _evaluate(
    project: ProjectOk,
    stored: dict[tuple[str, str, str], ToolingRecord] | None = None,
    *,
    types: tuple[str, ...] = (SECURITY,),
    window_days: int = 60,
    now: datetime = NOW,
) -> ToolingEvaluation:
    return evaluate_tooling(
        project, stored or {}, audit_types=types, window_days=window_days, now=now
    )


@pytest.mark.parametrize(
    ("age", "window_days", "is_gap", "age_days"),
    [
        pytest.param(WINDOW - timedelta(hours=1), 60, False, None, id="just_inside"),
        pytest.param(WINDOW, 60, True, 60, id="exactly_window"),
        pytest.param(timedelta(days=7), 7, True, 7, id="window_7_edge"),
        pytest.param(timedelta(days=6, hours=23), 7, False, None, id="window_7_inside"),
        pytest.param(timedelta(days=364), 365, False, None, id="window_365_inside"),
        pytest.param(timedelta(days=365), 365, True, 365, id="window_365_edge"),
        pytest.param(timedelta(days=-2), 60, False, None, id="future_run_is_recent"),
    ],
)
def test_window_edge(age: timedelta, window_days: int, is_gap: bool, age_days: int | None) -> None:
    project = _project(_repo("r1", "service-a", _run(SECURITY, NOW - age)))
    evaluation = _evaluate(project, window_days=window_days)
    assert len(evaluation.gaps) == int(is_gap)
    if is_gap:
        assert evaluation.gaps[0].age_days == age_days
        assert evaluation.gaps[0].window_days == window_days
    assert evaluation.undecided == 0


@pytest.mark.parametrize(
    ("complete", "observed_days", "last_days", "outcome"),
    [
        pytest.param(False, 10, None, "undecided", id="incomplete_watched_10d_no_run"),
        pytest.param(False, 60, None, "gap", id="incomplete_watched_exactly_window"),
        pytest.param(True, 0, None, "gap", id="complete_watched_now"),
        pytest.param(False, 61, 70, "gap", id="incomplete_old_run_watched_61d"),
        pytest.param(False, 10, 70, "undecided", id="incomplete_old_run_watched_10d"),
        pytest.param(False, 10, 5, "none", id="incomplete_recent_run"),
    ],
)
def test_evidence_decides_whether_a_missing_run_is_a_gap(
    complete: bool, observed_days: int, last_days: int | None, outcome: str
) -> None:
    runs = () if last_days is None else (_run(SECURITY, NOW - timedelta(days=last_days)),)
    project = _project(_repo("r1", "service-a", *runs, complete=complete))
    stored = _stored(SECURITY, observed=NOW - timedelta(days=observed_days))
    evaluation = _evaluate(project, stored)
    assert len(evaluation.gaps) == int(outcome == "gap")
    assert evaluation.undecided == int(outcome == "undecided")
    assert len(evaluation.records) == 1


def test_evidence_logs_a_gap_and_one_undecided_line_per_project(
    caplog: pytest.LogCaptureFixture,
) -> None:
    project = _project(_repo("r1", "a", complete=False), _repo("r2", "b", complete=True))
    with caplog.at_level(logging.INFO):
        evaluation = _evaluate(project)
    assert (len(evaluation.gaps), evaluation.undecided) == (1, 1)
    assert "tooling gap: p1/r2/audit.security last=none" in caplog.text
    assert caplog.text.count("1 pair(s) undecided") == 1


def test_accumulation_keeps_the_stored_last_run_when_the_poll_shows_none() -> None:
    last = NOW - timedelta(days=20)
    observed = NOW - timedelta(days=90)
    evaluation = _evaluate(
        _project(_repo(complete=False)), _stored(SECURITY, observed=observed, last=last)
    )
    assert evaluation.gaps == ()
    [record] = evaluation.records
    assert (record.last_completed_at, record.observed_since) == (last, observed)


def test_accumulation_takes_a_newer_run_and_never_moves_back() -> None:
    stored_last = NOW - timedelta(days=20)
    observed = NOW - timedelta(days=90)
    stored = _stored(SECURITY, observed=observed, last=stored_last)
    newer = _evaluate(_project(_repo("r1", "a", _run(SECURITY, NOW - timedelta(days=3)))), stored)
    older = _evaluate(_project(_repo("r1", "a", _run(SECURITY, NOW - timedelta(days=40)))), stored)
    assert newer.records[0].last_completed_at == NOW - timedelta(days=3)
    assert older.records[0].last_completed_at == stored_last


def test_accumulation_sets_observed_since_to_now_for_a_new_pair() -> None:
    evaluation = _evaluate(_project(_repo()))
    assert evaluation.records[0].observed_since == NOW
    assert evaluation.records[0].last_completed_at is None


def test_newest_run_of_the_type_is_used_and_non_counting_runs_are_ignored() -> None:
    runs = (
        _run(SECURITY, NOW - timedelta(days=100)),
        _run(SECURITY, NOW - timedelta(days=30)),
        _run(SECURITY, NOW - timedelta(days=1), counts=False),
        _run(SECURITY, None, counts=False),
    )
    evaluation = _evaluate(_project(_repo("r1", "a", *runs)))
    assert evaluation.gaps == ()
    assert evaluation.records[0].last_completed_at == NOW - timedelta(days=30)


def test_newest_only_non_counting_runs_is_a_missing_run() -> None:
    runs = (
        _run(SECURITY, NOW - timedelta(days=1), counts=False),
        _run(SECURITY, None, counts=False),
    )
    evaluation = _evaluate(_project(_repo("r1", "a", *runs)))
    assert [gap.last_completed_at for gap in evaluation.gaps] == [None]


def test_configured_audit_types_limit_records_and_gaps() -> None:
    evaluation = _evaluate(_project(_repo()), types=(DEAD_CODE, SECURITY))
    assert [r.action_key for r in evaluation.records] == [DEAD_CODE, SECURITY]
    assert [g.action_key for g in evaluation.gaps] == [DEAD_CODE, SECURITY]


def test_configured_scope_drops_stale_and_foreign_records() -> None:
    observed = NOW - timedelta(days=5)
    stored = {
        **_stored(DEPENDENCY, observed=observed),
        **_stored(SECURITY, observed=observed, repo_id="gone"),
        **_stored(SECURITY, observed=observed, project_id="other"),
    }
    evaluation = _evaluate(_project(_repo()), stored, types=(SECURITY,))
    assert [r.key() for r in evaluation.records] == [("p1", "r1", SECURITY)]


def test_configured_project_without_repositories_has_nothing() -> None:
    evaluation = _evaluate(_project())
    assert (evaluation.records, evaluation.gaps, evaluation.undecided) == ((), (), 0)


def test_order_never_seen_first_then_oldest_then_names() -> None:
    project = _project(
        _repo("r2", "beta", _run(SECURITY, NOW - timedelta(days=100))),
        _repo("r1", "alpha", _run(SECURITY, NOW - timedelta(days=70))),
        _repo("r3", "zeta"),
        _repo("r0", "gamma"),
        name="Project A",
    )
    evaluation = _evaluate(project)
    assert [g.repo_name for g in evaluation.gaps] == ["gamma", "zeta", "beta", "alpha"]


def test_order_two_repositories_are_independent() -> None:
    project = _project(
        _repo("r1", "a", _run(SECURITY, NOW - timedelta(days=1))),
        _repo("r2", "b"),
    )
    assert [g.repo_id for g in _evaluate(project).gaps] == ["r2"]


def test_invalid_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _evaluate(_project(_repo()), now=datetime(2026, 11, 20, 12, 0))  # noqa: DTZ001


def test_invalid_window_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="window_days"):
        _evaluate(_project(_repo()), window_days=0)


def test_idempotent_second_poll_changes_nothing_but_a_newer_last_run() -> None:
    project = _project(_repo("r1", "a", _run(SECURITY, NOW - timedelta(days=3)), complete=False))
    first = _evaluate(project, types=(DEAD_CODE, SECURITY))
    assert _evaluate(project, types=(DEAD_CODE, SECURITY)) == first
    stored = {record.key(): record for record in first.records}
    assert _evaluate(project, stored, types=(DEAD_CODE, SECURITY)) == first
    later = _evaluate(
        _project(_repo("r1", "a", _run(SECURITY, NOW - timedelta(days=1)), complete=False)),
        stored,
        types=(DEAD_CODE, SECURITY),
    )
    assert later.records[1].last_completed_at == NOW - timedelta(days=1)
    assert later.records[0] == first.records[0]
