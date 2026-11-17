"""Tracer: a live run on fixtures baselines, sends one alert, and never re-sends it.

Everything goes through cli.main with a fake Telegram transport: no network.
"""

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, ok_response

from enji_guard_radar import cli
from enji_guard_radar.ledger import ConditionRecord, State, deserialize_state
from enji_guard_radar.telegram import HttpTransport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
BASELINE_NOW = "2026-11-16T12:00:00Z"
STALE_NOW = "2026-12-01T12:00:00Z"
A_ID = "00000000-0000-4000-8000-00000000a001"
ENVIRON = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "-1001234567890"}


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def run_live(
    tmp_path: Path, now: str, transport: HttpTransport, *, state_dir: Path | None = None
) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(
        [
            "run",
            "--fixtures",
            str(FIXTURES),
            "--config",
            str(FIXTURES / "radar.yaml"),
            "--state-dir",
            str(state_dir or tmp_path / "state"),
            "--env-file",
            str(tmp_path / "none.env"),
            "--now",
            now,
        ],
        environ=ENVIRON,
        transport=transport,
        sleep=FakeTime().sleep,
        stdout=out,
    )
    return code, out.getvalue()


def load_state(tmp_path: Path) -> State:
    return deserialize_state((tmp_path / "state" / "state.json").read_bytes())


def condition(state: State, project_id: str, level: str) -> ConditionRecord:
    (record,) = [
        c for c in state.conditions if c.key.project_id == project_id and c.key.level == level
    ]
    return record


def test_first_live_run_baselines_and_sends_nothing(tmp_path: Path) -> None:
    transport = FakeTransport([])
    code, out = run_live(tmp_path, BASELINE_NOW, transport)
    assert code == 0
    assert transport.requests == []
    assert load_state(tmp_path).schema_version == 2
    assert "2 baselined" in out


def test_newly_stale_project_is_sent_once(tmp_path: Path) -> None:
    run_live(tmp_path, BASELINE_NOW, FakeTransport([]))

    transport = FakeTransport([ok_response(7)])
    code, _ = run_live(tmp_path, STALE_NOW, transport)
    assert code == 0
    assert len(transport.requests) == 1
    text = str(transport.requests[0][1]["text"])
    assert 'Stale Guard scans: <b>Project A &lt;core&gt; &amp; "tools"</b>' in text
    assert "No completed Guard run for 20 days (last: 2026-11-10)." in text
    assert condition(load_state(tmp_path), A_ID, "stale").alerted_at is not None

    again = FakeTransport([])
    code, _ = run_live(tmp_path, STALE_NOW, again)
    assert code == 0
    assert again.requests == []


def test_live_runs_never_print_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        _, first = run_live(tmp_path, BASELINE_NOW, FakeTransport([]))
        _, second = run_live(tmp_path, STALE_NOW, FakeTransport([ok_response()]))
        _, third = run_live(tmp_path, STALE_NOW, FakeTransport([]))
    captured = capsys.readouterr()
    everything = captured.out + captured.err + caplog.text + first + second + third
    assert FAKE_TOKEN not in everything
