"""`serve` as its own process: real signals, real files, no network and no credentials.

The Guard login is missing, so every poll fails fast and Telegram is never called. The loop must
keep running, exit 0 on SIGTERM and leave a loadable state file.
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

LAUNCH = "import sys; from enji_guard_radar import main; sys.exit(main(sys.argv[1:]))"
BOUNDARY_GUARD_SECONDS = 15


def seconds_to_midnight_utc() -> float:
    now = datetime.now(UTC)
    return 86400 - (now.hour * 3600 + now.minute * 60 + now.second)


def start(tmp_path: Path, log: Path) -> subprocess.Popen[bytes]:
    config = tmp_path / "radar.yaml"
    config.write_text("schedule:\n  every_minutes: 1440\n", encoding="utf-8")
    env_file = tmp_path / "radar.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=123456789:process-test-token\nTELEGRAM_CHAT_ID=-1001234567890\n",
        encoding="utf-8",
    )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    with log.open("wb") as stderr:
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                LAUNCH,
                "serve",
                "--config",
                str(config),
                "--state-dir",
                str(tmp_path / "state"),
                "--env-file",
                str(env_file),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )


def wait_for(log: Path, text: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if text in log.read_text(encoding="utf-8"):
            return
        time.sleep(0.1)
    pytest.fail(f"{text!r} never appeared in the log:\n{log.read_text(encoding='utf-8')}")


def test_serve_process_catches_up_survives_a_failed_poll_and_stops_on_sigterm(
    tmp_path: Path,
) -> None:
    log1 = tmp_path / "log1.txt"
    process = start(tmp_path, log1)
    try:
        wait_for(log1, "serve: run finish")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
    text = log1.read_text(encoding="utf-8")
    assert text.count("serve: run start") == 1
    assert "reason=catch-up" in text
    assert "serve: run finish exit=1 " in text
    assert "next_slot=" in text
    assert "T00:00:00Z" in text.split("serve: run finish", 1)[1]
    assert "serve: stop requested (SIGTERM)" in text
    assert text.rstrip().endswith("serve: stopped")
    state = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    assert state["schema_version"] == 3
    assert state["health"]["consecutive_failures"] == 1


def test_serve_process_started_again_in_the_same_slot_does_not_run(tmp_path: Path) -> None:
    first = tmp_path / "log1.txt"
    process = start(tmp_path, first)
    try:
        wait_for(first, "serve: run finish")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
    if seconds_to_midnight_utc() < BOUNDARY_GUARD_SECONDS:
        pytest.skip("too close to the 00:00 UTC slot to tell a restart from a new slot")

    second = tmp_path / "log2.txt"
    process = start(tmp_path, second)
    try:
        wait_for(second, "serve: started")
        time.sleep(1.5)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
    text = second.read_text(encoding="utf-8")
    assert "serve: run start" not in text
    assert text.count("reason=scheduled") == 1
    assert "serve: stop requested (SIGTERM)" in text
