"""High alerts through the whole run: the tracer on a fixture copy and the pipeline edge table.

Everything is offline: a copy of fixtures/guard through cli.main with a fake Telegram transport,
and in-memory Guard worlds through pipeline.run_once.
"""

import io
import json
import logging
import shutil
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from guard_fakes import (
    InMemoryGuardTools,
    Key,
    Response,
    call_key,
    report_page,
    run_item,
    world,
)
from pydantic import SecretStr
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, error_response, ok_response
from test_live_run_e2e import A_ID, ENVIRON, FIXTURES, condition, load_state

from enji_guard_radar import cli
from enji_guard_radar.config import CLEAR_AFTER_MAX, RadarConfig, parse_config
from enji_guard_radar.guard import (
    ARG_CURSOR,
    ARG_PROJECT_ID,
    ARG_REPOSITORY_ID,
    TOOL_LIST_AUDIT_RUNS,
    TOOL_LIST_REPOSITORIES,
)
from enji_guard_radar.guard_report import ARG_TASK_ID, TOOL_GET_AUDIT_REPORT
from enji_guard_radar.high_history import HISTORY_WINDOW
from enji_guard_radar.ledger import (
    ConditionRecord,
    Loaded,
    SeriesHistory,
    State,
    serialize_state,
)
from enji_guard_radar.pipeline import Deps, RunOutcome, RunReport, run_once
from enji_guard_radar.ports import Delivered, GuardCallError, Notifier, SendOutcome
from enji_guard_radar.secret_guard import SecretGatedNotifier
from enji_guard_radar.state_store import FileStateStore
from enji_guard_radar.telegram import HttpTransport, TelegramNotifier

B_REPO_ID = "repo_00000000-0000-4000-8000-00000000b001"
C014 = "00000000-0000-4000-8000-00000000c014"
FIRST_POLL = "2026-11-16T12:00:00Z"
RISE_POLL = "2026-11-21T12:00:00Z"


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


# ---- tracer: cli.main on a fixture copy ------------------------------------------------------


def run_cli(
    guard_dir: Path,
    tmp_path: Path,
    now: str,
    transport: HttpTransport,
    *,
    dry_run: bool = False,
) -> tuple[int, str]:
    out = io.StringIO()
    args = [
        "run",
        "--fixtures",
        str(guard_dir),
        "--config",
        str(guard_dir / "radar.yaml"),
        "--state-dir",
        str(tmp_path / "state"),
        "--env-file",
        str(tmp_path / "none.env"),
        "--now",
        now,
    ]
    code = cli.main(
        [*args, "--dry-run"] if dry_run else args,
        environ=ENVIRON,
        transport=transport,
        sleep=FakeTime().sleep,
        stdout=out,
    )
    return code, out.getvalue()


def add_security_rise(guard_dir: Path) -> None:
    """A newer completed security run of Project A's first repository, with three High findings."""
    runs_path = guard_dir / "list_audit_runs.json"
    runs = json.loads(runs_path.read_text(encoding="utf-8"))
    runs["runs"].insert(
        0,
        {
            "task_id": C014,
            "action_key": "audit.security",
            "linked_at": "2026-11-20T09:30:00.000Z",
            "task_available": True,
            "status": "completed",
            "created_at": "2026-11-20T09:30:00.000Z",
            "completed_at": "2026-11-20T10:00:00.000000Z",
            "report_status": "ready",
        },
    )
    runs_path.write_text(json.dumps(runs), encoding="utf-8")
    page = report_page(C014, high=3, medium=1, score=25)
    (guard_dir / "get_audit_report_security_c014.json").write_text(
        json.dumps(page), encoding="utf-8"
    )
    calls_path = guard_dir / "calls.json"
    calls = json.loads(calls_path.read_text(encoding="utf-8"))
    calls["calls"].append(
        {
            "tool": TOOL_GET_AUDIT_REPORT,
            "args": {ARG_TASK_ID: C014},
            "response": "get_audit_report_security_c014.json",
        }
    )
    calls_path.write_text(json.dumps(calls), encoding="utf-8")


