"""Table tests for the pure ledger: baseline, retry, resolve, freeze, count, serialization."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from enji_guard_radar.ledger import (
    ConditionRecord,
    EvaluationOutcome,
    ProjectEvaluation,
    ProjectRecord,
    State,
    StateFormatError,
    apply_delivery,
    deserialize_state,
    reconcile,
    serialize_state,
)
from enji_guard_radar.model import Alert, AlertKey, AlertKind, Severity

T0 = datetime(2026, 11, 16, 12, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
T2 = T0 + timedelta(days=2)


def key(project_id: str, level: str = "stale", repo_id: str = "*") -> AlertKey:
    return AlertKey(project_id=project_id, repo_id=repo_id, level=level)


def alert(
    project_id: str, *, age_days: int = 15, count: int | None = None, level: str = "stale"
) -> Alert:
    return Alert(
        key=key(project_id, level),
        kind=AlertKind.STALE,
        project_name=f"Name {project_id}",
        severity=Severity.MEDIUM,
        age_days=age_days,
        last_run_at=None,
        repo_count=1,
        count=count,
    )


def ev(
    project_id: str,
    *alerts: Alert,
    outcome: EvaluationOutcome = EvaluationOutcome.evaluated,
) -> ProjectEvaluation:
    return ProjectEvaluation(project_id=project_id, outcome=outcome, alerts=alerts)


def known_state(*conditions: ConditionRecord, projects: tuple[str, ...] = ("a",)) -> State:
    return State(
        projects=tuple(
            ProjectRecord(project_id=p, first_seen_at=T0, last_evaluated_at=T0) for p in projects
        ),
        conditions=conditions,
    )


def record(
    project_id: str,
    *,
    baseline: bool = False,
    alerted: bool = False,
    count: int | None = None,
) -> ConditionRecord:
    return ConditionRecord(
        key=key(project_id),
        first_seen_at=T0,
        last_seen_at=T0,
        baseline=baseline,
        alerted_at=T0 if alerted else None,
        count=count,
    )


def test_first_run_baseline_records_everything_and_sends_nothing() -> None:
    result = reconcile(State.empty(), [ev("a", alert("a")), ev("b")], T0)
    assert result.to_send == ()
    assert result.baselined == (alert("a"),)
    assert result.first_seen_projects == ("a", "b")
    assert [c.baseline for c in result.state.conditions] == [True]
    assert [p.project_id for p in result.state.projects] == ["a", "b"]


def test_first_seen_project_without_alerts_gets_a_project_record_only_baseline() -> None:
    result = reconcile(State.empty(), [ev("a")], T0)
    assert [p.project_id for p in result.state.projects] == ["a"]
    assert result.state.conditions == ()


def test_baseline_of_new_project_does_not_hide_new_condition_of_known_project() -> None:
    state = known_state()
    result = reconcile(state, [ev("a", alert("a")), ev("b", alert("b"))], T1)
    assert result.to_send == (alert("a"),)
    assert result.baselined == (alert("b"),)
    assert result.first_seen_projects == ("b",)


def test_result_is_the_same_for_reversed_evaluations() -> None:
    state = known_state(projects=("a", "c"))
    evaluations = [ev("a", alert("a")), ev("b", alert("b")), ev("c", alert("c"))]
    forward = reconcile(state, evaluations, T1)
    reversed_ = reconcile(state, list(reversed(evaluations)), T1)
    assert forward == reversed_
    assert [a.key.project_id for a in forward.to_send] == ["a", "c"]


@pytest.mark.parametrize("age_days", [15, 16])
def test_alerted_condition_is_not_planned_again_whatever_its_age(age_days: int) -> None:
    state = known_state(record("a", alerted=True))
    result = reconcile(state, [ev("a", alert("a", age_days=age_days))], T1)
    assert result.to_send == ()
    assert len(result.state.conditions) == 1
    assert result.state.conditions[0].alerted_at == T0


def test_unacknowledged_condition_is_retried() -> None:
    state = known_state(record("a"))
    result = reconcile(state, [ev("a", alert("a"))], T1)
    assert result.to_send == (alert("a"),)
    assert result.retried == (key("a"),)


def test_new_condition_of_known_project_is_planned_but_not_listed_as_retried() -> None:
    result = reconcile(known_state(), [ev("a", alert("a"))], T1)
    assert result.to_send == (alert("a"),)
    assert result.retried == ()


def test_baselined_condition_is_never_planned() -> None:
    state = known_state(record("a", baseline=True))
    result = reconcile(state, [ev("a", alert("a"))], T1)
    assert result.to_send == ()
    assert result.retried == ()


def test_condition_is_resolved_when_its_project_is_evaluated_without_it() -> None:
    state = known_state(record("a", alerted=True), record("b", alerted=True), projects=("a", "b"))
    result = reconcile(state, [ev("a")], T1)
    assert result.resolved == (key("a"),)
    assert [c.key.project_id for c in result.state.conditions] == ["b"]
    assert result.to_send == ()


@pytest.mark.parametrize("outcome", [EvaluationOutcome.unknown, EvaluationOutcome.skipped])
def test_records_of_unknown_or_skipped_projects_are_frozen(outcome: EvaluationOutcome) -> None:
    state = known_state(record("a", alerted=True))
    result = reconcile(state, [ev("a", outcome=outcome)], T1)
    assert serialize_state(result.state) == serialize_state(state)
    assert result.resolved == ()


def test_records_of_projects_missing_from_the_listing_are_frozen() -> None:
    state = known_state(record("a", alerted=True), record("b"), projects=("a", "b"))
    result = reconcile(state, [], T1)
    assert serialize_state(result.state) == serialize_state(state)
    assert result.resolved == ()


def test_unknown_first_seen_project_gets_no_project_record() -> None:
    result = reconcile(State.empty(), [ev("a", outcome=EvaluationOutcome.unknown)], T0)
    assert result.state == State.empty()
    assert result.first_seen_projects == ()


def test_run_without_conditions_plans_nothing_and_resolves_only_evaluated_projects() -> None:
    state = known_state(record("a"), record("b"), projects=("a", "b"))
    result = reconcile(state, [ev("a")], T1)
    assert result.to_send == ()
    assert result.resolved == (key("a"),)
    assert [c.key.project_id for c in result.state.conditions] == ["b"]


def test_reconcile_updates_last_evaluated_and_last_seen() -> None:
    state = known_state(record("a", alerted=True))
    result = reconcile(state, [ev("a", alert("a"))], T2)
    assert result.state.projects[0].last_evaluated_at == T2
    assert result.state.projects[0].first_seen_at == T0
    assert result.state.conditions[0].last_seen_at == T2
    assert result.state.conditions[0].first_seen_at == T0


def test_reconcile_rejects_a_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        reconcile(State.empty(), [], datetime(2026, 11, 16, 12))  # noqa: DTZ001


def test_apply_delivery_marks_only_the_given_keys() -> None:
    state = known_state(record("a"), record("b"), projects=("a", "b"))
    result = apply_delivery(state, [key("a"), key("zzz")], T1)
    by_project = {c.key.project_id: c for c in result.conditions}
    assert by_project["a"].alerted_at == T1
    assert by_project["b"].alerted_at is None
    assert len(result.conditions) == 2


def test_reconcile_never_sets_alerted_at() -> None:
    result = reconcile(known_state(), [ev("a", alert("a"))], T1)
    assert all(c.alerted_at is None for c in result.state.conditions)


def test_count_round_trips_and_does_not_rearm() -> None:
    plain = record("a")
    counted = record("b", count=2)
    state = known_state(plain, counted, projects=("a", "b"))
    assert deserialize_state(serialize_state(state)) == state

    for bad in (-1, "2", True):
        raw = json.loads(serialize_state(state))
        raw["conditions"][1]["count"] = bad
        with pytest.raises(StateFormatError):
            deserialize_state(json.dumps(raw).encode())

    alerted = known_state(record("a", alerted=True, count=2))
    again = reconcile(alerted, [ev("a", alert("a", count=3))], T1)
    assert again.to_send == ()
    assert again.state.conditions[0].count == 3

    unalerted = known_state(record("a", count=2))
    retry = reconcile(unalerted, [ev("a", alert("a", count=3))], T1)
    assert retry.to_send == (alert("a", count=3),)


def test_ids_with_separator_and_non_ascii_and_microseconds_round_trip() -> None:
    odd = AlertKey(project_id="проект|1", repo_id="репо|x", action_key="a|b", level="stale|ü")
    when = datetime(2026, 11, 16, 12, 0, 0, 123456, tzinfo=UTC)
    state = State(
        projects=(
            ProjectRecord(project_id="проект|1", first_seen_at=when, last_evaluated_at=when),
        ),
        conditions=(
            ConditionRecord(
                key=odd, first_seen_at=when, last_seen_at=when, baseline=False, alerted_at=when
            ),
        ),
    )
    restored = deserialize_state(serialize_state(state))
    assert restored == state
    assert restored.conditions[0].alerted_at == when
    assert serialize_state(restored) == serialize_state(state)


def test_serialization_is_normalized_and_ends_with_a_newline() -> None:
    forward = known_state(record("a"), record("b"), projects=("a", "b"))
    shuffled = State(
        projects=tuple(reversed(forward.projects)), conditions=tuple(reversed(forward.conditions))
    )
    assert serialize_state(forward) == serialize_state(shuffled)
    assert serialize_state(forward).endswith(b"}\n")


CANARY = "LEAK-CANARY"


def _with(**changes: object) -> bytes:
    raw: dict[str, object] = {"schema_version": 1, "projects": [], "conditions": []}
    raw.update(changes)
    return json.dumps(raw).encode()


@pytest.mark.parametrize(
    "data",
    [
        b"{",
        b"[]",
        _with(schema_version=2),
        _with(schema_version="1"),
        _with(schema_version=True),
        _with(**{CANARY: 1}),
        _with(
            conditions=[
                {
                    "key": {"project_id": CANARY, "level": "stale"},
                    "first_seen_at": "2026-11-16T12:00:00",
                    "last_seen_at": "2026-11-16T12:00:00Z",
                    "baseline": True,
                }
            ]
        ),
        b"\xff\xfe" + CANARY.encode(),
    ],
)
def test_deserialize_rejects_bad_input_without_echoing_content(data: bytes) -> None:
    with pytest.raises(StateFormatError) as info:
        deserialize_state(data)
    assert CANARY not in info.value.reason
    assert CANARY not in str(info.value)


def test_unsupported_version_reason_names_the_version_or_its_type() -> None:
    with pytest.raises(
        StateFormatError, match=r"unsupported schema_version 2 \(this radar reads 1\)"
    ):
        deserialize_state(_with(schema_version=2))
    with pytest.raises(StateFormatError, match=r"unsupported schema_version str"):
        deserialize_state(_with(schema_version="1"))
