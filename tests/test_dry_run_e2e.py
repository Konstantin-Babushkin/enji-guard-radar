import hashlib
import io
import logging
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport

from enji_guard_radar import cli
from enji_guard_radar.cli import read_config
from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard import parse_guard_time
from enji_guard_radar.pipeline import Deps, ProjectStatus, run_once
from enji_guard_radar.ports import GuardTools
from enji_guard_radar.state_store import DryRunStateStore, FileStateStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
DEMO_NOW = "2026-11-16T12:00:00Z"
LATER_NOW = "2026-12-01T12:00:00Z"
LIVE_ENVIRON = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "-1001234567890"}

A_NAME = 'Project A &lt;core&gt; &amp; "tools"'
A_HIGH_LINE = f"🔴 <b>{A_NAME}</b> · security High 0→1 · 6d"
B_LINES = ["🟡 <b>Project B</b> · no scan 27d"]
C_LINES = ["🟡 <b>Project C (no repositories)</b> · no scan yet"]


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def cli_args(tmp_path: Path, now: str, config: Path | None) -> list[str]:
    args = [
        "run",
        "--fixtures",
        str(FIXTURES),
        "--state-dir",
        str(tmp_path / "state"),
        "--env-file",
        str(tmp_path / "none.env"),
        "--now",
        now,
    ]
    return [*args, "--config", str(config)] if config is not None else args


def run_dry(tmp_path: Path, now: str = DEMO_NOW, config: Path | None = None) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main([*cli_args(tmp_path, now, config), "--dry-run"], environ={}, stdout=out)
    return code, out.getvalue()


def seed_live_baseline(tmp_path: Path, now: str = DEMO_NOW, config: Path | None = None) -> None:
    """A real live run with a fake transport: it has nothing to send and leaves a baseline."""
    transport = FakeTransport([])
    code = cli.main(
        cli_args(tmp_path, now, config),
        environ=LIVE_ENVIRON,
        transport=transport,
        sleep=FakeTime().sleep,
        stdout=io.StringIO(),
    )
    assert code == 0
    assert transport.requests == []


def contains_contiguous(text: str, wanted: list[str]) -> bool:
    lines = text.splitlines()
    return any(lines[i : i + len(wanted)] == wanted for i in range(len(lines)))


