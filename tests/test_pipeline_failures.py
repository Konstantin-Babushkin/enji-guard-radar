"""Run-level failure paths: corrupt state, held lock, failed sends, Telegram errors, secrets.

Everything uses fake transports and fixture copies: no network, no real secrets.
"""

import io
import json
import logging
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from pydantic import SecretStr
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, error_response, ok_response

from enji_guard_radar import cli
from enji_guard_radar.cli import read_config
from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard import parse_guard_time
from enji_guard_radar.ledger import State, StateLoad, deserialize_state
from enji_guard_radar.model import AlertKey, OutboundMessage
from enji_guard_radar.pipeline import Deps, RunOutcome, run_once
from enji_guard_radar.ports import Delivered, GuardTools, SendOutcome
from enji_guard_radar.secret_guard import SecretGatedNotifier
from enji_guard_radar.settings import ENV_OPS_CHAT_ID
from enji_guard_radar.state_store import FileStateStore
from enji_guard_radar.telegram import HttpTransport, TelegramNotifier

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
BASELINE_NOW = "2026-11-16T12:00:00Z"
STALE_NOW = "2026-12-01T12:00:00Z"
A_ID = "00000000-0000-4000-8000-00000000a001"
CHAT_ID = "-1001234567890"
OPS_CHAT_ID = "-1009990001"
ENVIRON = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": CHAT_ID}


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

    def __init__(self, tmp_path: Path, fixtures: Path = FIXTURES) -> None:
        self.tmp_path = tmp_path
        self.fixtures = fixtures
        self.state_dir = tmp_path / "state"
        self.state_file = self.state_dir / "state.json"

    def run(
        self,
        now: str,
        transport: HttpTransport | None = None,
        *,
        environ: Mapping[str, str] = ENVIRON,
        env_file: Path | None = None,
        sleep: FakeTime | None = None,
    ) -> int:
        return cli.main(
            [
                "run",
                "--fixtures",
                str(self.fixtures),
                "--config",
                str(FIXTURES / "radar.yaml"),
                "--state-dir",
                str(self.state_dir),
                "--env-file",
                str(env_file or self.tmp_path / "none.env"),
                "--now",
                now,
            ],
            environ=environ,
            transport=transport if transport is not None else FakeTransport([]),
            sleep=(sleep or FakeTime()).sleep,
            stdout=io.StringIO(),
        )

    def baseline(self) -> None:
        assert self.run(BASELINE_NOW) == 0

    def state(self) -> State:
        return deserialize_state(self.state_file.read_bytes())

    def alerted_at(self) -> object:
        (record,) = [
            c
            for c in self.state().conditions
            if c.key.project_id == A_ID and c.key.level == "stale"
        ]
        return record.alerted_at

    def snapshot(self) -> dict[str, tuple[bytes, int]]:
        return {
            path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in sorted(self.state_dir.glob("*"))
            if path.is_file()
        }


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


class RecordingGuard:
    def __init__(self, inner: GuardTools) -> None:
        self.inner = inner
        self.calls: list[str] = []

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append(tool)
        return self.inner.call(tool, args)


def pipeline_deps(
    env: Env, group: FakeTransport, ops: FakeTransport | None = None
) -> tuple[Deps, RecordingGuard]:
    guard = RecordingGuard(FixtureGuardTools(FIXTURES))
    pause = FakeTime().sleep
    notifier = SecretGatedNotifier(
        TelegramNotifier(SecretStr(FAKE_TOKEN), CHAT_ID, group, sleep=pause), (FAKE_TOKEN,)
    )
    ops_notifier = (
        SecretGatedNotifier(
            TelegramNotifier(
                SecretStr(FAKE_TOKEN), OPS_CHAT_ID, ops, chat_env_name=ENV_OPS_CHAT_ID, sleep=pause
            ),
            (FAKE_TOKEN,),
        )
        if ops is not None
        else None
    )
    deps = Deps(
        guard=guard,
        notifier=notifier,
        config=read_config(FIXTURES / "radar.yaml"),
        clock=lambda: parse_guard_time(STALE_NOW),
        store=FileStateStore(env.state_dir),
        ops_notifier=ops_notifier,
    )
    return deps, guard


def write_corrupt(env: Env) -> None:
    env.state_dir.mkdir()
    env.state_file.write_bytes(b"{")


# ---- corrupt state ---------------------------------------------------------------------------