@pytest.fixture
def guard_copy(tmp_path: Path) -> Path:
    target = tmp_path / "guard"
    shutil.copytree(FIXTURES, target)
    return target


def test_new_security_run_with_more_highs_is_sent_once(tmp_path: Path, guard_copy: Path) -> None:
    transport = FakeTransport([])
    code, _ = run_cli(guard_copy, tmp_path, FIRST_POLL, transport)
    assert code == 0
    assert transport.requests == []
    state = load_state(tmp_path)
    (history,) = [h for h in state.histories if h.key() == (A_ID, B_REPO_ID, "audit.security")]
    assert history.open is True
    assert history.opened_at == datetime(2026, 11, 9, 23, 44, 19, 861187, tzinfo=UTC)
    assert [(p.task_id[-4:], p.high) for p in history.points] == [("c001", 1)]
    baseline = condition(state, A_ID, "high")
    assert (baseline.baseline, baseline.count) == (True, 1)

    add_security_rise(guard_copy)
    transport = FakeTransport([ok_response(9)])
    code, _ = run_cli(guard_copy, tmp_path, RISE_POLL, transport)
    assert code == 0
    assert len(transport.requests) == 1
    text = str(transport.requests[0][1]["text"])
    assert 'High findings: <b>Project A &lt;core&gt; &amp; "tools"</b>' in text
    assert "High findings rose from 1 to 3 in audit.security (acme/service-a)." in text
    assert "Seen by the radar since 2026-11-09 (11 days)." in text
    for leak in ("F-00", "Placeholder", "src/"):
        assert leak not in text
    record = condition(load_state(tmp_path), A_ID, "high")
    assert record.alerted_at is not None
    assert (record.baseline, record.count) == (False, 3)

    transport = FakeTransport([])
    code, _ = run_cli(guard_copy, tmp_path, RISE_POLL, transport)
    assert code == 0
    assert transport.requests == []


def test_dry_run_prints_rise_and_writes_no_state(tmp_path: Path, guard_copy: Path) -> None:
    run_cli(guard_copy, tmp_path, FIRST_POLL, FakeTransport([]))
    add_security_rise(guard_copy)
    state_dir = tmp_path / "state"

    def snapshot() -> dict[str, tuple[bytes, int]]:
        return {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(state_dir.glob("*"))
            if path.is_file()
        }

    before = snapshot()
    transport = FakeTransport([])
    code, out = run_cli(guard_copy, tmp_path, RISE_POLL, transport, dry_run=True)
    assert code == 0
    assert transport.requests == []
    lines = out.splitlines()
    sending = lines[lines.index("--- would send to group ---") :]
    assert "High findings rose from 1 to 3 in audit.security (acme/service-a)." in sending
    assert snapshot() == before


# ---- edge table: in-memory worlds through run_once -------------------------------------------

PID = "p-1"
PROJECT = "Proj"
RID = "repo_r-1"
REPO = "acme/svc"
RID2 = "repo_r-2"
REPO2 = "acme/other"
SECURITY = "audit.security"
BASE = datetime(2026, 11, 2, 10, tzinfo=UTC)
CALM_CONFIG = "defaults:\n  stale_after_days: 365\n"
CHAT_ID = "-1001234567890"