def section(text: str, header_prefix: str) -> list[str]:
    """Lines from the header that starts with header_prefix up to the next `---` header."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(header_prefix))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("---")), len(lines))
    return lines[start:end]


def fixture_digests() -> dict[str, str]:
    return {
        str(path.relative_to(FIXTURES)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(FIXTURES.rglob("*"))
        if path.is_file()
    }


def state_snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_dry_run_baselines_stale_project_b(tmp_path: Path) -> None:
    code, out = run_dry(tmp_path)
    assert code == 0
    assert contains_contiguous(out, B_LINES)
    assert contains_contiguous("\n".join(section(out, "--- baseline")), B_LINES)


def test_dry_run_reports_project_c_as_never_ran(tmp_path: Path) -> None:
    _, out = run_dry(tmp_path)
    assert contains_contiguous("\n".join(section(out, "--- baseline")), C_LINES)


def test_dry_run_does_not_alert_fresh_project_a(tmp_path: Path) -> None:
    # A's newest run (c012) sits on page 2 of its runs; page 3 is unrecorded, so reading it
    # would make A "not checked". A must appear neither as an alert nor under "not checked".
    _, out = run_dry(tmp_path)
    assert not any(line.startswith("🟡") and "<b>Project A" in line for line in out.splitlines())
    assert not any("Project A" in line for line in section(out, "--- not checked"))
    assert A_HIGH_LINE in section(out, "--- baseline")


def test_dry_run_is_repeatable_and_leaves_fixtures_unchanged(tmp_path: Path) -> None:
    before = fixture_digests()
    first = run_dry(tmp_path)
    second = run_dry(tmp_path)
    assert first == second
    assert fixture_digests() == before


def test_dry_run_creates_nothing_under_the_state_directory(tmp_path: Path) -> None:
    run_dry(tmp_path)
    assert not (tmp_path / "state").exists()


def test_dry_run_lists_unreachable_project_d_as_not_checked(tmp_path: Path) -> None:
    code, out = run_dry(tmp_path)
    assert code == 0
    lines = out.splitlines()
    assert "--- not checked (1) ---" in lines
    assert (
        "Project D (Guard unreachable): Guard data unavailable "
        "(list_repositories failed: simulated Guard outage)"
    ) in lines
    assert "Project D" not in "\n".join(line for line in lines if line.endswith("</b>"))


def test_dry_run_baselines_long_stale_project_e(tmp_path: Path) -> None:
    _, out = run_dry(tmp_path)
    assert "🟡 <b>Project E (paused in config)</b> · no scan 104d" in out.splitlines()


def test_dry_run_summary_counts(tmp_path: Path) -> None:
    _, out = run_dry(tmp_path)
    assert (
        "summary: 5 evaluated, 2 stale, 1 never ran, 1 not checked, "
        "0 message(s) would be sent, 4 baselined"
    ) in out.splitlines()
    assert "--- baseline, not sent (4) ---" in out.splitlines()
    baseline = section(out, "--- baseline")
    assert A_HIGH_LINE in baseline


def test_dry_run_orders_baseline_by_project_id(tmp_path: Path) -> None:
    _, out = run_dry(tmp_path)
    headlines = [line for line in section(out, "--- baseline") if line.startswith("🟡")]
    assert headlines == [
        "🟡 <b>Project B</b> · no scan 27d",
        "🟡 <b>Project C (no repositories)</b> · no scan yet",
        "🟡 <b>Project E (paused in config)</b> · no scan 104d",
    ]


def test_dry_run_orders_would_send_worst_first(tmp_path: Path) -> None:
    seed_live_baseline(tmp_path)
    _, out = run_dry(tmp_path, LATER_NOW)
    headlines = [line for line in out.splitlines() if line.startswith("🟡")]
    ages = [int(match.group(1)) for match in re.finditer(r"· no scan (\d+)d$", out, re.MULTILINE)]
    assert len(headlines) == len(ages) == 2
    assert ages == sorted(ages, reverse=True)


SCENARIO_CONFIG = FIXTURES / "radar.yaml"
PAUSED_ID = "00000000-0000-4000-8000-00000000a005"
EXCLUDED_ID = "00000000-0000-4000-8000-00000000a006"


def test_dry_run_with_scenario_config_skips_paused_and_excluded(tmp_path: Path) -> None:
    code, out = run_dry(tmp_path, config=SCENARIO_CONFIG)
    lines = out.splitlines()
    assert code == 0
    assert lines.count("--- would send to group ---") == 0
    assert "--- baseline, not sent (3) ---" in lines
    assert contains_contiguous(out, B_LINES)
    assert contains_contiguous(out, C_LINES)
    assert "Project E (paused in config): paused in config" in lines
    assert "Project F" not in out
    assert (
        "summary: 3 evaluated, 1 stale, 1 never ran, 2 not checked, "
        "0 message(s) would be sent, 3 baselined"
    ) in lines


class RecordingGuard:
    def __init__(self, inner: GuardTools) -> None:
        self.inner = inner
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append((tool, args))
        return self.inner.call(tool, args)


def test_paused_and_excluded_projects_get_no_guard_calls(tmp_path: Path) -> None:
    guard = RecordingGuard(FixtureGuardTools(FIXTURES))
    config = read_config(SCENARIO_CONFIG)
    deps = Deps(
        guard=guard,
        notifier=cli.PrintNotifier([]),
        config=config,
        clock=lambda: parse_guard_time(DEMO_NOW),
        store=DryRunStateStore(FileStateStore(tmp_path / "state")),
    )
    report = run_once(deps)
    assert guard.calls
    for _, args in guard.calls:
        assert PAUSED_ID not in args.values()
        assert EXCLUDED_ID not in args.values()
    statuses = {r.project_id: r.status for r in report.results}
    assert statuses[PAUSED_ID] is ProjectStatus.PAUSED
    assert EXCLUDED_ID not in statuses


def test_config_naming_an_unlisted_project_is_logged_and_changes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ghost = "00000000-0000-4000-8000-0000000000ff"
    config = tmp_path / "ghost.yaml"
    config.write_text(f'projects:\n  "{ghost}": {{mode: paused}}\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        code, out = run_dry(tmp_path, config=config)
    assert code == 0
    assert f"config names project id {ghost}, which Guard did not list" in caplog.text
    assert "summary: 5 evaluated, 2 stale, 1 never ran, 1 not checked" in out


def test_future_newest_run_is_treated_as_fresh_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        code, out = run_dry(tmp_path, now="2026-01-01T00:00:00Z")
    assert code == 0
    assert "is in the future" in caplog.text
    assert "treated as fresh" in caplog.text
    assert not re.search(r"no scan \d+d", out)


def test_dry_run_after_live_baseline_prints_would_send(tmp_path: Path) -> None:
    seed_live_baseline(tmp_path, config=SCENARIO_CONFIG)
    before = state_snapshot(tmp_path / "state")
    assert before

    code, out = run_dry(tmp_path, LATER_NOW, config=SCENARIO_CONFIG)

    assert code == 0
    lines = out.splitlines()
    assert lines.count("--- would send to group ---") == 1
    assert f"🟡 <b>{A_NAME}</b> · no scan 20d" in lines
    assert "--- baseline, not sent (0) ---" in lines
    assert state_snapshot(tmp_path / "state") == before


def test_dry_run_collapses_alerts_above_the_configured_threshold(tmp_path: Path) -> None:
    config = tmp_path / "collapse.yaml"
    config.write_text("alerts:\n  collapse_above: 1\n", encoding="utf-8")
    seed_live_baseline(tmp_path, config=config)

    code, out = run_dry(tmp_path, LATER_NOW, config=config)

    lines = out.splitlines()
    assert code == 0
    assert lines.count("--- would send to group ---") == 1
    assert "Radar · problems: 2 projects" in lines
    assert f"🟡 <b>{A_NAME}</b> · no scan 20d" in lines
    assert "1 message(s) would be sent" in out


def test_two_alerts_are_sent_individually_with_default_collapse(tmp_path: Path) -> None:
    seed_live_baseline(tmp_path)

    code, out = run_dry(tmp_path, LATER_NOW)

    lines = out.splitlines()
    assert code == 0
    assert lines.count("--- would send to group ---") == 2
    assert not any(line.startswith("Radar ·") for line in lines)
    assert f"🟡 <b>{A_NAME}</b> · no scan 20d" in lines
    assert "2 message(s) would be sent" in out
