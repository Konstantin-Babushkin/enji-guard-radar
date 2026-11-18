"""Score rules (RULE-05, RULE-06): the tracer through the real reader, then the score tables."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from guard_fakes import InMemoryGuardTools, report_page, run_item, world

from enji_guard_radar.config import AI_READINESS_ACTION_KEY, TESTS_ACTION_KEY
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
from enji_guard_radar.guard_report import AuditReport, ReportUnknown
from enji_guard_radar.ledger import ScoreRecord, SeriesKey
from enji_guard_radar.score_rules import evaluate_scores, score_known

BASE = datetime(2026, 11, 2, 10, tzinfo=UTC)


def stamp(day: int) -> str:
    return (BASE + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def test_low_ai_readiness_score_is_flagged_and_kept_when_the_report_is_skipped() -> None:
    responses = world(
        [("p1", "Proj")],
        {"p1": [("r1", "acme/svc")]},
        {
            "r1": [
                run_item("t-1", AI_READINESS_ACTION_KEY, stamp(1)),
                run_item("t-2", TESTS_ACTION_KEY, stamp(2)),
            ]
        },
        {
            "t-1": report_page("t-1", high=0, score=40),
            "t-2": report_page("t-2", high=0, score=90),
        },
    )
    guard_project = GuardProject(id="p1", name="Proj")
    thresholds = {AI_READINESS_ACTION_KEY: 50, TESTS_ACTION_KEY: 50}

    first = fetch_project(InMemoryGuardTools(responses), guard_project)
    assert isinstance(first, ProjectOk)
    evaluated = evaluate_scores(first, {}, thresholds=thresholds)

    assert len(evaluated.records) == 2
    assert evaluated.observed == 2
    assert evaluated.unknown_reports == 0
    assert len(evaluated.low) == 1
    low = evaluated.low[0]
    assert low.action_key == AI_READINESS_ACTION_KEY
    assert (low.score, low.threshold) == (40, 50)
    assert low.completed_at == BASE + timedelta(days=1)
    assert (low.repo_name, low.project_name) == ("acme/svc", "Proj")

    # Next poll does not read t-1's report: its record, and so the low score, stay.
    second = fetch_project(
        InMemoryGuardTools(responses),
        guard_project,
        skip_report=lambda _repo, _action, task_id: task_id == "t-1",
    )
    assert isinstance(second, ProjectOk)
    again = evaluate_scores(
        second, {record.key(): record for record in evaluated.records}, thresholds=thresholds
    )
    assert again.low == evaluated.low
    assert again.records == evaluated.records
    assert again.observed == 1


# ---- tables over ProjectOk values built from the Guard models --------------------------------

AI = AI_READINESS_ACTION_KEY
TESTS = TESTS_ACTION_KEY
PID = "p1"


def at(day: int) -> datetime:
    return BASE + timedelta(days=day)


def run(
    task_id: str,
    action_key: str = AI,
    day: int = 1,
    *,
    completed: bool = True,
) -> AuditRun:
    when = at(day)
    return AuditRun(
        task_id=task_id,
        action_key=action_key,
        status=RunStatus.COMPLETED if completed else RunStatus.UNKNOWN,
        status_raw="completed" if completed else "running",
        report_status=ReportStatus.READY if completed else ReportStatus.PENDING,
        report_status_raw="ready" if completed else "pending",
        created_at=when,
        linked_at=when,
        completed_at=when if completed else None,
    )


def scored(task_id: str, score: int, action_key: str = AI) -> AuditReport:
    return AuditReport(
        task_id=task_id,
        action_key=action_key,
        layout="scored",
        score=score,
        findings=(),
        content_version="v1",
    )


def unreadable(task_id: str, action_key: str = AI) -> ReportUnknown:
    return ReportUnknown(task_id=task_id, action_key=action_key, reason="bad page")


def no_score(task_id: str, action_key: str = AI) -> AuditReport:
    return AuditReport(
        task_id=task_id,
        action_key=action_key,
        layout="overview",
        score=None,
        findings=(),
        content_version="v1",
    )


def repo_runs(
    repo_id: str, runs: list[AuditRun], reports: list[AuditReport | ReportUnknown]
) -> RepoRuns:
    return RepoRuns(
        repository=GuardRepository(
            id=repo_id, project_id=PID, name=f"acme/{repo_id}", provider="github", connected=True
        ),
        runs=tuple(runs),
        reports=tuple(reports),
    )


def project_of(*repos: RepoRuns, name: str = "Proj") -> ProjectOk:
    return ProjectOk(project=GuardProject(id=PID, name=name), repositories=repos)


def record(
    task_id: str,
    score: int,
    *,
    day: int = 1,
    action_key: str = AI,
    repo_id: str = "r1",
    project_id: str = PID,
) -> ScoreRecord:
    return ScoreRecord(
        project_id=project_id,
        repo_id=repo_id,
        action_key=action_key,
        task_id=task_id,
        completed_at=at(day),
        score=score,
    )


def keyed(*records: ScoreRecord) -> dict[SeriesKey, ScoreRecord]:
    return {item.key(): item for item in records}


def one_run(score: int, action_key: str = AI) -> ProjectOk:
    return project_of(repo_runs("r1", [run("t-1", action_key)], [scored("t-1", score, action_key)]))


@pytest.mark.parametrize(
    ("score", "threshold", "flagged"),
    [
        (49, 50, True),
        (50, 50, False),
        (51, 50, False),
        (0, 0, False),
        (0, 1, True),
        (99, 100, True),
        (100, 100, False),
        (0, 100, True),
    ],
)
def test_boundary_score_below_threshold_only(score: int, threshold: int, flagged: bool) -> None:
    result = evaluate_scores(one_run(score), {}, thresholds={AI: threshold})
    assert len(result.low) == (1 if flagged else 0)
    assert [item.score for item in result.records] == [score]
    for item in result.low:
        assert (item.score, item.threshold) == (score, threshold)


def test_boundary_each_audit_type_uses_its_own_threshold() -> None:
    project = project_of(
        repo_runs(
            "r1",
            [run("t-1", AI), run("t-2", TESTS)],
            [scored("t-1", 59, AI), scored("t-2", 45, TESTS)],
        )
    )
    result = evaluate_scores(project, {}, thresholds={AI: 60, TESTS: 30})
    assert [(item.action_key, item.score) for item in result.low] == [(AI, 59)]


def test_off_without_thresholds_flags_nothing_but_keeps_records() -> None:
    project = project_of(
        repo_runs(
            "r1",
            [run("t-1", AI), run("t-2", TESTS)],
            [scored("t-1", 0, AI), scored("t-2", 0, TESTS)],
        )
    )
    result = evaluate_scores(project, {}, thresholds={})
    assert result.low == ()
    assert [(r.action_key, r.score) for r in result.records] == [(AI, 0), (TESTS, 0)]


def test_off_threshold_for_one_type_does_not_switch_the_other_on() -> None:
    project = project_of(
        repo_runs(
            "r1",
            [run("t-1", AI), run("t-2", TESTS)],
            [scored("t-1", 10, AI), scored("t-2", 10, TESTS)],
        )
    )
    result = evaluate_scores(project, {}, thresholds={TESTS: 50})
    assert [item.action_key for item in result.low] == [TESTS]


@pytest.mark.parametrize(
    "report",
    [unreadable("t-2"), no_score("t-2")],
    ids=["report-unknown", "no-score"],
)
def test_unknown_report_neither_flags_nor_clears(report: AuditReport | ReportUnknown) -> None:
    project = project_of(repo_runs("r1", [run("t-1", day=1), run("t-2", day=2)], [report]))
    low_record = record("t-1", 40)
    result = evaluate_scores(project, keyed(low_record), thresholds={AI: 50})
    assert result.records == (low_record,)
    assert [item.score for item in result.low] == [40]
    assert (result.observed, result.unknown_reports) == (0, 1)

    high_record = record("t-1", 80)
    result = evaluate_scores(project, keyed(high_record), thresholds={AI: 50})
    assert result.records == (high_record,)
    assert result.low == ()


@pytest.mark.parametrize(
    "report",
    [unreadable("t-1"), no_score("t-1")],
    ids=["report-unknown", "no-score"],
)
def test_unknown_report_without_stored_record_gives_nothing(
    report: AuditReport | ReportUnknown,
) -> None:
    project = project_of(repo_runs("r1", [run("t-1")], [report]))
    result = evaluate_scores(project, {}, thresholds={AI: 100})
    assert (result.records, result.low) == ((), ())
    assert result.unknown_reports == 1


def test_unknown_report_missing_from_the_poll_counts_nowhere() -> None:
    project = project_of(repo_runs("r1", [run("t-2", day=2)], []))
    stored = record("t-1", 40)
    result = evaluate_scores(project, keyed(stored), thresholds={AI: 50})
    assert result.records == (stored,)
    assert len(result.low) == 1
    assert (result.observed, result.unknown_reports) == (0, 0)


def test_newest_scored_run_clears_a_low_score() -> None:
    project = project_of(repo_runs("r1", [run("t-2", day=2)], [scored("t-2", 70)]))
    result = evaluate_scores(project, keyed(record("t-1", 40)), thresholds={AI: 50})
    assert [(r.task_id, r.score) for r in result.records] == [("t-2", 70)]
    assert result.low == ()


def test_newest_scored_run_raises_a_low_score() -> None:
    project = project_of(repo_runs("r1", [run("t-2", day=2)], [scored("t-2", 40)]))
    result = evaluate_scores(project, keyed(record("t-1", 70)), thresholds={AI: 50})
    assert [item.score for item in result.low] == [40]
    assert result.low[0].completed_at == at(2)


def test_newest_run_older_than_the_stored_one_is_ignored() -> None:
    project = project_of(repo_runs("r1", [run("t-1", day=1)], [scored("t-1", 10)]))
    stored = record("t-9", 80, day=5)
    result = evaluate_scores(project, keyed(stored), thresholds={AI: 50})
    assert result.records == (stored,)
    assert result.low == ()


def test_newest_same_task_id_twice_changes_nothing() -> None:
    project = project_of(repo_runs("r1", [run("t-1")], [scored("t-1", 40)]))
    first = evaluate_scores(project, {}, thresholds={AI: 50})
    second = evaluate_scores(project, keyed(*first.records), thresholds={AI: 50})
    assert second.records == first.records
    assert second.low == first.low


def test_newest_of_two_runs_of_one_type_is_the_later_one() -> None:
    project = project_of(
        repo_runs(
            "r1",
            [run("t-1", day=1), run("t-2", day=3)],
            [scored("t-1", 10), scored("t-2", 90)],
        )
    )
    result = evaluate_scores(project, {}, thresholds={AI: 50})
    assert [(r.task_id, r.score) for r in result.records] == [("t-2", 90)]
    assert result.low == ()


def test_newest_run_that_is_not_completed_is_ignored() -> None:
    project = project_of(
        repo_runs(
            "r1",
            [run("t-1", day=1), run("t-2", day=2, completed=False)],
            [scored("t-1", 40), scored("t-2", 90)],
        )
    )
    result = evaluate_scores(project, {}, thresholds={AI: 50})
    assert [(r.task_id, r.score) for r in result.records] == [("t-1", 40)]
    assert len(result.low) == 1


def test_scope_drops_removed_repositories_other_projects_and_other_audit_types() -> None:
    project = project_of(repo_runs("r1", [run("t-1")], [scored("t-1", 80)]))
    stored = keyed(
        record("t-0", 20, repo_id="gone"),
        record("t-0", 20, project_id="other"),
        record("t-0", 20, action_key="audit.security"),
        record("t-0", 20, repo_id="r1", day=0),
    )
    result = evaluate_scores(project, stored, thresholds={AI: 50})
    assert [(r.repo_id, r.task_id, r.score) for r in result.records] == [("r1", "t-1", 80)]
    assert result.low == ()


def test_scope_project_without_repositories_has_no_records() -> None:
    result = evaluate_scores(project_of(), keyed(record("t-0", 20)), thresholds={AI: 50})
    assert (result.records, result.low) == ((), ())


def test_scope_run_of_another_audit_type_is_not_a_score() -> None:
    project = project_of(
        repo_runs("r1", [run("t-1", "audit.security")], [scored("t-1", 5, "audit.security")])
    )
    result = evaluate_scores(project, {}, thresholds={AI: 50})
    assert (result.records, result.observed) == ((), 0)


def test_order_oldest_run_first_then_lower_score_then_name_then_ids() -> None:
    project = project_of(
        repo_runs("r3", [run("t-3", AI, 5)], [scored("t-3", 30, AI)]),
        repo_runs("r2", [run("t-2", AI, 2)], [scored("t-2", 30, AI)]),
        repo_runs(
            "r1",
            [run("t-1", AI, 2), run("t-4", TESTS, 2)],
            [scored("t-1", 20, AI), scored("t-4", 20, TESTS)],
        ),
        name="Zed",
    )
    result = evaluate_scores(project, {}, thresholds={AI: 50, TESTS: 50})
    assert [(i.repo_id, i.action_key) for i in result.low] == [
        ("r1", AI),
        ("r1", TESTS),
        ("r2", AI),
        ("r3", AI),
    ]
    assert [r.key() for r in result.records] == sorted(r.key() for r in result.records)


def test_order_scores_name_ties_by_repository_id_not_input_order() -> None:
    project = project_of(
        repo_runs("rb", [run("t-2")], [scored("t-2", 10)]),
        repo_runs("ra", [run("t-1")], [scored("t-1", 10)]),
    )
    result = evaluate_scores(project, {}, thresholds={AI: 50})
    assert [item.repo_id for item in result.low] == ["ra", "rb"]


def test_invalid_threshold_key_raises() -> None:
    with pytest.raises(ValueError, match=r"audit\.security"):
        evaluate_scores(one_run(10), {}, thresholds={"audit.security": 50})


@pytest.mark.parametrize("score", [0, 100])
def test_invalid_free_score_edges_are_accepted(score: int) -> None:
    result = evaluate_scores(one_run(score), {}, thresholds={AI: 50})
    assert [r.score for r in result.records] == [score]


def test_score_known_matches_only_the_stored_task() -> None:
    stored = record("t-1", 40)
    assert score_known(stored, "t-1") is True
    assert score_known(stored, "t-2") is False
    assert score_known(None, "t-1") is False


def test_low_score_log_names_ids_only(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.score_rules"):
        evaluate_scores(one_run(40), {}, thresholds={AI: 50})
    text = "\n".join(caplog.messages)
    assert f"score below threshold: {PID}/r1/{AI} 40 < 50" in text
    assert "Proj" not in text
    assert "acme" not in text
