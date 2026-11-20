"""Triage in the run, end to end through run_once: in-memory Guard, fake notifier, FakeTriager.

Everything is offline. The state file and the cost file are real files in tmp_path. Finding
titles come from reports built here, because guard_fakes only writes a placeholder title.
"""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from guard_fakes import InMemoryGuardTools, world
from test_high_pipeline import (
    BASE,
    CALM_CONFIG,
    PID,
    RID,
    RID2,
    SECURITY,
    Accepting,
    Sim,
    texts,
)
from triage_fakes import DEFAULT_CAUSE, FakeTriager, reply_for

from enji_guard_radar.ledger import SCHEMA_VERSION
from enji_guard_radar.pipeline import Deps, RunReport, run_once
from enji_guard_radar.ports import DeliveryError, NotDelivered, Notifier, SendOutcome
from enji_guard_radar.state_store import STATE_FILE, FileStateStore
from enji_guard_radar.triage import (
    TriageFailed,
    TriageFailure,
    TriageOutcome,
    TriageReply,
    TriageRequest,
)
from enji_guard_radar.triage_cost import TriageCostStore, call_line
from enji_guard_radar.triage_cost_store import COST_FILE, FileTriageCostStore

SQL = "SQL injection in order search"
HASH = "Weak password hashing in login"
REDIRECT = "Open redirect on logout page"
PID2 = "p-2"
RID3 = "repo_r-3"
TRIAGE_ON = CALM_CONFIG + "triage:\n  enabled: true\n"
TRIAGE_OFF = CALM_CONFIG + "triage:\n  enabled: false\n"
P1_SQL = f"\n   P1 · {SQL}"


def titled_page(task_id: str, titles: list[str]) -> dict[str, object]:
    """A ready scored report whose High findings carry the given titles (same layout as
    guard_fakes.report_markdown)."""
    lines = ["# Placeholder report title", "", "**Score: 50 / 100** · Placeholder", ""]
    lines += ["## Security findings", ""]
    for number, title in enumerate(titles, start=1):
        lines += [f"### `F-{number:03d}` — {title}", ""]
        lines += ["**Severity:** High · **Confidence:** High", ""]
    return {
        "task_id": task_id,
        "report_status": "ready",
        "content": "\n".join(lines),
        "content_version": "v1",
        "has_more": False,
    }


def ok_reply(request: TriageRequest) -> TriageOutcome:
    return TriageReply(
        text=reply_for(request),
        seconds=1.5,
        cost_usd=0.0123,
        input_tokens=100,
        output_tokens=20,
    )


class TriageSim(Sim):
    """The Sim project plus a second repository and a second project, with triage in Deps."""

    def __init__(
        self,
        tmp_path: Path,
        config_text: str = TRIAGE_ON,
        *,
        triager: FakeTriager | None = None,
        cost_store: TriageCostStore | None = None,
        known_secrets: tuple[str, ...] = (),
        **kwargs: bool,
    ) -> None:
        super().__init__(tmp_path, config_text, **kwargs)
        self.triager = triager
        self.cost_store = cost_store
        self.known_secrets = known_secrets
        self.second_project = False

    def add_titled_run(
        self, titles: list[str], *, day: int, rid: str = RID, action_key: str = SECURITY
    ) -> str:
        task_id = self.add_run(action_key, day=day, report=False, rid=rid)
        self.reports[task_id] = titled_page(task_id, titles)
        return task_id

    def add_second_repo(self) -> None:
        self.repos.append((RID2, "acme/other"))
        self.runs[RID2] = []
        self.add_run("audit.recon", day=0, report=False, rid=RID2)

    def add_second_project(self) -> None:
        self.second_project = True
        self.runs[RID3] = []
        self.add_run("audit.recon", day=0, report=False, rid=RID3)

    def poll(
        self,
        day: int,
        *,
        notifier: Notifier | None = None,
        caplog: pytest.LogCaptureFixture | None = None,
    ) -> RunReport:
        projects = [(PID, "Proj")]
        repos = {PID: self.repos}
        if self.second_project:
            projects.append((PID2, "Second"))
            repos[PID2] = [(RID3, "acme/third")]
        guard = InMemoryGuardTools(
            world(projects, repos, self.runs, self.reports, self.failures),
        )
        self.last_guard = guard
        now = BASE + timedelta(days=day, hours=2)
        deps = Deps(
            guard=guard,
            notifier=notifier or Accepting(),
            config=self.config,
            clock=lambda: now,
            store=FileStateStore(self.state_dir),
            triager=self.triager,
            cost_store=self.cost_store,
            known_secrets=self.known_secrets,
        )
        return run_once(deps)

    def state_bytes(self) -> bytes:
        return (self.state_dir / STATE_FILE).read_bytes()


