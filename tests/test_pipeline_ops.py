"""Poll health and the ops chat (OPS-02): one outage message after K failed polls, one recovery.

Everything uses fake transports and fixture copies: no network, no real secrets.
"""

import io
import json
import logging
import shutil
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, ok_response

from enji_guard_radar import cli
from enji_guard_radar.ledger import PollHealth, State, deserialize_state, serialize_state
from enji_guard_radar.telegram import HttpTransport

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
    ) -> int:
        argv = [
            "run",
            "--fixtures",
            str(self.fixtures),
            "--config",
            str(FIXTURES / "radar.yaml"),
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

    def seed_failures(self, count: int, *, since: str = T0) -> None:
        """Write a state that already holds `count` failed polls (none reported)."""
        from enji_guard_radar.guard import parse_guard_time

        first = parse_guard_time(since)
        health = PollHealth(
            consecutive_failures=count, first_failure_at=first, last_failure_cause="unavailable"
        )
        self.state_dir.mkdir(exist_ok=True)
        self.state_file.write_bytes(serialize_state(State(health=health)))

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
