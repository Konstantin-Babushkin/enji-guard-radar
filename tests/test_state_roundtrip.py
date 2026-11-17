"""Tracer: baseline, alert, acknowledge, save, reload, never re-sent."""

import json
from datetime import UTC, datetime
from pathlib import Path

from enji_guard_radar.ledger import (
    EvaluationOutcome,
    Loaded,
    ProjectEvaluation,
    State,
    apply_delivery,
    reconcile,
)
from enji_guard_radar.model import Alert, AlertKey, AlertKind, Severity
from enji_guard_radar.state_store import FileStateStore


def stale_alert(project_id: str, name: str) -> Alert:
    return Alert(
        key=AlertKey(project_id=project_id, level="stale"),
        kind=AlertKind.STALE,
        project_name=name,
        severity=Severity.MEDIUM,
        age_days=15,
        last_run_at=None,
        repo_count=1,
    )


def evaluated(project_id: str, *alerts: Alert) -> ProjectEvaluation:
    return ProjectEvaluation(
        project_id=project_id, outcome=EvaluationOutcome.evaluated, alerts=alerts
    )


def load_state(store: FileStateStore) -> State:
    loaded = store.load()
    assert isinstance(loaded, Loaded)
    return loaded.state


def test_alerted_condition_is_not_planned_again_after_reload(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path / "state")
    alert_a = stale_alert("a", "Project A")
    alert_b = stale_alert("b", "Project B")

    first = reconcile(
        State.empty(),
        [evaluated("a"), evaluated("b", alert_b)],
        datetime(2026, 11, 16, 12, tzinfo=UTC),
    )
    assert first.to_send == ()
    assert first.baselined == (alert_b,)
    store.save(first.state)

    now = datetime(2026, 12, 1, 12, tzinfo=UTC)
    evaluations = [evaluated("a", alert_a), evaluated("b", alert_b)]
    second = reconcile(load_state(store), evaluations, now)
    assert second.to_send == (alert_a,)

    store.save(apply_delivery(second.state, [alert_a.key], now))
    third = reconcile(load_state(store), evaluations, now)
    assert third.to_send == ()

    raw = (tmp_path / "state" / "state.json").read_bytes()
    assert json.loads(raw)["schema_version"] == 2
    assert b"Project A" not in raw
    assert b"Project B" not in raw
