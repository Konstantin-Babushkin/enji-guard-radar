"""End to end: the radar CLI runs as its own process on the synthetic fixtures (no network)."""

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

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