def test_corrupt_state_halts_before_guard_and_group(env: Env) -> None:
    write_corrupt(env)
    group, ops = FakeTransport([]), FakeTransport([ok_response()])
    deps, guard = pipeline_deps(env, group, ops)

    report = run_once(deps)

    assert report.outcome is RunOutcome.HALTED
    assert report.exit_code == 2
    assert guard.calls == []
    assert group.requests == []
    assert len(ops.requests) == 1
    assert env.state_file.read_bytes() == b"{"


def test_corrupt_state_exit_2_names_state_file_and_sends_one_ops_message(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    write_corrupt(env)
    transport = FakeTransport([ok_response()])

    code = env.run(STALE_NOW, transport, environ={**ENVIRON, "TELEGRAM_OPS_CHAT_ID": OPS_CHAT_ID})

    assert code == 2
    err = capsys.readouterr().err
    assert "state.json" in err
    assert "backup valid" in err
    assert FAKE_TOKEN not in err
    assert len(transport.requests) == 1
    payload = transport.requests[0][1]
    assert payload["chat_id"] == int(OPS_CHAT_ID)
    assert str(payload["text"]).startswith("Guard radar halted")
    assert env.state_file.read_bytes() == b"{"


def test_corrupt_state_without_ops_chat_sends_nothing(env: Env) -> None:
    write_corrupt(env)
    transport = FakeTransport([])
    assert env.run(STALE_NOW, transport) == 2
    assert transport.requests == []


def test_corrupt_state_missing_file_with_backup_halts(env: Env) -> None:
    env.baseline()
    env.run(BASELINE_NOW)  # a second save leaves state.json.bak next to state.json
    env.state_file.unlink()
    deps, guard = pipeline_deps(env, FakeTransport([]))

    assert run_once(deps).outcome is RunOutcome.HALTED
    assert guard.calls == []
    assert env.run(STALE_NOW) == 2
    assert not env.state_file.exists()


def test_corrupt_state_during_run_keeps_corrupt_bytes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.baseline()

    class CorruptsAfterObservation(FileStateStore):
        def __init__(self, directory: Path) -> None:
            super().__init__(directory)
            self.saves = 0

        def save(self, state: State) -> None:
            super().save(state)
            self.saves += 1
            if self.saves == 1:
                self.state_path.write_bytes(b"{")

    monkeypatch.setattr(cli, "FileStateStore", CorruptsAfterObservation)

    assert env.run(STALE_NOW, FakeTransport([ok_response()])) == 2
    assert env.state_file.read_bytes() == b"{"

    monkeypatch.undo()
    assert env.run(STALE_NOW) == 2
    assert env.state_file.read_bytes() == b"{"


# ---- lock ------------------------------------------------------------------------------------


def test_lock_held_exits_zero_without_work(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    env.baseline()
    before = env.snapshot()
    transport = FakeTransport([])

    with FileStateStore(env.state_dir).lock() as acquired, caplog.at_level(logging.INFO):
        assert acquired
        code = env.run(STALE_NOW, transport)
        deps, guard = pipeline_deps(env, transport)
        report = run_once(deps)

    assert code == 0
    assert report.outcome is RunOutcome.LOCKED
    assert guard.calls == []
    assert transport.requests == []
    assert env.snapshot() == before
    assert "another radar run holds the state lock" in caplog.text


# ---- failed and blocked sends ----------------------------------------------------------------


def test_failed_send_leaves_alert_unmarked_and_next_run_retries(env: Env) -> None:
    env.baseline()

    failing = FakeTransport([error_response(500, 500, "Internal Server Error")])
    assert env.run(STALE_NOW, failing) == 1
    assert len(failing.requests) == 1
    assert env.alerted_at() is None

    retry = FakeTransport([ok_response()])
    assert env.run(STALE_NOW, retry) == 0
    assert len(retry.requests) == 1
    assert env.alerted_at() is not None


def test_secret_blocked_message_is_withheld_and_stays_unmarked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    listing = fixtures / "list_projects.json"
    token_like = "123456789:" + "x" * 35
    listing.write_text(listing.read_text().replace("Project A", f"Project {token_like}"))
    env = Env(tmp_path, fixtures)
    env.baseline()

    transport = FakeTransport([])
    assert env.run(STALE_NOW, transport) == 1

    assert transport.requests == []
    assert env.alerted_at() is None
    err = capsys.readouterr().err
    assert "message withheld" in err
    assert token_like not in err


@pytest.mark.parametrize("status", [401, 404])
def test_unauthorized_token_stops_and_never_prints_the_token(
    env: Env, capsys: pytest.CaptureFixture[str], status: int
) -> None:
    env.baseline()
    transport = FakeTransport([error_response(status, status, "Unauthorized")])

    assert env.run(STALE_NOW, transport) == 1

    assert len(transport.requests) == 1
    captured = capsys.readouterr()
    assert "rejected the bot token" in captured.err
    assert FAKE_TOKEN not in captured.out + captured.err
    assert env.alerted_at() is None


def test_forbidden_chat_ends_the_run_with_a_clear_error(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.baseline()
    transport = FakeTransport([error_response(403, 403, "Forbidden: bot was kicked")])

    assert env.run(STALE_NOW, transport) == 1

    assert "refused to post" in capsys.readouterr().err
    assert env.alerted_at() is None


def test_forbidden_group_send_sends_one_ops_message_but_unauthorized_does_not(env: Env) -> None:
    env.baseline()
    environ = {**ENVIRON, "TELEGRAM_OPS_CHAT_ID": OPS_CHAT_ID}

    forbidden = FakeTransport([error_response(403, 403, "Forbidden"), ok_response()])
    assert env.run(STALE_NOW, forbidden, environ=environ) == 1
    assert len(forbidden.requests) == 2
    ops_request = forbidden.requests[1][1]
    assert ops_request["chat_id"] == int(OPS_CHAT_ID)
    assert str(ops_request["text"]).startswith("Guard radar needs attention:")
    assert "refused to post" in str(ops_request["text"])

    unauthorized = FakeTransport([error_response(401, 401, "Unauthorized")])
    assert env.run(STALE_NOW, unauthorized, environ=environ) == 1
    assert len(unauthorized.requests) == 1


def test_migrated_chat_is_followed_and_reported(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.baseline()
    transport = FakeTransport(
        [
            error_response(
                400,
                400,
                "Bad Request: group chat was upgraded to a supergroup chat",
                {"migrate_to_chat_id": -1009876543210},
            ),
            ok_response(),
        ]
    )

    assert env.run(STALE_NOW, transport) == 1

    assert len(transport.requests) == 2
    assert transport.requests[1][1]["chat_id"] == -1009876543210
    assert env.alerted_at() is not None
    err = capsys.readouterr().err
    assert "-1009876543210" in err
    assert "TELEGRAM_CHAT_ID" in err


def test_rate_limited_send_is_left_for_the_next_run_without_a_long_sleep(env: Env) -> None:
    env.baseline()
    pause = FakeTime()
    transport = FakeTransport(
        [error_response(429, 429, "Too Many Requests: retry after 60", {"retry_after": 60})]
    )

    assert env.run(STALE_NOW, transport, sleep=pause) == 1

    assert 60.0 not in pause.sleeps
    assert env.alerted_at() is None


# ---- settings --------------------------------------------------------------------------------


class GuardCallCounter:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, directory: Path) -> GuardTools:
        counter = self

        class Counting:
            inner = FixtureGuardTools(directory)

            def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
                counter.calls += 1
                return self.inner.call(tool, args)

        return Counting()


@pytest.mark.parametrize(
    ("environ", "missing"),
    [
        ({"TELEGRAM_CHAT_ID": CHAT_ID}, "TELEGRAM_BOT_TOKEN"),
        ({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN}, "TELEGRAM_CHAT_ID"),
    ],
)
def test_missing_token_or_chat_id_exits_2_before_guard(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
    missing: str,
) -> None:
    counter = GuardCallCounter()
    monkeypatch.setattr(cli, "FixtureGuardTools", counter)
    transport = FakeTransport([])

    assert env.run(STALE_NOW, transport, environ=environ) == 2

    err = capsys.readouterr().err
    assert missing in err
    assert FAKE_TOKEN not in err
    assert counter.calls == 0
    assert transport.requests == []
    assert not env.state_dir.exists()


def test_dry_run_without_telegram_variables_works(env: Env) -> None:
    out = io.StringIO()
    code = cli.main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(FIXTURES),
            "--state-dir",
            str(env.state_dir),
            "--env-file",
            str(env.tmp_path / "none.env"),
            "--now",
            BASELINE_NOW,
        ],
        environ={},
        stdout=out,
    )
    assert code == 0
    assert "summary:" in out.getvalue()
    assert not env.state_dir.exists()


def test_env_file_supplies_the_token_and_the_environment_overrides_it(env: Env) -> None:
    env.baseline()
    dotenv = env.tmp_path / "radar.env"
    dotenv.write_text(
        f"TELEGRAM_BOT_TOKEN=111111:from-the-env-file-aaaaaaaaaaaaaaaaaaaa\nTELEGRAM_CHAT_ID={CHAT_ID}\n"
    )

    from_file = FakeTransport([ok_response()])
    assert env.run(STALE_NOW, from_file, environ={}, env_file=dotenv) == 0
    assert "111111:from-the-env-file" in from_file.requests[0][0]

    # Same alert again: it is marked now, so reset by starting from a fresh baseline.
    fresh = Env(env.tmp_path / "second")
    fresh.tmp_path.mkdir()
    fresh.baseline()
    overridden = FakeTransport([ok_response()])
    assert fresh.run(STALE_NOW, overridden, environ=ENVIRON, env_file=dotenv) == 0
    assert FAKE_TOKEN in overridden.requests[0][0]
    assert "111111:from-the-env-file" not in overridden.requests[0][0]


# ---- Guard listing ---------------------------------------------------------------------------


def test_zero_project_listing_is_guard_unavailable(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.baseline()
    before = env.snapshot()

    empty = tmp_path / "empty"
    shutil.copytree(FIXTURES, empty)
    (empty / "list_projects.json").write_text(json.dumps({"projects": [], "has_more": False}))
    env.fixtures = empty
    transport = FakeTransport([])

    assert env.run(STALE_NOW, transport) == 1
    assert transport.requests == []
    assert env.snapshot() == before


def test_guard_listing_failure_exits_1_and_keeps_state(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.baseline()
    before = env.state_file.read_bytes()

    broken = tmp_path / "broken"
    shutil.copytree(FIXTURES, broken)
    manifest = json.loads((broken / "calls.json").read_text())
    for call in manifest["calls"]:
        if call["tool"] == "list_projects":
            del call["response"]
            call["error"] = "simulated Guard outage"
    (broken / "calls.json").write_text(json.dumps(manifest))
    env.fixtures = broken

    assert env.run(STALE_NOW) == 1
    assert env.state_file.read_bytes() == before


# ---- crash window and ordering ---------------------------------------------------------------


def test_crash_between_ack_and_save_resends_once(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """At-least-once: a crash after Telegram acknowledged but before the save re-sends once.

    The alert is acknowledged, the save of that acknowledgement fails (the process "crashes"),
    so the next run still sees it as unsent and sends the same message again: one duplicate.
    A pre-send intent marker would turn duplicates into losses and is deliberately not built.
    """
    env.baseline()

    class CrashesOnDeliverySave(FileStateStore):
        def __init__(self, directory: Path) -> None:
            super().__init__(directory)
            self.saves = 0

        def save(self, state: State) -> None:
            self.saves += 1
            if self.saves == 2:
                message = "simulated crash"
                raise OSError(message)
            super().save(state)

    monkeypatch.setattr(cli, "FileStateStore", CrashesOnDeliverySave)
    first = FakeTransport([ok_response()])
    assert env.run(STALE_NOW, first) == 2
    assert len(first.requests) == 1
    assert env.alerted_at() is None

    monkeypatch.undo()
    second = FakeTransport([ok_response()])
    assert env.run(STALE_NOW, second) == 0
    assert len(second.requests) == 1
    assert second.requests[0][1]["text"] == first.requests[0][1]["text"]
    assert env.alerted_at() is not None


def test_alert_is_marked_only_after_telegram_acknowledges(env: Env) -> None:
    env.baseline()
    events: list[str] = []

    class LoggingStore(FileStateStore):
        def load(self) -> StateLoad:
            events.append("load")
            return super().load()

        def save(self, state: State) -> None:
            marked = any(c.alerted_at is not None for c in state.conditions)
            events.append("save-marked" if marked else "save")
            super().save(state)

    class LoggingNotifier:
        def send(self, message: OutboundMessage) -> SendOutcome:
            events.append("send")
            return Delivered(message_id=1)

    deps, _ = pipeline_deps(env, FakeTransport([]))
    deps = Deps(
        guard=deps.guard,
        notifier=LoggingNotifier(),
        config=deps.config,
        clock=deps.clock,
        store=LoggingStore(env.state_dir),
    )

    run_once(deps)

    assert events == ["load", "save", "send", "save-marked"]
    assert AlertKey(project_id=A_ID, level="stale") in {c.key for c in env.state().conditions}
