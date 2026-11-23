"""End to end: the radar CLI with triage on, a fake `claude` first on PATH, fixtures, no network.

Every run starts from a state seeded by a live baseline at DEMO_NOW from which Project A's
security High series is removed, so a run at LATER_NOW sees a fresh High alert (the only kind
that is triaged). The token lives only in an env file; the fake records what it was given.
"""

import io
import logging
import os
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from fake_claude import SESSION_ID, error_envelope, install_fake_claude, ok_envelope, with_path
from telegram_fakes import FakeTime, FakeTransport, ok_response
from test_dry_run_e2e import (
    DEMO_NOW,
    FIXTURES,
    LATER_NOW,
    LIVE_ENVIRON,
    seed_live_baseline,
    state_snapshot,
)
from test_language_e2e import LAUNCH, without_timing

from enji_guard_radar import cli
from enji_guard_radar.ledger import deserialize_state, serialize_state
from enji_guard_radar.triage_cost_store import COST_FILE, FileTriageCostStore

TOKEN = "fake-claude-oauth-token-for-the-radar-e2e"
CAUSE = "Injection flaw in order search"
P1_LINE = f"   P1 · {CAUSE}"
FIXTURE_TITLE = "Placeholder high-severity finding title"
TRIAGE_ON = "triage:\n  enabled: true\n"
ALERT_PREFIX = "🔴 <b>Project A"
FAKE_CALLED = "record.json"


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def seed_fresh_high(tmp_path: Path) -> Path:
    """A live baseline, then Project A's security High condition and history are forgotten."""
    seed_live_baseline(tmp_path)
    state_file = tmp_path / "state" / "state.json"
    state = deserialize_state(state_file.read_bytes())
    pruned = state.model_copy(
        update={
            "conditions": tuple(
                c
                for c in state.conditions
                if not (c.key.action_key == "audit.security" and c.key.level == "high")
            ),
            "histories": tuple(h for h in state.histories if h.action_key != "audit.security"),
        }
    )
    assert len(pruned.conditions) == len(state.conditions) - 1
    assert len(pruned.histories) < len(state.histories)
    state_file.write_bytes(serialize_state(pruned))
    return state_file


def write_env(tmp_path: Path, *, token: str | None = TOKEN) -> Path:
    path = tmp_path / "radar.env"
    path.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={token}\n" if token else "\n", encoding="utf-8")
    return path


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "radar.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def cli_args(tmp_path: Path, env_file: Path, config: Path | None, *, dry_run: bool) -> list[str]:
    args = [
        "run",
        "--fixtures",
        str(FIXTURES),
        "--state-dir",
        str(tmp_path / "state"),
        "--env-file",
        str(env_file),
        "--now",
        LATER_NOW,
    ]
    if dry_run:
        args.append("--dry-run")
    if config is not None:
        args += ["--config", str(config)]
    return args


def run_process(
    code: str, args: Sequence[str], *, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": path if path is not None else os.environ.get("PATH", ""),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def dry_run(
    tmp_path: Path, *, config_text: str | None, bin_dir: Path | None, token: str | None = TOKEN
) -> subprocess.CompletedProcess[str]:
    config = write_config(tmp_path, config_text) if config_text is not None else None
    args = cli_args(tmp_path, write_env(tmp_path, token=token), config, dry_run=True)
    path = with_path(bin_dir) if bin_dir is not None else None
    return run_process(LAUNCH, args, path=path)


def alert_block(output: str) -> list[str]:
    """The Project A High alert line and the line after it."""
    lines = output.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(ALERT_PREFIX))
    return lines[index : index + 2]


def assert_no_leak(*texts: str) -> None:
    for text in texts:
        assert TOKEN not in text
        assert SESSION_ID not in text


def test_dry_run_with_triage_shows_cause(tmp_path: Path) -> None:
    seed_fresh_high(tmp_path)
    before = state_snapshot(tmp_path / "state")
    bin_dir = install_fake_claude(tmp_path, envelope=ok_envelope())

    done = dry_run(tmp_path, config_text=TRIAGE_ON, bin_dir=bin_dir)

    assert done.returncode == 0, done.stderr
    block = alert_block(done.stdout)
    assert "security High" in block[0]
    assert block[1] == P1_LINE
    assert "triage call: items 1, outcome ok" in done.stderr
    assert "triage today (UTC):" in done.stderr
    assert (bin_dir / FAKE_CALLED).exists()
    assert not (tmp_path / "state" / COST_FILE).exists()
    assert state_snapshot(tmp_path / "state") == before
    assert_no_leak(done.stdout, done.stderr)


