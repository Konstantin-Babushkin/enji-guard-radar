"""End to end: the radar CLI runs as its own process on the synthetic fixtures (no network)."""

import io
import logging
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from telegram_fakes import FakeTime, FakeTransport
from test_dry_run_e2e import DEMO_NOW, LIVE_ENVIRON, cli_args, seed_live_baseline

from enji_guard_radar import cli
from enji_guard_radar.ledger import deserialize_state

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "guard"
GOLDEN = ROOT / "tests" / "golden" / "dry_run_default.txt"
FIRST_NOW = "2026-11-18T06:00:00Z"
LAUNCH = "import sys; from enji_guard_radar import main; sys.exit(main(sys.argv[1:]))"


def run_cli(
    tmp_path: Path, now: str, *extra: str, state_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """`run --dry-run` on the fixtures in a fresh process with no Telegram settings."""
    args: Sequence[str] = (
        "run",
        "--dry-run",
        "--fixtures",
        str(FIXTURES),
        "--state-dir",
        str(state_dir if state_dir is not None else tmp_path / "state"),
        "--env-file",
        str(tmp_path / "none.env"),
        "--now",
        now,
        *extra,
    )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-c", LAUNCH, *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def without_timing(output: str) -> str:
    """The `poll cost:` line holds a wall-clock timing."""
    return "".join(
        line for line in output.splitlines(keepends=True) if not line.startswith("poll cost")
    )


def test_golden_default_dry_run_output(tmp_path: Path) -> None:
    done = run_cli(tmp_path, FIRST_NOW)
    assert done.returncode == 0
    assert without_timing(done.stdout) == GOLDEN.read_text(encoding="utf-8")
    assert not (tmp_path / "state").exists()


# --- messages.language: ru, end to end ---

ALERT_AT = "2026-12-01T12:00:00Z"
CYRILLIC = re.compile("[А-Яа-яЁё]")
ENGLISH_PHRASES = re.compile(
    "weekly digest|Stale Guard scans|No Guard runs|High findings|Severity:|Open items|"
    "Last successful|more open|could not be read|Resolved|Seen by the radar|No completed|"
    "Tooling gap|Stale:|High:"
)
FRAME = re.compile(r"^(---|summary:|follow-ups:|DRY RUN)", re.MULTILINE)
PROJECT_A = 'Project A &lt;core&gt; &amp; "tools"'


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main (used to seed the baseline) installs a handler on the root logger."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def ru_config(tmp_path: Path) -> Path:
    path = tmp_path / "ru.yaml"
    path.write_text("messages:\n  language: ru\n", encoding="utf-8")
    return path


def blocks(output: str, heading: str) -> list[list[str]]:
    """The text blocks printed under `--- would send to group ---`, one list of lines each."""
    found: list[list[str]] = []
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line == "--- would send to group ---":
            block = []
            for text in lines[index + 1 :]:
                if text == "":
                    break
                block.append(text)
            if block[0].startswith(heading) or heading == "":
                found.append(block)
    return found


def test_alerts_in_russian_match_the_english_ones(tmp_path: Path, ru_config: Path) -> None:
    seed_live_baseline(tmp_path)
    seeded = sorted(p.name for p in (tmp_path / "state").iterdir())
    english = run_cli(tmp_path, ALERT_AT)
    russian = run_cli(tmp_path, ALERT_AT, "--config", str(ru_config))
    assert english.returncode == russian.returncode == 0

    assert blocks(english.stdout, "") == [
        [f"🟡 <b>{PROJECT_A}</b> · no scan 20d"],
        ["🟡 <b>Project F (excluded in config)</b> · no scan 16d"],
    ]
    assert blocks(russian.stdout, "") == [
        [f"🟡 <b>{PROJECT_A}</b> · нет проверок 20 д"],
        ["🟡 <b>Project F (excluded in config)</b> · нет проверок 16 д"],
    ]
    assert not ENGLISH_PHRASES.search(russian.stdout)
    assert FRAME.findall(english.stdout) == FRAME.findall(russian.stdout)
    # A dry-run writes nothing, in either language.
    assert sorted(p.name for p in (tmp_path / "state").iterdir()) == seeded


def test_digest_and_baseline_in_russian_match_the_english_run(
    tmp_path: Path, ru_config: Path
) -> None:
    english = run_cli(tmp_path, FIRST_NOW)
    russian = run_cli(tmp_path, FIRST_NOW, "--config", str(ru_config))
    assert english.returncode == russian.returncode == 0
    assert not CYRILLIC.search(english.stdout)
    assert len(CYRILLIC.findall(russian.stdout)) >= 10
    assert not ENGLISH_PHRASES.search(russian.stdout)

    def names(output: str) -> list[str]:
        return re.findall(r"Project [A-F]", output)

    assert names(english.stdout) == names(russian.stdout)
    assert FRAME.findall(english.stdout) == FRAME.findall(russian.stdout)
    assert "--- baseline, not sent (4) ---" in russian.stdout
    for kept in ("audit.security", "acme/service-a", PROJECT_A):
        assert kept in russian.stdout
    lines = [without_timing(o).count("\n") for o in (english.stdout, russian.stdout)]
    assert lines[0] == lines[1]
    assert russian.stdout.startswith(f"DRY RUN at {FIRST_NOW}: nothing is sent")
    digest = blocks(russian.stdout, "")[0]
    assert digest[0] == "<b>Еженедельный дайджест Guard radar</b>"
    assert not (tmp_path / "state").exists()


def test_a_live_run_switched_to_russian_sends_nothing_new(tmp_path: Path, ru_config: Path) -> None:
    seed_live_baseline(tmp_path)
    state_file = tmp_path / "state" / "state.json"
    before = deserialize_state(state_file.read_bytes())
    transport = FakeTransport([])
    code = cli.main(
        cli_args(tmp_path, DEMO_NOW, ru_config),
        environ=LIVE_ENVIRON,
        transport=transport,
        sleep=FakeTime().sleep,
        stdout=io.StringIO(),
    )
    assert code == 0
    assert transport.requests == []
    after = deserialize_state(state_file.read_bytes())
    assert after.conditions == before.conditions  # nothing was alerted, marked or re-armed
