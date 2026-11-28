"""One project line through run_once: delivery, retry and rise stay per condition (offline).

In-memory Guard, a fake notifier and a real state file in tmp_path.
"""

import json
from pathlib import Path

from test_high_pipeline import RID, RID2, SECURITY, Sim, texts
from test_triage_pipeline import HASH, SQL, TriageSim, fake

from enji_guard_radar.ledger import SCHEMA_VERSION, ConditionRecord
from enji_guard_radar.model import OutboundMessage
from enji_guard_radar.ports import DeliveryError, NotDelivered, SendOutcome
from enji_guard_radar.state_store import STATE_FILE

TESTS = "audit.tests"


class AlwaysFailing:
    def send(self, message: OutboundMessage) -> SendOutcome:
        return NotDelivered(error=DeliveryError.TRANSIENT, detail="try later", stop_sending=False)


def three_series_sim(tmp_path: Path) -> Sim:
    """One project, two repos, two audit types: three open High series from day 1."""
    sim = Sim(tmp_path)
    sim.repos.append((RID2, "acme/other"))
    sim.runs[RID2] = []
    sim.add_run("audit.recon", day=0, report=False, rid=RID2)
    sim.poll(0)
    sim.add_run(SECURITY, high=1, day=1)
    sim.add_run(TESTS, high=2, day=1)
    sim.add_run(SECURITY, high=1, day=1, rid=RID2)
    return sim


def conditions(sim: Sim) -> list[ConditionRecord]:
    return [c for c in sim.state().conditions if c.key.level == "high"]


def test_three_series_of_one_project_are_one_message_and_three_marked_conditions(
    tmp_path: Path,
) -> None:
    sim = three_series_sim(tmp_path)
    report = sim.poll(1)
    assert [m.text for m in report.messages] == [
        "🔴 <b>Proj</b>: high problems 4, in 2 audits, mostly security · today"
    ]
    assert len(report.messages[0].keys) == 3
    assert (report.delivered, report.not_delivered) == (1, 0)

    marked = conditions(sim)
    assert len(marked) == 3
    assert {(c.key.repo_id, c.key.action_key) for c in marked} == {
        (RID, SECURITY),
        (RID, TESTS),
        (RID2, SECURITY),
    }
    assert all(c.alerted_at is not None for c in marked)
    saved = json.loads((sim.state_dir / STATE_FILE).read_text(encoding="utf-8"))
    assert saved["schema_version"] == SCHEMA_VERSION == 3
    assert len([c for c in saved["conditions"] if c["key"]["level"] == "high"]) == 3
    assert not report.baselined


def test_a_failed_send_marks_nothing_and_the_next_run_sends_the_same_line(tmp_path: Path) -> None:
    sim = three_series_sim(tmp_path)
    report = sim.poll(1, notifier=AlwaysFailing())
    assert report.not_delivered == 1
    assert len(report.messages) == 1
    assert len(conditions(sim)) == 3
    assert all(c.alerted_at is None for c in conditions(sim))

    retry = sim.poll(2)
    assert texts(retry) == [
        "🔴 <b>Proj</b>: high problems 4, in 2 audits, mostly security · open for 1 day"
    ]
    assert len(retry.messages[0].keys) == 3
    assert all(c.alerted_at is not None for c in conditions(sim))
    assert texts(sim.poll(3)) == []


def test_a_rise_of_one_audit_resends_the_line_and_rearms_only_that_condition(
    tmp_path: Path,
) -> None:
    sim = three_series_sim(tmp_path)
    sim.poll(1)
    before = {(c.key.repo_id, c.key.action_key): c for c in conditions(sim)}
    sim.add_run(TESTS, high=5, day=2)
    report = sim.poll(2)

    assert texts(report) == [
        "🔴 <b>Proj</b>: high problems 5 (was 2), in audit tests · open for 1 day"
    ]
    assert [(k.repo_id, k.action_key) for k in report.messages[0].keys] == [(RID, TESTS)]
    after = {(c.key.repo_id, c.key.action_key): c for c in conditions(sim)}
    assert len(after) == 3
    rearmed = after[(RID, TESTS)]
    assert rearmed.count == 5
    assert rearmed.alerted_at is not None
    assert rearmed.alerted_at > before[(RID, TESTS)].alerted_at  # type: ignore[operator]
    for key in ((RID, SECURITY), (RID2, SECURITY)):
        assert after[key].alerted_at == before[key].alerted_at


def test_triage_gives_one_claude_line_for_a_project_with_two_audits(tmp_path: Path) -> None:
    triager = fake()
    sim = TriageSim(tmp_path, triager=triager)
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    sim.add_titled_run([HASH], day=1, action_key=TESTS)
    report = sim.poll(1)

    assert triager.calls == 1
    assert len(triager.requests[0].items) == 2
    (message,) = report.messages
    lines = message.text.split("\n")
    assert lines[0] == "🔴 <b>Proj</b>: high problems 2, in 2 audits, mostly security · today"
    assert len(lines) == 2
    assert lines[1].startswith("   P1 · ")
    assert len(message.keys) == 2