def test_triage_off_prints_the_rule_only_alert_and_never_calls_claude(tmp_path: Path) -> None:
    seed_fresh_high(tmp_path)
    bin_dir = install_fake_claude(tmp_path, envelope=ok_envelope())

    done = dry_run(tmp_path, config_text=None, bin_dir=bin_dir)

    assert done.returncode == 0, done.stderr
    block = alert_block(done.stdout)
    assert "security High" in block[0]
    assert "P1" not in done.stdout
    assert "triage call" not in done.stderr
    assert "triage today" not in done.stderr
    assert not (bin_dir / FAKE_CALLED).exists()


def test_claude_exiting_1_falls_back_to_the_rule_only_output(tmp_path: Path) -> None:
    seed_fresh_high(tmp_path)
    rule_only = dry_run(tmp_path, config_text=None, bin_dir=None)
    bin_dir = install_fake_claude(
        tmp_path, envelope=error_envelope(result=f"junk {TOKEN}"), exit_code=1
    )

    done = dry_run(tmp_path, config_text=TRIAGE_ON, bin_dir=bin_dir)

    assert done.returncode == 0, done.stderr
    assert without_timing(done.stdout) == without_timing(rule_only.stdout)
    assert "outcome exit_nonzero" in done.stderr
    assert_no_leak(done.stdout, done.stderr)


def test_missing_token_falls_back_without_starting_claude(tmp_path: Path) -> None:
    seed_fresh_high(tmp_path)
    rule_only = dry_run(tmp_path, config_text=None, bin_dir=None)
    bin_dir = install_fake_claude(tmp_path, envelope=ok_envelope())

    done = dry_run(tmp_path, config_text=TRIAGE_ON, bin_dir=bin_dir, token=None)

    assert done.returncode == 0, done.stderr
    assert without_timing(done.stdout) == without_timing(rule_only.stdout)
    assert "outcome not_logged_in" in done.stderr
    assert not (bin_dir / FAKE_CALLED).exists()


def test_live_run_with_triage_sends_the_line_and_writes_the_cost_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = seed_fresh_high(tmp_path)
    bin_dir = install_fake_claude(tmp_path, envelope=ok_envelope(cost=0.0123))
    monkeypatch.setenv("PATH", with_path(bin_dir))
    config = write_config(tmp_path, TRIAGE_ON)
    transport = FakeTransport([ok_response(n) for n in range(1, 6)])
    out = io.StringIO()

    code = cli.main(
        cli_args(tmp_path, write_env(tmp_path), config, dry_run=False),
        environ=LIVE_ENVIRON,
        transport=transport,
        sleep=FakeTime().sleep,
        stdout=out,
    )

    assert code == 0
    texts = [str(payload["text"]) for _, payload in transport.requests]
    high = [text for text in texts if text.startswith(ALERT_PREFIX)]
    assert len(high) == 1
    assert high[0].endswith(f"\n{P1_LINE}")
    assert high[0].count("\n") == 1
    cost_file = tmp_path / "state" / COST_FILE
    (day,) = FileTriageCostStore(tmp_path / "state").load()
    assert day.calls == 1
    assert day.failures == 0
    assert stat.S_IMODE(cost_file.stat().st_mode) == 0o600
    assert deserialize_state(state_file.read_bytes()).schema_version == 3
    for stored in (state_file, cost_file):
        text = stored.read_text(encoding="utf-8")
        assert FIXTURE_TITLE not in text
        assert CAUSE not in text
    assert_no_leak(out.getvalue(), *texts)


@pytest.mark.parametrize(("config_text", "expected"), [(None, "False"), (TRIAGE_ON, "True")])
def test_the_adapter_is_imported_only_when_triage_is_on(
    tmp_path: Path, config_text: str | None, expected: str
) -> None:
    code = (
        "import sys; from enji_guard_radar import main; main(sys.argv[1:]); "
        "print('enji_guard_radar.claude_cli_triager' in sys.modules)"
    )
    config = write_config(tmp_path, config_text) if config_text is not None else None
    args = [
        "run",
        "--dry-run",
        "--fixtures",
        str(FIXTURES),
        "--state-dir",
        str(tmp_path / "state"),
        "--env-file",
        str(tmp_path / "none.env"),
        "--now",
        DEMO_NOW,
    ]
    if config is not None:
        args += ["--config", str(config)]

    done = run_process(code, args)

    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1] == expected
