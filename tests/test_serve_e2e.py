"""`serve` through cli.main: fake clock, fake wait, fake Guard and Telegram. No network.

The wait function advances the fake clock; after the catch-up run and two scheduled slots it
raises a real SIGTERM, which the handler installed by `serve` turns into a clean stop.
"""

import io
import logging
import signal
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import pytest
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport

from enji_guard_radar import cli
from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.ledger import deserialize_state
from enji_guard_radar.ports import GuardSession
from enji_guard_radar.schedule import next_slot

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
ENVIRON = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "-1001234567890"}
START = datetime(2026, 11, 23, 2, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


class FixtureSession:
    def __init__(self) -> None:
        self._inner = FixtureGuardTools(FIXTURES)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        return self._inner.call(tool, args)

    def tool_names(self) -> frozenset[str]:
        return frozenset()

    def tool_arguments(self) -> Mapping[str, tuple[str, ...]]:
        return {}

    def result_shapes(self) -> Mapping[str, int]:
        return {}


class Harness:
    def __init__(self, tmp_path: Path, every_minutes: int = 180) -> None:
        self.state_dir = tmp_path / "state"
        self.tmp_path = tmp_path
        self.config = tmp_path / "radar.yaml"
        self.config.write_text(f"schedule:\n  every_minutes: {every_minutes}\n", encoding="utf-8")
        self.now = START
        self.sessions = 0
        self.stop_after_runs = 3
        self.out = io.StringIO()

    def clock(self) -> datetime:
        return self.now

    def connector(self, _state_dir: Path) -> GuardSession:
        self.sessions += 1
        return FixtureSession()

    def wait(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        if self.sessions >= self.stop_after_runs:
            signal.raise_signal(signal.SIGTERM)

    def main(self) -> int:
        return cli.main(
            [
                "serve",
                "--config",
                str(self.config),
                "--state-dir",
                str(self.state_dir),
                "--env-file",
                str(self.tmp_path / "none.env"),
            ],
            environ=ENVIRON,
            transport=FakeTransport([]),
            sleep=FakeTime().sleep,
            stdout=self.out,
            guard_connector=self.connector,
            serve_clock=self.clock,
            serve_wait=self.wait,
        )


def test_serve_runs_two_slots_then_stops_cleanly_on_sigterm(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    harness = Harness(tmp_path)

    code = harness.main()

    assert code == 0
    assert harness.sessions == 3  # catch-up at 02:00, then the 03:00 and 06:00 slots
    starts = [m for m in caplog.messages if m.startswith("serve: run start")]
    assert [m.split()[3] for m in starts] == [
        "slot=2026-11-23T02:00:00Z",
        "slot=2026-11-23T03:00:00Z",
        "slot=2026-11-23T06:00:00Z",
    ]
    assert "reason=catch-up" in starts[0]
    assert all("reason=scheduled" in m for m in starts[1:])
    assert len([m for m in caplog.messages if m.startswith("serve: run finish exit=0")]) == 3
    assert caplog.messages[-2:] == ["serve: stop requested (SIGTERM)", "serve: stopped"]
    state = deserialize_state((harness.state_dir / "state.json").read_bytes())
    assert state.schema_version == 3
    assert {s: signal.getsignal(s) for s in before} == before
    assert harness.out.getvalue().count("summary: ") == 3


def test_the_next_slot_in_the_log_is_on_the_grid(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    harness = Harness(tmp_path)
    harness.stop_after_runs = 1
    assert harness.main() == 0
    (finish,) = [m for m in caplog.messages if m.startswith("serve: run finish")]
    assert finish.endswith("next_slot=2026-11-23T03:00:00Z")
    assert next_slot(START, 180) == datetime(2026, 11, 23, 3, tzinfo=UTC)


def test_serve_rejects_a_bad_schedule_before_touching_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = Harness(tmp_path, every_minutes=7)
    assert harness.main() == 2
    assert "schedule.every_minutes" in capsys.readouterr().err
    assert not harness.state_dir.exists()
    assert harness.sessions == 0


def test_serve_reports_a_missing_config_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = Harness(tmp_path)
    harness.config.unlink()
    assert harness.main() == 2
    assert "config file not found" in capsys.readouterr().err
