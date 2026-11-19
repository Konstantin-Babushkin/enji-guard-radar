"""State schema 2: a Phase 1 file upgrades through a live run; histories and health round-trip."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from telegram_fakes import FakeTransport, ok_response
from test_live_run_e2e import A_ID, STALE_NOW, condition, load_state, run_live

from enji_guard_radar.ledger import (
    ConditionRecord,
    PollHealth,
    RunPoint,
    SeriesHistory,
    State,
    deserialize_state,
    migrate_state_v1,
    serialize_state,
)
from enji_guard_radar.model import AlertKey

T0 = datetime(2026, 11, 16, 12, tzinfo=UTC)
T1 = datetime(2026, 11, 17, 12, tzinfo=UTC)
B_ID = "00000000-0000-4000-8000-00000000a002"
C_ID = "00000000-0000-4000-8000-00000000a003"
STAMP = "2026-11-16T12:00:00Z"


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _v1_document() -> dict[str, object]:
    def cond(project_id: str, level: str) -> dict[str, object]:
        return {
            "key": {"project_id": project_id, "repo_id": "*", "action_key": "*", "level": level},
            "first_seen_at": STAMP,
            "last_seen_at": STAMP,
            "baseline": True,
            "alerted_at": None,
            "count": None,
        }

    return {
        "schema_version": 1,
        "projects": [
            {"project_id": pid, "first_seen_at": STAMP, "last_evaluated_at": STAMP}
            for pid in (A_ID, B_ID, C_ID)
        ],
        "conditions": [cond(B_ID, "stale"), cond(C_ID, "never_ran")],
    }


def test_v1_state_file_upgrades_through_a_live_run(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps(_v1_document()), encoding="utf-8")

    transport = FakeTransport([ok_response(7), ok_response(8)])
    code, _ = run_live(tmp_path, STALE_NOW, transport)

    assert code == 0
    # A is a known project in the migrated state: its stale alert and its first-seen High series
    # are both sent immediately.
    texts = {str(request[1]["text"]) for request in transport.requests}
    assert len(transport.requests) == 2
    assert sum(text.startswith("🟡 <b>Project A") and "no scan" in text for text in texts) == 1
    assert (
        sum(text.startswith("🔴 <b>Project A") and "security High 0→1" in text for text in texts)
        == 1
    )
    state = load_state(tmp_path)
    assert state.schema_version == 3
    assert {h.project_id for h in state.histories} == {A_ID}
    assert any(h.action_key == "audit.security" and h.open for h in state.histories)
    assert state.health == PollHealth()
    for project_id, level in ((B_ID, "stale"), (C_ID, "never_ran")):
        record = condition(state, project_id, level)
        assert record.baseline is True
        assert record.first_seen_at == datetime(2026, 11, 16, 12, tzinfo=UTC)
    assert condition(state, A_ID, "stale").alerted_at is not None
    assert condition(state, A_ID, "high").alerted_at is not None


def test_migrate_state_v1_adds_histories_and_health() -> None:
    raw = _v1_document()
    before = json.dumps(raw, sort_keys=True)
    migrated = migrate_state_v1(raw)
    assert json.dumps(raw, sort_keys=True) == before
    assert migrated["schema_version"] == 2
    assert migrated["histories"] == []
    assert migrated["health"] == {}
    assert migrated["projects"] == raw["projects"]
    assert migrated["conditions"] == raw["conditions"]
    assert migrated is not raw


def _history(repo: str, *, open_: bool) -> SeriesHistory:
    return SeriesHistory(
        project_id="p",
        repo_id=repo,
        action_key="sast",
        points=(
            RunPoint(task_id="t1", completed_at=T0, high=2),
            RunPoint(task_id="t2", completed_at=T1, high=0),
        ),
        open=open_,
        opened_at=T0 if open_ else None,
        clean_runs=1 if open_ else 0,
    )


def test_schema_2_round_trips_histories_and_health() -> None:
    health = PollHealth(
        consecutive_failures=3,
        first_failure_at=T0,
        last_failure_cause="unavailable",
        last_success_at=T1,
        outage_reported_at=T1,
    )
    state = State(
        histories=(_history("r2", open_=False), _history("r1", open_=True)), health=health
    )
    data = serialize_state(state)
    restored = deserialize_state(data)
    assert [h.repo_id for h in restored.histories] == ["r1", "r2"]
    assert restored.health == health
    assert serialize_state(restored) == data
    assert deserialize_state(serialize_state(restored)) == restored


@pytest.mark.parametrize(
    "changes",
    [
        {"open": True, "opened_at": None},
        {"open": False, "opened_at": T0},
        {"open": False, "opened_at": None, "clean_runs": 1},
    ],
    ids=["open-without-opened_at", "opened_at-while-closed", "clean_runs-while-closed"],
)
def test_series_history_rejects_inconsistent_flags(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SeriesHistory(project_id="p", repo_id="r", action_key="a", **changes)


@pytest.mark.parametrize("high", [-1, "2", True])
def test_run_point_rejects_bad_high(high: object) -> None:
    with pytest.raises(ValidationError):
        RunPoint(task_id="t", completed_at=T0, high=high)


def test_state_bytes_hold_no_names() -> None:
    state = State(
        conditions=(
            ConditionRecord(
                key=AlertKey(project_id=A_ID, repo_id="*", level="stale"),
                first_seen_at=T0,
                last_seen_at=T0,
                baseline=False,
                count=2,
            ),
        ),
        histories=(_history("r1", open_=True),),
    )
    assert b"Project" not in serialize_state(state)


def test_condition_record_fields_unchanged_from_schema_1() -> None:
    assert set(ConditionRecord.model_fields) == {
        "key",
        "first_seen_at",
        "last_seen_at",
        "baseline",
        "alerted_at",
        "count",
    }
