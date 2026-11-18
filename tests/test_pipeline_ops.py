"""Poll health and the ops chat (OPS-02): one outage message after K failed polls, one recovery.

Everything uses fake transports and fixture copies: no network, no real secrets.
"""

import io
import json
import logging
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

import pytest
from guard_fakes import InMemoryGuardTools, call_key, projects_page
from pydantic import SecretStr
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, error_response, ok_response

from enji_guard_radar import cli
from enji_guard_radar.config import RadarConfig
from enji_guard_radar.guard import (
    ARG_PROJECT_ID,
    TOOL_LIST_PROJECTS,
    TOOL_LIST_REPOSITORIES,
    parse_guard_time,
)
from enji_guard_radar.ledger import PollHealth, State, deserialize_state, serialize_state
from enji_guard_radar.pipeline import Deps, RunOutcome, run_once
from enji_guard_radar.ports import AuthFailureKind, GuardAuthError
from enji_guard_radar.state_store import FileStateStore
from enji_guard_radar.telegram import HttpTransport, TelegramNotifier

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
CHAT_ID = "-1001234567890"
OPS_CHAT_ID = "-1009990001"
ENVIRON = {
    "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
    "TELEGRAM_CHAT_ID": CHAT_ID,
    "TELEGRAM_OPS_CHAT_ID": OPS_CHAT_ID,
}
NO_OPS_ENVIRON = {k: v for k, v in ENVIRON.items() if k != "TELEGRAM_OPS_CHAT_ID"}
OUTAGE_PREFIX = "Guard radar cannot read Guard: Guard is unreachable or failing."
RECOVERY_TEXT = (
    "Guard radar recovered: Guard polls work again (failing since 2026-11-16 00:00 UTC)."
)
T0 = "2026-11-16T00:00:00Z"
T3 = "2026-11-16T03:00:00Z"
T6 = "2026-11-16T06:00:00Z"
T9 = "2026-11-16T09:00:00Z"
T12 = "2026-11-16T12:00:00Z"


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


