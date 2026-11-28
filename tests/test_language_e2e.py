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
    "weekly|no scan|problems|need attention|Last poll|Poll |no runs|Ages count|"
    "more open|could not be read|Could not read|Resolved|closed|Open items|Tooling gap|Stale:|"
    "High:|most in|clean"
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
        [f"🟡 <b>{PROJECT_A}</b>: no scan for 20 days"],
        ["🟡 <b>Project F (excluded in config)</b>: no scan for 16 days"],
    ]
    assert blocks(russian.stdout, "") == [
        [f"🟡 <b>{PROJECT_A}</b>: нет проверок 20 дней"],
        ["🟡 <b>Project F (excluded in config)</b>: нет проверок 16 дней"],
    ]
    assert not ENGLISH_PHRASES.search(russian.stdout)
    assert FRAME.findall(english.stdout) == FRAME.findall(russian.stdout)
    # A dry-run writes nothing, in either language.
    assert sorted(p.name for p in (tmp_path / "state").iterdir()) == seeded


def test_collapsed_alerts_are_one_line_per_project_in_both_languages(
    tmp_path: Path, ru_config: Path
) -> None:
    collapse = tmp_path / "collapse.yaml"
    collapse.write_text("alerts:\n  collapse_above: 1\n", encoding="utf-8")
    ru_collapse = tmp_path / "ru-collapse.yaml"
    ru_collapse.write_text("messages:\n  language: ru\nalerts:\n  collapse_above: 1\n", "utf-8")
    seed_live_baseline(tmp_path)
    english = run_cli(tmp_path, ALERT_AT, "--config", str(collapse))
    russian = run_cli(tmp_path, ALERT_AT, "--config", str(ru_collapse))
    assert english.returncode == russian.returncode == 0

    assert blocks(english.stdout, "") == [
        [
            "Radar · problems: 2 projects",
            f"🟡 <b>{PROJECT_A}</b>: no scan for 20 days",
            "🟡 <b>Project F (excluded in config)</b>: no scan for 16 days",
        ]
    ]
    assert blocks(russian.stdout, "") == [
        [
            "Радар · проблемы: 2 проекта",
            f"🟡 <b>{PROJECT_A}</b>: нет проверок 20 дней",
            "🟡 <b>Project F (excluded in config)</b>: нет проверок 16 дней",
        ]
    ]
    assert not ENGLISH_PHRASES.search(russian.stdout)


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
    for kept in ("security", PROJECT_A):
        assert kept in russian.stdout
    lines = [without_timing(o).count("\n") for o in (english.stdout, russian.stdout)]
    assert lines[0] == lines[1]
    assert russian.stdout.startswith(f"DRY RUN at {FIRST_NOW}: nothing is sent")
    digest = blocks(russian.stdout, "")[0]
    assert digest[0] == "<b>Радар · недельная сводка</b>"
    assert digest[1] == "Высоких проблем: 1 в 1 проекте, закрыто за неделю: 0."
    assert digest[-1] == "⚠️ Не удалось прочитать 1 проект · опрос 2026-11-18 06:00 UTC"
    assert sum(line.startswith(("🔴", "🟡")) for line in digest) == 4
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


# --- the green block of clean projects ---


def test_green_block_in_russian_dry_run(tmp_path: Path, ru_config: Path) -> None:
    done = run_cli(tmp_path, FIRST_NOW, "--config", str(ru_config))
    assert done.returncode == 0
    lines = done.stdout.splitlines()
    assert "🟢 <b>Project F (excluded in config)</b> — чисто" in lines


def test_green_block_absent_when_no_project_is_clean(tmp_path: Path) -> None:
    done = run_cli(tmp_path, FIRST_NOW, "--config", str(FIXTURES / "radar.yaml"))
    assert done.returncode == 0
    assert "🟢" not in done.stdout
    assert "— clean" not in done.stdout
