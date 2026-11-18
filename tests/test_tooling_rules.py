"""RULE-07 / D-10: tooling gaps decided only with evidence, through the real reader and tables."""

from datetime import UTC, datetime, timedelta

from guard_fakes import InMemoryGuardTools, run_item, world

from enji_guard_radar.guard import GuardProject, ProjectOk, fetch_project
from enji_guard_radar.tooling_rules import evaluate_tooling

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
