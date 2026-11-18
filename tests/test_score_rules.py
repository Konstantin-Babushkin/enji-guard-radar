"""Score rules (RULE-05, RULE-06): the tracer through the real reader, then the score tables."""

from datetime import UTC, datetime, timedelta

from guard_fakes import InMemoryGuardTools, report_page, run_item, world

from enji_guard_radar.config import AI_READINESS_ACTION_KEY, TESTS_ACTION_KEY
from enji_guard_radar.guard import GuardProject, ProjectOk, fetch_project
from enji_guard_radar.score_rules import evaluate_scores

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
