"""High / low audit-type priority through the run: low types are tracked, never alerted.

Everything is offline: in-memory Guard worlds through pipeline.run_once (`Sim` from the High tests).
"""

import json
from pathlib import Path

from test_high_pipeline import CALM_CONFIG, PID, RID, SECURITY, Sim, texts
from test_triage_pipeline import SQL, TRIAGE_ON, TriageSim
from triage_fakes import FakeTriager, reply_for

from enji_guard_radar.ledger import SCHEMA_VERSION, SeriesHistory
from enji_guard_radar.state_store import STATE_FILE
from enji_guard_radar.triage import TriageReply, TriageRequest

TECH_HEALTH = "audit.tech-health"
LOW_SECURITY = "high:\n  high_priority_audit_types:\n    - audit.tech-health\n"
LOW = CALM_CONFIG + LOW_SECURITY
TESTS = "audit.tests"


def security_history(sim: Sim) -> SeriesHistory:
    return sim.history(SECURITY)


def conditions_for(sim: Sim, action_key: str) -> list[object]:
    return [c for c in sim.state().conditions if c.key.action_key == action_key]


def test_low_type_series_is_tracked_in_histories_but_has_no_condition(tmp_path: Path) -> None:
    sim = Sim(tmp_path, LOW)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    report = sim.poll(1)
    assert report.messages == ()
    history = security_history(sim)
    assert history.open is True
    assert [point.high for point in history.points] == [3]
    assert conditions_for(sim, SECURITY) == []
    assert [c for c in sim.state().conditions if c.key.level == "high"] == []


def test_low_type_rise_sends_no_alert(tmp_path: Path) -> None:
    sim = Sim(tmp_path, LOW)
    sim.poll(0)
    for day, high in ((1, 1), (2, 3), (3, 5)):
        sim.add_run(high=high, day=day)
        report = sim.poll(day)
        assert report.messages == ()
        assert report.delivered == 0
    assert [point.high for point in security_history(sim).points][-1] == 5
    assert conditions_for(sim, SECURITY) == []


def test_low_type_never_gets_a_reminder(tmp_path: Path) -> None:
    sim = Sim(tmp_path, LOW, reminders_on=True)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    for day in (8, 9, 20, 40):
        report = sim.poll(day)
        assert report.reminder_messages == ()
        assert report.reminders_delivered == 0
        assert report.messages == ()
    assert sim.state().reminders == ()


def test_low_type_never_reaches_triage_candidates(tmp_path: Path) -> None:
    def reply(request: TriageRequest) -> TriageReply:
        return TriageReply(text=reply_for(request), seconds=1.0)

    triager = FakeTriager(reply)
    sim = TriageSim(tmp_path, TRIAGE_ON + LOW_SECURITY, triager=triager)
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)  # security: low priority here
    sim.add_titled_run(["Config drift"], day=1, action_key=TECH_HEALTH)
    report = sim.poll(1)
    assert len(report.messages) == 1
    # T-0051 lines name the project, not the audit: check the keys the message carries.
    assert [key.action_key for key in report.messages[0].keys] == [TECH_HEALTH]
    (request,) = triager.requests
    assert [item.audit_type for item in request.items] == [TECH_HEALTH]
    assert all(SQL not in item.titles for item in request.items)

    only_low = FakeTriager([])  # a call would raise: the script is empty
    quiet = TriageSim(tmp_path / "quiet", TRIAGE_ON + LOW_SECURITY, triager=only_low)
    quiet.poll(0)
    quiet.add_titled_run([SQL], day=1)
    assert quiet.poll(1).messages == ()
    assert only_low.calls == 0


def test_default_config_keeps_every_type_high(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.add_run(TESTS, high=1, day=1)
    report = sim.poll(1)
    assert (
        sorted(texts(report))
        == [
            "🔴 <b>Proj</b>: high problems 2, in audit security · today",
            "🔴 <b>Proj</b>: high problems 1, in audit tests · today",
        ]
        or len(report.messages) == 1
    )
    levels = {c.key.action_key for c in sim.state().conditions if c.key.level == "high"}
    assert levels == {SECURITY, TESTS}


def test_reclassified_high_to_low_is_not_listed_as_resolved(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    assert len(sim.poll(1).messages) == 1
    assert len(conditions_for(sim, SECURITY)) == 1

    sim.config_text = LOW
    report = sim.poll(2)
    assert report.messages == ()
    assert report.resolved == ()
    assert sim.state().resolved == ()
    assert conditions_for(sim, SECURITY) == []
    assert security_history(sim).open is True


def test_reclassified_low_to_high_alerts_once(tmp_path: Path) -> None:
    sim = Sim(tmp_path, LOW)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    assert sim.poll(1).messages == ()
    assert sim.poll(2).messages == ()

    sim.config_text = CALM_CONFIG
    (message,) = sim.poll(3).messages
    assert "high problems 2, in audit security" in message.text
    assert [key.action_key for key in message.keys] == [SECURITY]
    assert sim.poll(4).messages == ()
    assert len(conditions_for(sim, SECURITY)) == 1


def test_state_schema_stays_3_and_old_state_loads(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 3
    sim = Sim(tmp_path, LOW)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    raw = json.loads((sim.state_dir / STATE_FILE).read_text(encoding="utf-8"))
    assert raw["schema_version"] == 3
    assert [h["action_key"] for h in raw["histories"]] == [SECURITY]
    assert raw["conditions"] == [] or all(
        c["key"]["action_key"] != SECURITY for c in raw["conditions"]
    )

    # A state written while every type was high (a condition and a history) loads under the
    # new rules and runs: the security series is simply low priority now.
    old = Sim(tmp_path / "old")
    old.poll(0)
    old.add_run(high=2, day=1)
    old.poll(1)
    assert len(conditions_for(old, SECURITY)) == 1
    old.config_text = LOW
    report = old.poll(2)
    assert report.messages == ()
    assert (PID, RID, SECURITY) in {h.key() for h in old.state().histories}