class Env:
    """One test's world: a state directory, a fixtures directory and a way to call the CLI."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.good = FIXTURES
        self.broken = tmp_path / "broken"
        shutil.copytree(FIXTURES, self.broken)
        manifest = json.loads((self.broken / "calls.json").read_text())
        for call in manifest["calls"]:
            if call["tool"] == "list_projects":
                del call["response"]
                call["error"] = "simulated Guard outage"
        (self.broken / "calls.json").write_text(json.dumps(manifest))
        self.fixtures = self.broken
        self.state_dir = tmp_path / "state"
        self.state_file = self.state_dir / "state.json"

    def run(
        self,
        now: str,
        transport: HttpTransport | None = None,
        *,
        environ: Mapping[str, str] = ENVIRON,
        dry_run: bool = False,
        out: io.StringIO | None = None,
        config: Path | None = None,
    ) -> int:
        argv = [
            "run",
            "--fixtures",
            str(self.fixtures),
            "--config",
            str(config or FIXTURES / "radar.yaml"),
            "--state-dir",
            str(self.state_dir),
            "--env-file",
            str(self.tmp_path / "none.env"),
            "--now",
            now,
        ]
        if dry_run:
            argv.append("--dry-run")
        return cli.main(
            argv,
            environ=environ,
            transport=transport if transport is not None else FakeTransport([]),
            sleep=FakeTime().sleep,
            stdout=out if out is not None else io.StringIO(),
        )

    def health(self) -> PollHealth:
        return deserialize_state(self.state_file.read_bytes()).health

    def state(self) -> State:
        return deserialize_state(self.state_file.read_bytes())

    def seed_health(self, health: PollHealth, state: State | None = None) -> None:
        self.state_dir.mkdir(exist_ok=True)
        base = state if state is not None else State.empty()
        self.state_file.write_bytes(serialize_state(base.model_copy(update={"health": health})))

    def seed_failures(self, count: int) -> None:
        """Write a state that already holds `count` failed polls, none reported."""
        self.seed_health(
            PollHealth(
                consecutive_failures=count,
                first_failure_at=parse_guard_time(T0),
                last_failure_cause="unavailable",
            )
        )

    def snapshot(self) -> dict[str, tuple[bytes, int]]:
        return {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(self.state_dir.glob("*"))
            if path.is_file()
        }


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def texts(transport: FakeTransport) -> list[str]:
    return [str(payload["text"]) for _, payload in transport.requests]


def test_k_failed_polls_send_one_outage_and_one_recovery(env: Env) -> None:
    for number, now in enumerate((T0, T3), start=1):
        transport = FakeTransport([])
        assert env.run(now, transport) == 1
        assert transport.requests == []
        health = env.health()
        assert health.consecutive_failures == number
        assert health.outage_reported_at is None

    third = FakeTransport([ok_response()])
    assert env.run(T6, third) == 1
    assert len(third.requests) == 1
    payload = third.requests[0][1]
    assert payload["chat_id"] == int(OPS_CHAT_ID)
    assert str(payload["text"]).startswith(OUTAGE_PREFIX)
    assert "3 failed polls in a row" in str(payload["text"])
    health = env.health()
    assert health.consecutive_failures == 3
    assert health.outage_reported_at is not None
    assert health.last_failure_cause == "unavailable"

    fourth = FakeTransport([])
    assert env.run(T9, fourth) == 1
    assert fourth.requests == []
    assert env.health().consecutive_failures == 4

    env.fixtures = env.good
    fifth = FakeTransport([ok_response()])
    assert env.run(T12, fifth) == 0
    assert len(fifth.requests) == 1
    assert fifth.requests[0][1]["chat_id"] == int(OPS_CHAT_ID)
    assert str(fifth.requests[0][1]["text"]).startswith(RECOVERY_TEXT)
    health = env.health()
    assert health == PollHealth(last_success_at=health.last_success_at)
    assert health.last_success_at is not None

    sixth = FakeTransport([])
    assert env.run("2026-11-16T15:00:00Z", sixth) == 0
    assert sixth.requests == []


def _fixed(moment: datetime) -> datetime:
    return moment


def fail_polls(env: Env, count: int) -> None:
    for step in range(count):
        transport = FakeTransport([ok_response()])  # only the K-th poll may use it
        assert env.run(f"2026-11-16T{3 * step:02d}:00:00Z", transport) == 1


def test_ops_send_fails_then_next_failed_poll_sends_again_and_marks(env: Env) -> None:
    fail_polls(env, 2)

    failing = FakeTransport([error_response(500, 500, "Internal Server Error")])
    assert env.run(T6, failing) == 1
    assert len(failing.requests) == 1
    assert env.health().consecutive_failures == 3
    assert env.health().outage_reported_at is None

    retry = FakeTransport([ok_response()])
    assert env.run(T9, retry) == 1
    assert len(retry.requests) == 1
    assert "4 failed polls in a row" in str(retry.requests[0][1]["text"])
    assert env.health().outage_reported_at is not None

    quiet = FakeTransport([])
    assert env.run(T12, quiet) == 1
    assert quiet.requests == []


def test_no_ops_chat_names_the_variable_and_marks_nothing(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    for now in (T0, T3, T6):
        transport = FakeTransport([])
        assert env.run(now, transport, environ=NO_OPS_ENVIRON) == 1
        assert transport.requests == []
    assert "ops chat not configured (TELEGRAM_OPS_CHAT_ID)" in capsys.readouterr().err
    assert env.health().consecutive_failures == 3
    assert env.health().outage_reported_at is None


def test_dry_run_prints_the_ops_message_and_writes_nothing(env: Env) -> None:
    env.seed_failures(2)
    before = env.snapshot()
    transport = FakeTransport([])
    out = io.StringIO()

    assert env.run(T6, transport, dry_run=True, out=out) == 1

    printed = out.getvalue()
    assert "--- would send to ops ---" in printed
    assert "Guard radar cannot read Guard: Guard is unreachable or failing." in printed
    assert "3 failed polls in a row" in printed
    assert transport.requests == []
    assert env.snapshot() == before


def test_mixed_poll_after_failures_resets_counters_without_an_ops_message(env: Env) -> None:
    fail_polls(env, 2)
    env.fixtures = env.good  # Project D stays unknown in the fixtures, the others work
    transport = FakeTransport([])

    assert env.run(T6, transport) == 0

    assert transport.requests == []
    health = env.health()
    assert health.consecutive_failures == 0
    assert health.outage_reported_at is None
    assert health.last_success_at == parse_guard_time(T6)


def test_k_of_one_first_failed_poll_sends_the_outage_message(env: Env, tmp_path: Path) -> None:
    config = tmp_path / "k1.yaml"
    config.write_text("ops:\n  failures_before_alert: 1\n")
    transport = FakeTransport([ok_response()])

    assert env.run(T0, transport, config=config) == 1

    assert len(transport.requests) == 1
    assert "1 failed polls in a row" in str(transport.requests[0][1]["text"])


def test_poll_log_lines_name_the_branch(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    fail_polls(env, 3)
    assert "poll failed (unavailable): 3 in a row" in caplog.text
    env.fixtures = env.good
    assert env.run(T9, FakeTransport([ok_response()])) == 0
    assert "poll ok" in caplog.text
    assert "ops chat told: Guard recovery" in caplog.text


def test_group_alerts_go_before_the_ops_recovery_and_both_are_marked(env: Env) -> None:
    env.fixtures = env.good
    assert env.run("2026-11-16T12:00:00Z") == 0  # baseline everything
    state = env.state()
    env.seed_health(
        PollHealth(
            consecutive_failures=3,
            first_failure_at=parse_guard_time(T0),
            last_failure_cause="unavailable",
            outage_reported_at=parse_guard_time(T6),
        ),
        state,
    )
    transport = FakeTransport([ok_response(n) for n in range(1, 10)])

    assert env.run("2026-12-01T12:00:00Z", transport) in (0, 1)

    chats = [payload["chat_id"] for _, payload in transport.requests]
    assert chats[-1] == int(OPS_CHAT_ID)
    assert chats[:-1]
    assert set(chats[:-1]) == {int(CHAT_ID)}
    assert str(transport.requests[-1][1]["text"]).startswith("Guard radar recovered")
    health = env.health()
    assert health.outage_reported_at is None
    assert health.first_failure_at is None
    assert all(c.alerted_at is not None for c in env.state().conditions if not c.baseline)


def test_all_active_projects_auth_expired_fails_the_poll_and_names_the_command() -> None:
    tools = InMemoryGuardTools(
        {
            call_key(TOOL_LIST_PROJECTS): projects_page(("p1", "One"), ("p2", "Two")),
            call_key(TOOL_LIST_REPOSITORIES, **{ARG_PROJECT_ID: "p1"}): GuardAuthError(
                AuthFailureKind.AUTH_EXPIRED, "expired"
            ),
            call_key(TOOL_LIST_REPOSITORIES, **{ARG_PROJECT_ID: "p2"}): GuardAuthError(
                AuthFailureKind.AUTH_EXPIRED, "expired"
            ),
        }
    )
    ops = FakeTransport([ok_response()])
    group = FakeTransport([])
    pause = FakeTime().sleep
    notifier = TelegramNotifier(SecretStr(FAKE_TOKEN), CHAT_ID, group, sleep=pause)
    ops_notifier = TelegramNotifier(
        SecretStr(FAKE_TOKEN), OPS_CHAT_ID, ops, chat_env_name="TELEGRAM_OPS_CHAT_ID", sleep=pause
    )
    with tempfile.TemporaryDirectory() as directory:
        store = FileStateStore(Path(directory))
        reports = []
        for step in range(3):
            moment = parse_guard_time(T0) + timedelta(hours=3 * step)
            deps = Deps(
                guard=tools,
                notifier=notifier,
                config=RadarConfig(),
                clock=partial(_fixed, moment),
                store=store,
                ops_notifier=ops_notifier,
            )
            reports.append(run_once(deps))

    assert [r.exit_code for r in reports] == [1, 1, 1]
    assert all(r.outcome is RunOutcome.GUARD_UNAVAILABLE and r.poll_failed for r in reports)
    assert [r.ops_sent for r in reports] == [0, 0, 1]
    assert group.requests == []
    assert len(ops.requests) == 1
    text = str(ops.requests[0][1]["text"])
    assert "Guard login expired or revoked: run `enji-guard-radar auth`" in text
