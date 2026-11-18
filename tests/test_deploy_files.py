"""The shipped unit files, cron example and runbook, checked statically (no systemd here)."""

import argparse
import re
import shlex
from collections import defaultdict
from pathlib import Path

import pytest

from enji_guard_radar import cli
from enji_guard_radar.secret_guard import find_secret

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy" / "systemd" / "enji-guard-radar.service"
TIMER = ROOT / "deploy" / "systemd" / "enji-guard-radar.timer"
CRON = ROOT / "deploy" / "cron.example"
RUNBOOK = ROOT / "docs" / "deploy.md"
ENTRY_POINT = "/opt/enji-guard-radar/.venv/bin/enji-guard-radar"

Unit = dict[str, dict[str, list[str]]]


def read_unit(path: Path) -> Unit:
    """INI-style reader that keeps repeated keys."""
    sections: Unit = {}
    current: dict[str, list[str]] = defaultdict(list)
    for raw in path.read_text(encoding="utf-8").splitlines():
        text = raw.strip()
        if not text or text.startswith(("#", ";")):
            continue
        if text.startswith("[") and text.endswith("]"):
            current = defaultdict(list)
            sections[text[1:-1]] = current
            continue
        key, _, value = text.partition("=")
        current[key.strip()].append(value.strip())
    return sections


def only(unit: Unit, section: str, key: str) -> str:
    values = unit[section][key]
    assert len(values) == 1, f"{key} must appear once in [{section}]"
    return values[0]


def parse_command(words: list[str]) -> argparse.Namespace:
    """Parse arguments with the real CLI parser (it exits on an unknown flag)."""
    return cli._build_parser().parse_args(words)


def assert_runs_the_radar(args: argparse.Namespace, *, state_dir: str) -> None:
    assert args.command == "run"
    assert args.fixtures is None
    assert args.dry_run is False
    assert args.state_dir == Path(state_dir)
    assert args.config == Path("/etc/enji-guard-radar/radar.yaml")
    assert args.env_file == Path("/etc/enji-guard-radar/radar.env")


def timeout_seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)(s|min|m)?", value)
    assert match, value
    unit = match.group(2) or "s"
    return int(match.group(1)) * (1 if unit == "s" else 60)


def test_service_is_a_oneshot_with_a_start_timeout() -> None:
    service = read_unit(SERVICE)
    assert only(service, "Service", "Type") == "oneshot"
    assert only(service, "Service", "TimeoutStartSec") == "15min"
    assert 0 < timeout_seconds(only(service, "Service", "TimeoutStartSec")) <= 30 * 60


def test_service_runs_as_a_hardened_dedicated_user() -> None:
    service = read_unit(SERVICE)
    expected = {
        "User": "enji-guard-radar",
        "Group": "enji-guard-radar",
        "WorkingDirectory": "/opt/enji-guard-radar",
        "StateDirectory": "enji-guard-radar",
        "StateDirectoryMode": "0700",
        "UMask": "0077",
        "NoNewPrivileges": "true",
        "PrivateTmp": "true",
        "ProtectSystem": "strict",
    }
    for key, value in expected.items():
        assert only(service, "Service", key) == value, key
    assert only(service, "Unit", "Wants") == "network-online.target"
    assert only(service, "Unit", "After") == "network-online.target"


def test_service_exec_start_parses_with_the_real_cli_parser() -> None:
    service = read_unit(SERVICE)
    words = shlex.split(only(service, "Service", "ExecStart"))
    assert words[0] == ENTRY_POINT
    state_directory = only(service, "Service", "StateDirectory")
    assert_runs_the_radar(parse_command(words[1:]), state_dir=f"/var/lib/{state_directory}")


def test_service_carries_no_secret_and_no_uv() -> None:
    service = read_unit(SERVICE)
    assert "EnvironmentFile" not in service["Service"]
    assert "Environment" not in service["Service"]
    assert "uv run" not in " ".join(service["Service"]["ExecStart"])


def test_timer_runs_every_three_hours_with_catch_up() -> None:
    timer = read_unit(TIMER)
    assert only(timer, "Timer", "OnCalendar") == "*-*-* 00/3:00:00"
    assert only(timer, "Timer", "Persistent") == "true"
    assert only(timer, "Timer", "RandomizedDelaySec") == "5min"
    assert only(timer, "Timer", "Unit") == "enji-guard-radar.service"
    assert only(timer, "Install", "WantedBy") == "timers.target"


def cron_commands() -> list[str]:
    lines = CRON.read_text(encoding="utf-8").splitlines()
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


def test_cron_example_bounds_the_run_and_parses_like_exec_start() -> None:
    commands = cron_commands()
    assert len(commands) == 1
    words = shlex.split(commands[0])
    at = words.index("timeout")
    assert words[at : at + 3] == ["timeout", "15m", ENTRY_POINT]
    assert_runs_the_radar(parse_command(words[at + 3 :]), state_dir="/var/lib/enji-guard-radar")


def test_cron_example_has_no_flock_wrapper_but_explains_why() -> None:
    assert all("flock" not in command for command in cron_commands())
    comments = [
        line
        for line in CRON.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("#")
    ]
    assert any("flock" in line and "radar.lock" in line for line in comments)


@pytest.mark.parametrize("path", [SERVICE, TIMER, CRON, RUNBOOK], ids=lambda p: p.name)
def test_no_secret_in_shipped_files(path: Path) -> None:
    assert find_secret(path.read_text(encoding="utf-8"), ()) is None