def stamp(day: int) -> str:
    return (BASE + timedelta(days=day)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


class Accepting:
    """A notifier that acknowledges everything."""

    def send(self, message: object) -> SendOutcome:
        return Delivered(message_id=1)


class Sim:
    """One project with a repository; every poll builds a fresh in-memory Guard world from it."""

    def __init__(self, tmp_path: Path, config_text: str = CALM_CONFIG) -> None:
        self.state_dir = tmp_path / "state"
        self.config_text = config_text
        self.repos: list[tuple[str, str]] = [(RID, REPO)]
        self.runs: dict[str, list[dict[str, object]]] = {RID: []}
        self.reports: dict[str, Mapping[str, object]] = {}
        self.failures: dict[Key, Response] = {}
        self.paused = False
        self.counter = 0
        # A recent overview run keeps the project fresh, so no stale alert gets in the way.
        self.add_run("audit.recon", day=0, report=False)

    def add_run(
        self,
        action_key: str = SECURITY,
        *,
        high: int = 0,
        day: int,
        report: bool = True,
        rid: str = RID,
    ) -> str:
        self.counter += 1
        task_id = f"t-{self.counter:04d}"
        self.runs.setdefault(rid, []).insert(0, run_item(task_id, action_key, stamp(day)))
        if report:
            self.reports[task_id] = report_page(task_id, high=high)
        return task_id

    @property
    def config(self) -> RadarConfig:
        text = self.config_text
        if self.paused:
            text += f'projects:\n  "{PID}":\n    mode: paused\n'
        return parse_config(text, source="test")

    def poll(
        self,
        day: int,
        *,
        notifier: Notifier | None = None,
        caplog: pytest.LogCaptureFixture | None = None,
    ) -> RunReport:
        guard = InMemoryGuardTools(
            world(
                [(PID, PROJECT)],
                {PID: self.repos},
                self.runs,
                self.reports,
                self.failures,
            )
        )
        now = BASE + timedelta(days=day, hours=2)
        deps = Deps(
            guard=guard,
            notifier=notifier or Accepting(),
            config=self.config,
            clock=lambda: now,
            store=FileStateStore(self.state_dir),
        )
        return run_once(deps)

    def state(self) -> State:
        load = FileStateStore(self.state_dir).load()
        assert isinstance(load, Loaded)
        return load.state

    def history(self, action_key: str = SECURITY, rid: str = RID) -> SeriesHistory:
        (found,) = [h for h in self.state().histories if h.key() == (PID, rid, action_key)]
        return found

    def high_condition(self) -> ConditionRecord | None:
        found = [c for c in self.state().conditions if c.key.level == "high"]
        assert len(found) <= 1
        return found[0] if found else None

    def high_record(self) -> ConditionRecord:
        record = self.high_condition()
        assert record is not None
        return record


def texts(report: RunReport) -> list[str]:
    return [message.text for message in report.messages]


def assert_quiet(report: RunReport) -> None:
    """Nothing was sent and nothing was resolved."""
    assert texts(report) == []
    assert report.resolved == ()


def frozen_view(state: State) -> bytes:
    """The histories and conditions of a state as bytes: they must not change when frozen."""
    return serialize_state(State(conditions=state.conditions, histories=state.histories))


def test_same_task_id_twice_changes_nothing(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.add_run(high=1, day=0)
    first = sim.poll(0)
    assert first.baselined
    assert texts(first) == []
    second = sim.poll(1)
    assert texts(second) == []
    assert len(sim.history().points) == 1


def test_unchanged_count_sends_nothing(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    assert len(texts(sim.poll(1))) == 1
    sim.add_run(high=3, day=2)
    assert texts(sim.poll(2)) == []
    assert len(sim.history().points) == 2


def test_first_sight_opens_sends_immediately(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    (text,) = texts(sim.poll(1))
    assert "2 High findings in audit.security (acme/svc)." in text
    assert "rose" not in text


def test_closed_series_reopens_as_a_rise_from_zero(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.add_run(high=0, day=0)
    sim.poll(0)
    assert sim.history().open is False
    sim.add_run(high=2, day=1)
    (text,) = texts(sim.poll(1))
    assert "High findings rose from 0 to 2 in audit.security (acme/svc)." in text
    assert "Seen by the radar since 2026-11-03" in text


def test_rise_n_to_n_plus_k_sends_once(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    sim.poll(1)
    sim.add_run(high=5, day=2)
    (text,) = texts(sim.poll(2))
    assert "High findings rose from 3 to 5 in audit.security (acme/svc)." in text


def test_three_two_three_alerts_again(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    assert len(texts(sim.poll(1))) == 1
    sim.add_run(high=2, day=2)
    assert texts(sim.poll(2)) == []
    assert sim.high_record().count == 2
    sim.add_run(high=3, day=3)
    (text,) = texts(sim.poll(3))
    assert "High findings rose from 2 to 3 in audit.security (acme/svc)." in text


def test_clean_runs_clear_after_m_and_a_new_high_opens_an_episode(tmp_path: Path) -> None:
    sim = Sim(tmp_path)  # M = 2 by default
    sim.poll(0)
    sim.add_run(high=3, day=1)
    sim.poll(1)
    sim.add_run(high=0, day=2)
    first_clean = sim.poll(2)
    assert_quiet(first_clean)
    assert sim.high_record().count == 0
    sim.add_run(high=0, day=3)
    second_clean = sim.poll(3)
    assert texts(second_clean) == []
    assert [key.level for key in second_clean.resolved] == ["high"]
    assert sim.high_condition() is None
    assert sim.history().open is False
    sim.add_run(high=1, day=4)
    (text,) = texts(sim.poll(4))
    assert "High findings rose from 0 to 1 in audit.security (acme/svc)." in text
    assert "Seen by the radar since 2026-11-06" in text


def test_flap_inside_debounce_sends_the_rise_but_keeps_the_episode(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    sim.poll(1)
    opened_at = sim.history().opened_at
    sim.add_run(high=0, day=2)
    dip = sim.poll(2)
    assert_quiet(dip)
    assert sim.high_condition() is not None
    sim.add_run(high=3, day=3)
    back = sim.poll(3)
    (text,) = texts(back)
    assert "High findings rose from 0 to 3 in audit.security (acme/svc)." in text
    assert "Seen by the radar since 2026-11-03" in text
    assert back.resolved == ()
    assert sim.history().opened_at == opened_at


def test_unknown_report_keeps_alert_and_is_read_again(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    sim.poll(1)
    before = sim.state()
    task_id = sim.add_run(day=2, report=False)
    unknown = sim.poll(2)
    assert_quiet(unknown)
    assert sim.history() == next(h for h in before.histories if h.action_key == SECURITY)
    record = sim.high_record()
    assert record.alerted_at is not None
    assert record.count == 3
    sim.reports[task_id] = report_page(task_id, high=3)
    assert texts(sim.poll(3)) == []
    assert len(sim.history().points) == 2


def test_missing_run_changes_nothing(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    sim.poll(1)
    before = sim.history()
    sim.runs[RID] = [run for run in sim.runs[RID] if run["action_key"] != SECURITY]
    report = sim.poll(2)
    assert_quiet(report)
    assert sim.history() == before
    assert sim.high_condition() is not None


def test_unknown_report_no_history_creates_nothing_and_is_read_again(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    task_id = sim.add_run(day=1, report=False)
    assert texts(sim.poll(1)) == []
    assert [h for h in sim.state().histories if h.action_key == SECURITY] == []
    assert sim.high_condition() is None
    sim.reports[task_id] = report_page(task_id, high=2)
    (text,) = texts(sim.poll(2))
    assert "2 High findings in audit.security (acme/svc)." in text


def test_partial_fetch_frozen_keeps_histories_and_conditions(tmp_path: Path) -> None:
    failing_listing: dict[Key, Response] = {
        call_key(TOOL_LIST_REPOSITORIES, **{ARG_PROJECT_ID: PID}): GuardCallError("outage")
    }
    broken_second_page: dict[Key, Response] = {
        call_key(TOOL_LIST_AUDIT_RUNS, **{ARG_REPOSITORY_ID: RID}): {
            "runs": [],
            "has_more": True,
            "next_cursor": "c2",
        },
        call_key(
            TOOL_LIST_AUDIT_RUNS, **{ARG_REPOSITORY_ID: RID, ARG_CURSOR: "c2"}
        ): GuardCallError("outage"),
    }
    for name, failures in (("repos", failing_listing), ("runs", broken_second_page)):
        sim = Sim(tmp_path / name)
        sim.poll(0)
        sim.add_run(high=3, day=1)
        sim.poll(1)
        before = frozen_view(sim.state())
        sim.failures = failures
        report = sim.poll(2)
        assert_quiet(report)
        assert frozen_view(sim.state()) == before


def test_paused_frozen_keeps_histories_and_conditions(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    sim.poll(1)
    before = frozen_view(sim.state())
    sim.paused = True
    sim.add_run(high=9, day=2)
    report = sim.poll(2)
    assert_quiet(report)
    assert frozen_view(sim.state()) == before


def test_scope_drops_history_and_resolves_the_condition_silently(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    sim.poll(1)
    assert sim.high_condition() is not None
    sim.config_text = CALM_CONFIG + "high:\n  audit_types:\n    - audit.tests\n"
    report = sim.poll(2)
    assert texts(report) == []
    assert [key.level for key in report.resolved] == ["high"]
    assert sim.state().histories == ()
    assert sim.high_condition() is None

    fresh = Sim(tmp_path / "fresh", CALM_CONFIG + "high:\n  audit_types:\n    - audit.tests\n")
    fresh.poll(0)
    fresh.add_run(high=3, day=1)
    assert texts(fresh.poll(1)) == []
    assert fresh.state().histories == ()


def test_removed_repo_drops_its_series_and_resolves(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.repos = [(RID, REPO), (RID2, REPO2)]
    sim.runs[RID2] = []
    sim.poll(0)
    sim.add_run(high=3, day=1, rid=RID2)
    (text,) = texts(sim.poll(1))
    assert "3 High findings in audit.security (acme/other)." in text
    sim.repos = [(RID, REPO)]
    del sim.runs[RID2]
    report = sim.poll(2)
    assert texts(report) == []
    assert [key.level for key in report.resolved] == ["high"]
    assert sim.state().histories == ()
    assert sim.high_condition() is None


def test_aged_no_send_without_a_rise(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    sim.poll(1)
    assert texts(sim.poll(40)) == []
    sim.add_run(high=5, day=41)
    (text,) = texts(sim.poll(41))
    assert "Long-standing: seen by the radar since 2026-11-03" in text
    assert "High findings rose from 3 to 5 in audit.security (acme/svc)." in text


def test_stale_and_high_of_one_project_are_two_messages(tmp_path: Path) -> None:
    sim = Sim(tmp_path, "defaults:\n  stale_after_days: 14\n")
    sim.poll(0)
    sim.add_run(high=2, day=1)
    report = sim.poll(20)
    assert len(report.messages) == 2
    levels = {key.level for message in report.messages for key in message.keys}
    assert levels == {"stale", "high"}
    assert any(text.startswith("Stale Guard scans:") for text in texts(report))
    assert any(text.startswith("High findings:") for text in texts(report))


def test_failed_send_retries_next_poll(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    def telegram(transport: FakeTransport) -> Notifier:
        return SecretGatedNotifier(
            TelegramNotifier(SecretStr(FAKE_TOKEN), CHAT_ID, transport, sleep=FakeTime().sleep),
            (FAKE_TOKEN,),
        )

    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    sim.poll(1)
    sim.add_run(high=5, day=2)
    failing = FakeTransport([error_response(500, 500, "Internal Server Error")])
    report = sim.poll(2, notifier=telegram(failing))
    assert report.outcome is RunOutcome.ATTENTION
    assert report.not_delivered == 1
    record = sim.high_record()
    assert record.alerted_at is None
    assert record.count == 5

    retry = FakeTransport([ok_response(3)])
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.pipeline"):
        report = sim.poll(3, notifier=telegram(retry))
    assert "retried 1, rearmed 0" in caplog.text
    assert len(retry.requests) == 1
    assert "High findings rose from 3 to 5 in audit.security (acme/svc)." in str(
        retry.requests[0][1]["text"]
    )
    assert sim.high_record().alerted_at is not None


def test_history_window_covers_m() -> None:
    assert HISTORY_WINDOW >= CLEAR_AFTER_MAX
