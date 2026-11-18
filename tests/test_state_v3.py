"""State schema 3: a Phase 2 file upgrades through a live run; the new blocks are tabled below."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from telegram_fakes import FakeTransport
from test_live_run_e2e import BASELINE_NOW, load_state, run_live

from enji_guard_radar.ledger import (
    RESOLVED_LIMIT,
    ConditionRecord,
    DigestRecord,
    EvaluationOutcome,
    PollHealth,
    ProjectEvaluation,
    ProjectRecord,
    ReminderClock,
    ResolvedItem,
    RunPoint,
    ScoreRecord,
    SeriesHistory,
    State,
    StateFormatError,
    ToolingRecord,
    append_resolved,
    apply_delivery,
    deserialize_state,
    migrate_state_v1,
    migrate_state_v2,
    reconcile,
    serialize_state,
)
from enji_guard_radar.model import Alert, AlertKey, AlertKind, Severity

T0 = datetime(2026, 11, 16, 12, tzinfo=UTC)
T1 = datetime(2026, 11, 17, 12, tzinfo=UTC)
UNLISTED_ID = "00000000-0000-4000-8000-00000000ffff"


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _unlisted_history() -> SeriesHistory:
    return SeriesHistory(
        project_id=UNLISTED_ID,
        repo_id="r1",
        action_key="audit.security",
        points=(
            RunPoint(task_id="t1", completed_at=T0, high=2),
            RunPoint(task_id="t2", completed_at=T1, high=3),
        ),
        open=True,
        opened_at=T0,
        clean_runs=0,
    )


def test_v2_state_file_upgrades_through_a_live_run(tmp_path: Path) -> None:
    history = _unlisted_history()
    document = {
        "schema_version": 2,
        "projects": [],
        "conditions": [],
        "histories": [json.loads(history.model_dump_json())],
        "health": {},
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps(document), encoding="utf-8")

    transport = FakeTransport([])
    code, _ = run_live(tmp_path, BASELINE_NOW, transport)

    assert code == 0
    assert transport.requests == []
    state = load_state(tmp_path)
    assert state.schema_version == 3
    assert [h for h in state.histories if h.project_id == UNLISTED_ID] == [history]
    assert state.health == PollHealth()


NAIVE = datetime(2026, 1, 1)  # noqa: DTZ001 - naive on purpose: the rows below must reject it
STAMP = "2026-11-16T12:00:00Z"
CANARY = "LEAK-CANARY"


def _v1_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "projects": [{"project_id": "p", "first_seen_at": STAMP, "last_evaluated_at": STAMP}],
        "conditions": [],
    }


def _v2_document() -> dict[str, object]:
    history = json.loads(_unlisted_history().model_dump_json())
    return {
        **_v1_document(),
        "schema_version": 2,
        "histories": [history],
        "health": {"consecutive_failures": 3},
    }


def _score(repo: str, score: int = 50) -> ScoreRecord:
    return ScoreRecord(
        project_id="p", repo_id=repo, action_key="a", task_id="t", completed_at=T0, score=score
    )


def _tooling(repo: str, *, completed: bool = False) -> ToolingRecord:
    return ToolingRecord(
        project_id="p",
        repo_id=repo,
        action_key="a",
        observed_since=T0,
        last_completed_at=T1 if completed else None,
    )


def _reminder(repo: str) -> ReminderClock:
    return ReminderClock(project_id="p", repo_id=repo, action_key="a", last_reminded_at=T1)


def _resolved_key(index: int) -> AlertKey:
    return AlertKey(project_id="p", repo_id="r", action_key=f"a{index:03d}", level="high")


def _item(index: int) -> ResolvedItem:
    return ResolvedItem(key=_resolved_key(index), resolved_at=T0 + timedelta(minutes=index))


def _full_state(order: tuple[int, ...] = (1, 2, 3)) -> State:
    repos = [f"r{i}" for i in order]
    return State(
        scores=tuple(_score(r, 10 * int(r[1:])) for r in repos),
        tooling=tuple(_tooling(r, completed=r == "r2") for r in repos if r != "r3"),
        reminders=tuple(_reminder(r) for r in repos if r != "r3"),
        digest=DigestRecord(planned_for=T0, outcome="sent", sent_at=T1),
        resolved=tuple(_item(i) for i in order),
    )


@pytest.mark.parametrize("document", [_v1_document(), _v2_document()], ids=["v1", "v2"])
def test_old_documents_migrate_to_schema_3_with_empty_blocks(document: dict[str, object]) -> None:
    state = deserialize_state(json.dumps(document).encode())
    assert state.schema_version == 3
    assert (state.scores, state.tooling, state.reminders, state.resolved) == ((), (), (), ())
    assert state.digest == DigestRecord()
    assert [p.project_id for p in state.projects] == ["p"]


def test_v2_migration_keeps_histories_and_health() -> None:
    state = deserialize_state(json.dumps(_v2_document()).encode())
    assert state.histories == (_unlisted_history(),)
    assert state.health == PollHealth(consecutive_failures=3)


def test_migrate_state_v2_is_pure() -> None:
    raw = _v2_document()
    before = json.dumps(raw, sort_keys=True)
    migrated = migrate_state_v2(raw)
    assert json.dumps(raw, sort_keys=True) == before
    assert migrated is not raw
    assert migrated["schema_version"] == 3
    assert migrated["histories"] == raw["histories"]
    assert migrated["digest"] == {}


def test_migrate_state_v1_still_returns_schema_2_and_leaves_input() -> None:
    raw = _v1_document()
    before = json.dumps(raw, sort_keys=True)
    assert migrate_state_v1(raw)["schema_version"] == 2
    assert json.dumps(raw, sort_keys=True) == before


def test_schema_3_round_trip_is_byte_stable_and_order_free() -> None:
    forward = _full_state((1, 2, 3))
    shuffled = _full_state((3, 1, 2))
    data = serialize_state(forward)
    assert serialize_state(shuffled) == data
    restored = deserialize_state(data)
    assert restored == deserialize_state(serialize_state(shuffled))
    assert serialize_state(restored) == data
    assert json.loads(data)["schema_version"] == 3
    assert [s.repo_id for s in restored.scores] == ["r1", "r2", "r3"]
    assert [i.key.action_key for i in restored.resolved] == ["a001", "a002", "a003"]
    assert restored.tooling[0].last_completed_at is None


@pytest.mark.parametrize("score", [-1, 101, "5", True])
def test_score_record_rejects_bad_score(score: object) -> None:
    with pytest.raises(ValidationError):
        ScoreRecord(
            project_id="p", repo_id="r", action_key="a", task_id="t", completed_at=T0, score=score
        )


def test_score_record_accepts_the_bounds_and_keys_by_series() -> None:
    assert _score("r", 0).score == 0
    assert _score("r", 100).key() == ("p", "r", "a")


@pytest.mark.parametrize(
    "build",
    [
        lambda: ScoreRecord(
            project_id="p",
            repo_id="r",
            action_key="a",
            task_id="t",
            completed_at=NAIVE,
            score=5,
        ),
        lambda: ToolingRecord(project_id="p", repo_id="r", action_key="a", observed_since=NAIVE),
        lambda: ToolingRecord(
            project_id="p",
            repo_id="r",
            action_key="a",
            observed_since=T0,
            last_completed_at=NAIVE,
        ),
        lambda: ReminderClock(project_id="p", repo_id="r", action_key="a", last_reminded_at=NAIVE),
        lambda: ResolvedItem(key=_resolved_key(1), resolved_at=NAIVE),
        lambda: DigestRecord(planned_for=NAIVE, outcome="skipped"),
        lambda: ScoreRecord(
            project_id="", repo_id="r", action_key="a", task_id="t", completed_at=T0, score=5
        ),
        lambda: ToolingRecord(project_id="p", repo_id="", action_key="a", observed_since=T0),
        lambda: ReminderClock(project_id="p", repo_id="r", action_key="", last_reminded_at=T0),
        lambda: ScoreRecord(
            project_id="p",
            repo_id="r",
            action_key="a",
            task_id="t",
            completed_at=T0,
            score=5,
            extra=1,  # type: ignore[call-arg]
        ),
    ],
    ids=[
        "score-naive",
        "tooling-naive-since",
        "tooling-naive-completed",
        "reminder-naive",
        "resolved-naive",
        "digest-naive",
        "score-empty-id",
        "tooling-empty-id",
        "reminder-empty-id",
        "score-extra-field",
    ],
)
def test_records_reject_bad_values(build: object) -> None:
    with pytest.raises(ValidationError):
        build()  # type: ignore[operator]


@pytest.mark.parametrize(
    "fields",
    [
        {"outcome": "sent", "sent_at": T0},
        {"planned_for": T0},
        {"planned_for": T0, "outcome": "sent"},
        {"sent_at": T0},
        {"planned_for": T0, "outcome": "late"},
    ],
    ids=["outcome-only", "planned-only", "sent-without-sent_at", "sent_at-only", "bad-outcome"],
)
def test_digest_record_rejects_inconsistent_fields(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DigestRecord(**fields)


def test_digest_record_allows_a_skipped_week_to_keep_the_last_sent_at() -> None:
    record = DigestRecord(planned_for=T1, outcome="skipped", sent_at=T0)
    assert record.sent_at == T0
    assert DigestRecord().planned_for is None


def _doc(**changes: object) -> bytes:
    raw: dict[str, object] = {"schema_version": 3, "projects": [], "conditions": []}
    raw.update(changes)
    return json.dumps(raw).encode()


@pytest.mark.parametrize(
    "changes",
    [
        {
            "scores": [
                {
                    "project_id": CANARY,
                    "repo_id": "r",
                    "action_key": "a",
                    "task_id": "t",
                    "completed_at": STAMP,
                    "score": 101,
                }
            ]
        },
        {"scores": [{"project_id": CANARY, "score": -1}]},
        {"digest": {"planned_for": STAMP, "outcome": "sent", CANARY: 1}},
        {"digest": {"planned_for": STAMP, "outcome": "sent"}},
        {"tooling": [{"project_id": "p", "repo_id": "r", "action_key": CANARY}]},
        {"reminders": [CANARY]},
        {"resolved": [{"key": {"project_id": CANARY}, "resolved_at": "2026-11-16T12:00:00"}]},
        {CANARY: 1},
    ],
    ids=[
        "score-101",
        "score-negative",
        "digest-extra",
        "digest-sent-no-sent_at",
        "tooling",
        "reminder",
        "resolved-naive",
        "top-level-extra",
    ],
)
def test_corrupt_block_is_a_format_error_without_file_content(changes: dict[str, object]) -> None:
    with pytest.raises(StateFormatError) as info:
        deserialize_state(_doc(**changes))
    assert info.value.reason.startswith("does not match the state schema")
    assert CANARY not in info.value.reason
    assert CANARY not in str(info.value)


def test_unsupported_versions_name_the_readable_ones() -> None:
    for version in (4, "3", True):
        with pytest.raises(StateFormatError, match=r"\(this radar reads 1, 2 and 3\)"):
            deserialize_state(_doc(schema_version=version))


def _alert(project_id: str) -> Alert:
    return Alert(
        key=AlertKey(project_id=project_id, repo_id="*", level="stale"),
        kind=AlertKind.STALE,
        project_name=f"Name {project_id}",
        severity=Severity.MEDIUM,
        age_days=15,
        last_run_at=None,
        repo_count=1,
        count=None,
    )


def _carried(state: State) -> tuple[object, ...]:
    return (state.scores, state.tooling, state.reminders, state.digest, state.resolved)


def test_reconcile_and_apply_delivery_pass_the_new_blocks_through() -> None:
    stale = _alert("a")
    base = _full_state()
    known = base.model_copy(
        update={
            "projects": (ProjectRecord(project_id="a", first_seen_at=T0, last_evaluated_at=T0),),
            "conditions": (
                ConditionRecord(
                    key=stale.key, first_seen_at=T0, last_seen_at=T0, baseline=False, count=None
                ),
            ),
        }
    )
    expected = _carried(serialize_and_load(known))
    evaluation = ProjectEvaluation(
        project_id="a", outcome=EvaluationOutcome.evaluated, alerts=(stale,)
    )
    result = reconcile(known, [evaluation], T1)
    assert _carried(result.state) == expected
    delivered = apply_delivery(result.state, [stale.key], T1)
    assert _carried(delivered) == expected
    assert delivered.conditions[0].alerted_at == T1
    resolved = reconcile(
        delivered,
        [ProjectEvaluation(project_id="a", outcome=EvaluationOutcome.evaluated)],
        T1,
    )
    assert resolved.resolved == (stale.key,)
    assert _carried(resolved.state) == expected


def serialize_and_load(state: State) -> State:
    return deserialize_state(serialize_state(state))


def test_append_resolved_stamps_now_and_keeps_order_stable() -> None:
    state = State(resolved=(_item(1),), scores=(_score("r1"),))
    keys = [_resolved_key(7), _resolved_key(5)]
    result = append_resolved(state, keys, T1)
    assert [(i.key.action_key, i.resolved_at) for i in result.resolved] == [
        ("a001", T0 + timedelta(minutes=1)),
        ("a005", T1),
        ("a007", T1),
    ]
    assert result.scores == state.scores
    assert result.digest == state.digest
    assert state.resolved == (_item(1),)


def test_append_resolved_keeps_only_the_newest_limit() -> None:
    assert RESOLVED_LIMIT == 200
    state = append_resolved(State(), [_resolved_key(i) for i in range(150)], T0)
    state = append_resolved(state, [_resolved_key(i) for i in range(150, 300)], T1)
    names = sorted(i.key.action_key for i in state.resolved)
    assert len(state.resolved) == RESOLVED_LIMIT
    assert names[0] == "a100"
    assert names[-1] == "a299"


def test_append_resolved_rejects_a_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        append_resolved(State(), [_resolved_key(1)], NAIVE)


def test_serialized_state_holds_no_names() -> None:
    names = ("Project Alpha", "acme/service-a")
    state = _full_state()
    data = serialize_state(state)
    for name in names:
        assert name.encode() not in data
    assert set(json.loads(data)) == {
        "schema_version",
        "projects",
        "conditions",
        "histories",
        "health",
        "scores",
        "tooling",
        "reminders",
        "digest",
        "resolved",
    }