def fake(cost: bool = True) -> FakeTriager:
    return FakeTriager(
        ok_reply if cost else (lambda r: TriageReply(text=reply_for(r), seconds=0.5))
    )


def first_alert_sim(
    tmp_path: Path, triager: FakeTriager | None, *, config: str = TRIAGE_ON, **kwargs: object
) -> TriageSim:
    """A known project that gets one new High series on day 1; day 0 has been polled."""
    sim = TriageSim(tmp_path, config, triager=triager, **kwargs)  # type: ignore[arg-type]
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    return sim


def three_alert_sim(tmp_path: Path, triager: FakeTriager | None, **kwargs: object) -> TriageSim:
    """Two projects, three repositories; every one gets a new High series on day 1."""
    sim = TriageSim(tmp_path, TRIAGE_ON, triager=triager, **kwargs)  # type: ignore[arg-type]
    sim.add_second_repo()
    sim.add_second_project()
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    sim.add_titled_run([HASH], day=1, rid=RID2)
    sim.add_titled_run([REDIRECT], day=1, rid=RID3)
    return sim


def state_files(directory: Path) -> Mapping[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


# ---- tracer ----------------------------------------------------------------------------------


def test_fresh_high_alert_gets_cause(tmp_path: Path) -> None:
    triager = fake()
    sim = first_alert_sim(tmp_path, triager)
    report = sim.poll(1)
    assert texts(report) == [f"🔴 <b>Proj</b> · security High 0→1 · 0d{P1_SQL}"]
    assert triager.calls == 1
    (request,) = triager.requests
    assert len(request.items) == 1
    assert request.items[0].titles == (SQL,)


# ---- off -------------------------------------------------------------------------------------


def test_off_disabled_makes_no_call_and_same_texts(tmp_path: Path) -> None:
    triager = fake()
    with_fake = first_alert_sim(tmp_path / "a", triager, config=TRIAGE_OFF)
    plain = first_alert_sim(tmp_path / "b", None, config=TRIAGE_OFF)
    assert texts(with_fake.poll(1)) == texts(plain.poll(1))
    assert triager.calls == 0
    assert texts(plain.poll(2)) == []


def test_off_without_triager_or_store_is_the_old_run(tmp_path: Path) -> None:
    sim = first_alert_sim(tmp_path, None)
    assert texts(sim.poll(1)) == ["🔴 <b>Proj</b> · security High 0→1 · 0d"]


@dataclass
class TouchedStore:
    touched: list[str] = field(default_factory=list)

    def load(self) -> tuple[()]:
        self.touched.append("load")
        return ()

    def save(self, days: object) -> None:
        self.touched.append("save")


def test_off_never_touches_the_cost_store(tmp_path: Path) -> None:
    store = TouchedStore()
    sim = first_alert_sim(tmp_path / "a", fake(), config=TRIAGE_OFF, cost_store=store)
    sim.poll(1)
    other = first_alert_sim(tmp_path / "b", None, cost_store=store)
    other.poll(1)
    assert store.touched == []


# ---- one call --------------------------------------------------------------------------------


def test_one_call_for_three_fresh_alerts_in_two_projects(tmp_path: Path) -> None:
    triager = fake()
    sim = three_alert_sim(tmp_path, triager)
    report = sim.poll(1)
    assert triager.calls == 1
    assert len(triager.requests[0].items) == 3
    assert len(report.messages) == 3
    assert all(message.text.endswith(f"\n   P1 · {DEFAULT_CAUSE}") for message in report.messages)


def test_one_call_item_cap_keeps_the_rest_rule_only(tmp_path: Path) -> None:
    triager = fake()
    config = TRIAGE_ON + "  max_items_per_call: 1\n"
    sim = TriageSim(tmp_path, config, triager=triager)
    sim.add_second_repo()
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    sim.add_titled_run([HASH], day=1, rid=RID2)
    report = sim.poll(1)
    assert triager.calls == 1
    assert len(triager.requests[0].items) == 1
    assert sorted(m.text.count("P1 ·") for m in report.messages) == [0, 1]


# ---- not fresh -------------------------------------------------------------------------------


def test_not_fresh_rearmed_alert_is_not_sent_to_claude(tmp_path: Path) -> None:
    triager = fake()
    sim = first_alert_sim(tmp_path, triager)
    sim.poll(1)
    sim.add_titled_run([SQL, HASH, REDIRECT], day=2)
    (text,) = texts(sim.poll(2))
    assert text == "🔴 <b>Proj</b> · security High 1→3 · 1d"
    assert triager.calls == 1


def test_not_fresh_retried_alert_is_not_sent_to_claude_again(tmp_path: Path) -> None:
    class FailsOnce:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, message: object) -> SendOutcome:
            self.sent.append(getattr(message, "text", ""))
            if len(self.sent) == 1:
                return NotDelivered(
                    error=DeliveryError.TRANSIENT, detail="try later", stop_sending=False
                )
            return Accepting().send(message)

    triager = fake()
    sim = first_alert_sim(tmp_path, triager)
    notifier = FailsOnce()
    failed = sim.poll(1, notifier=notifier)
    assert failed.not_delivered == 1
    assert notifier.sent[0].endswith(P1_SQL)
    # A new run with the same count is read this run, so titles exist: still not fresh.
    sim.add_titled_run([SQL], day=2)
    retried = sim.poll(2, notifier=notifier)
    assert retried.delivered == 1
    assert notifier.sent[1] == "🔴 <b>Proj</b> · security High 1→1 · 1d"
    assert triager.calls == 1


def test_not_fresh_reminder_and_digest_never_reach_claude(tmp_path: Path) -> None:
    triager = fake()
    sim = TriageSim(tmp_path, TRIAGE_ON, triager=triager, reminders_on=True, digest_on=True)
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    assert sim.poll(1).messages[0].text.endswith(P1_SQL)
    # Day 9 is a Wednesday and 8 days after the alert: a reminder and the digest are due.
    report = sim.poll(9)
    assert report.messages == ()
    assert report.reminder_messages
    assert report.digest_messages
    assert triager.calls == 1
    for message in (*report.reminder_messages, *report.digest_messages):
        assert "P1 ·" not in message.text


# ---- skips -----------------------------------------------------------------------------------


def test_no_titles_skips_with_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    triager = fake()
    sim = TriageSim(tmp_path, TRIAGE_ON, triager=triager)
    sim.poll(0)
    sim.add_titled_run(["https://example.com/a/b"], day=1)
    with caplog.at_level(logging.INFO):
        report = sim.poll(1)
    assert texts(report) == ["🔴 <b>Proj</b> · security High 0→1 · 0d"]
    assert triager.calls == 0
    assert "triage skipped: no usable titles" in caplog.text


def test_nothing_fresh_skips_with_log(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    triager = fake()
    sim = TriageSim(tmp_path, TRIAGE_ON, triager=triager)
    with caplog.at_level(logging.INFO):
        sim.poll(0)
        sim.poll(1)
    assert triager.calls == 0
    assert "triage skipped: no fresh High alerts" in caplog.text


# ---- fallback --------------------------------------------------------------------------------

FAILURES: list[TriageOutcome] = [
    TriageFailed(reason=TriageFailure.TIMEOUT, seconds=60.0),
    TriageReply(text="this is not json", seconds=1.0),
]


@pytest.mark.parametrize("outcome", FAILURES, ids=["timeout", "not_json"])
def test_fallback_is_the_rule_only_run(tmp_path: Path, outcome: TriageOutcome) -> None:
    plain = three_alert_sim(tmp_path / "plain", None)
    expected = plain.poll(1)
    failing = three_alert_sim(tmp_path / "failing", FakeTriager([outcome]))
    actual = failing.poll(1)
    assert failing.triager is not None
    assert failing.triager.calls == 1
    assert actual == expected
    assert [m.keys for m in actual.messages] == [m.keys for m in expected.messages]
    assert state_files(failing.state_dir) == state_files(plain.state_dir)
    assert "P1 ·" not in "".join(texts(actual))


def test_fallback_unknown_ids_reply_is_rule_only(tmp_path: Path) -> None:
    reply = json.dumps({"items": [{"item_id": "i9", "priority": "p1", "cause": "Some cause"}]})
    sim = first_alert_sim(tmp_path, FakeTriager([TriageReply(text=reply, seconds=1.0)]))
    assert texts(sim.poll(1)) == ["🔴 <b>Proj</b> · security High 0→1 · 0d"]


# ---- partial ---------------------------------------------------------------------------------


def test_partial_one_item_rejected_keeps_the_other(tmp_path: Path) -> None:
    def reply(request: TriageRequest) -> TriageOutcome:
        causes = ["Fine plain cause", "Cause with 3 digits"]
        items = [
            {"item_id": item.item_id, "priority": "p2", "cause": cause}
            for item, cause in zip(request.items, causes, strict=True)
        ]
        return TriageReply(text=json.dumps({"items": items}), seconds=1.0)

    triager = FakeTriager(reply)
    sim = TriageSim(tmp_path, TRIAGE_ON, triager=triager)
    sim.add_second_repo()
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    sim.add_titled_run([HASH], day=1, rid=RID2)
    report = sim.poll(1)
    assert len(triager.requests[0].items) == 2
    lines = [line for message in report.messages for line in message.text.split("\n")]
    assert lines.count("   P2 · Fine plain cause") == 1
    assert sum("P2 ·" in line for line in lines) == 1
    assert len(report.messages) == 2


# ---- cost ------------------------------------------------------------------------------------


def test_cost_file_has_one_call_and_both_lines_are_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = FileTriageCostStore(tmp_path / "cost")
    sim = first_alert_sim(tmp_path / "run", fake(), cost_store=store)
    with caplog.at_level(logging.INFO):
        sim.poll(1)
    (day,) = store.load()
    assert day.day == (BASE + timedelta(days=1, hours=2)).astimezone(UTC).date()
    assert (day.calls, day.failures, day.unknown_cost_calls) == (1, 0, 0)
    assert day.input_tokens == 100
    assert (
        "triage call: items 1, outcome ok, seconds 1.5, cost_usd 0.0123, "
        "input_tokens 100, output_tokens 20" in caplog.text
    )
    assert "triage today (UTC): 1 calls, 0 failed, $0.0123" in caplog.text
    sim.add_titled_run([HASH], day=1, rid=RID, action_key="audit.dependencies")
    sim.poll(1)
    assert store.load()[0].calls == 2


def test_cost_failed_call_is_counted(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = FileTriageCostStore(tmp_path / "cost")
    outcome = TriageFailed(reason=TriageFailure.NOT_INSTALLED, seconds=0.2)
    sim = first_alert_sim(tmp_path / "run", FakeTriager([outcome]), cost_store=store)
    with caplog.at_level(logging.INFO):
        sim.poll(1)
    (day,) = store.load()
    assert (day.calls, day.failures, day.unknown_cost_calls) == (1, 1, 1)
    assert call_line(1, outcome) in caplog.text
    assert "triage today (UTC): 1 calls, 1 failed, $unknown" in caplog.text


def test_cost_none_store_writes_nothing_but_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sim = first_alert_sim(tmp_path, fake(), cost_store=None)
    with caplog.at_level(logging.INFO):
        sim.poll(1)
    assert not list(tmp_path.rglob(COST_FILE))
    assert "triage call: items 1, outcome ok" in caplog.text
    assert "triage today (UTC): 1 calls, 0 failed" in caplog.text


def test_cost_write_failure_changes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    plain = first_alert_sim(tmp_path / "plain", fake(), cost_store=None)
    expected = plain.poll(1)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    broken = first_alert_sim(
        tmp_path / "broken", fake(), cost_store=FileTriageCostStore(blocker / "cost")
    )
    with caplog.at_level(logging.WARNING):
        actual = broken.poll(1)
    assert actual == expected
    assert broken.state_bytes() == plain.state_bytes()
    assert "triage cost not written" in caplog.text


# ---- persistence and logs --------------------------------------------------------------------


def test_persist_state_has_no_titles_and_schema_3(tmp_path: Path) -> None:
    titles = [SQL, HASH, REDIRECT]
    sim = TriageSim(tmp_path, TRIAGE_ON, triager=fake(), cost_store=None)
    sim.poll(0)
    sim.add_titled_run(titles, day=1)
    sim.poll(1)
    raw = b"".join(state_files(sim.state_dir).values())
    for title in titles:
        assert title.encode() not in raw
    assert DEFAULT_CAUSE.encode() not in raw
    assert json.loads(sim.state_bytes())["schema_version"] == SCHEMA_VERSION == 3


def test_logs_contain_no_title_or_cause(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cause = "Unique cause marker"
    title = "Unique title marker"

    def reply(request: TriageRequest) -> TriageOutcome:
        return TriageReply(text=reply_for(request, cause=cause), seconds=1.0)

    sim = TriageSim(tmp_path, TRIAGE_ON, triager=FakeTriager(reply), cost_store=None)
    sim.poll(0)
    sim.add_titled_run([title], day=1)
    with caplog.at_level(logging.DEBUG):
        report = sim.poll(1)
    assert texts(report) == [f"🔴 <b>Proj</b> · security High 0→1 · 0d\n   P1 · {cause}"]
    assert title not in caplog.text
    assert cause not in caplog.text
    assert "triage call:" in caplog.text


def test_logs_known_secret_in_a_title_never_reaches_the_request(tmp_path: Path) -> None:
    triager = fake()
    sim = TriageSim(tmp_path, TRIAGE_ON, triager=triager, known_secrets=("hunter2-secret",))
    sim.poll(0)
    sim.add_titled_run(["Leaks hunter2-secret in a header", HASH], day=1)
    sim.poll(1)
    assert triager.requests[0].items[0].titles == (HASH,)


def test_fake_triager_never_raises_on_a_scripted_outcome() -> None:
    # The step has no try/except (D-11: the adapter owns the one broad except), so the shared
    # fake must hand back every outcome instead of raising.
    request = TriageRequest(language="en", items=())
    outcome = TriageFailed(reason=TriageFailure.TIMEOUT, seconds=1.0)
    assert FakeTriager([outcome]).triage(request) == outcome
