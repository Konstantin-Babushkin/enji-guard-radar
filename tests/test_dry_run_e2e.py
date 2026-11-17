import hashlib
import io
import logging
from collections.abc import Mapping
from pathlib import Path

import pytest

from enji_guard_radar import cli
from enji_guard_radar.cli import read_config
from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard import parse_guard_time
from enji_guard_radar.pipeline import Deps, ProjectStatus, run_once
from enji_guard_radar.ports import GuardTools

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
DEMO_NOW = "2026-11-16T12:00:00Z"

B_LINES = [
    "Stale Guard scans: <b>Project B</b>",
    "No completed Guard run for 27 days (last: 2026-10-20).",
    "Severity: medium.",
]
C_LINES = [
    "No Guard runs: <b>Project C (no repositories)</b>",
    "It has no repositories in Guard.",
    "Severity: medium.",
]


def run_dry(now: str = DEMO_NOW) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(
        ["run", "--dry-run", "--fixtures", str(FIXTURES), "--now", now],
        stdout=out,
    )
    return code, out.getvalue()


def contains_contiguous(text: str, wanted: list[str]) -> bool:
    lines = text.splitlines()
    return any(lines[i : i + len(wanted)] == wanted for i in range(len(lines)))


def fixture_digests() -> dict[str, str]:
    return {
        str(path.relative_to(FIXTURES)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(FIXTURES.rglob("*"))
        if path.is_file()
    }


def test_dry_run_prints_stale_alert_for_project_b() -> None:
    code, out = run_dry()
    assert code == 0
    assert contains_contiguous(out, B_LINES)


def test_dry_run_reports_project_c_as_never_ran() -> None:
    _, out = run_dry()
    assert contains_contiguous(out, C_LINES)


def test_dry_run_does_not_alert_fresh_project_a() -> None:
    # A's newest run (c012) sits on page 2 of its runs; page 3 is unrecorded, so reading it
    # would make A "not checked". A must appear neither as an alert nor under "not checked".
    _, out = run_dry()
    assert "Project A" not in out


def test_dry_run_is_repeatable_and_leaves_fixtures_unchanged() -> None:
    before = fixture_digests()
    first = run_dry()
    second = run_dry()
    assert first == second
    assert fixture_digests() == before


def test_dry_run_lists_unreachable_project_d_as_not_checked() -> None:
    code, out = run_dry()
    assert code == 0
    lines = out.splitlines()
    assert "--- not checked (1) ---" in lines
    assert (
        "Project D (Guard unreachable): Guard data unavailable "
        "(list_repositories failed: simulated Guard outage)"
    ) in lines
    assert "Project D" not in "\n".join(line for line in lines if line.endswith("</b>"))


def test_dry_run_alerts_long_stale_project_e() -> None:
    _, out = run_dry()
    assert "No completed Guard run for 104 days (last: 2026-08-04)." in out.splitlines()


def test_dry_run_summary_counts() -> None:
    _, out = run_dry()
    assert (
        "summary: 5 evaluated, 2 stale, 1 never ran, 1 not checked, 3 message(s) would be sent"
        in out.splitlines()
    )


def test_dry_run_orders_alerts_worst_first() -> None:
    _, out = run_dry()
    headlines = [
        line
        for line in out.splitlines()
        if line.startswith(("Stale Guard scans:", "No Guard runs:"))
    ]
    assert headlines == [
        "Stale Guard scans: <b>Project E (paused in config)</b>",
        "Stale Guard scans: <b>Project B</b>",
        "No Guard runs: <b>Project C (no repositories)</b>",
    ]


SCENARIO_CONFIG = FIXTURES / "radar.yaml"
PAUSED_ID = "00000000-0000-4000-8000-00000000a005"
EXCLUDED_ID = "00000000-0000-4000-8000-00000000a006"


def run_dry_with_config(config: Path, now: str = DEMO_NOW) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(FIXTURES),
            "--config",
            str(config),
            "--now",
            now,
        ],
        stdout=out,
    )
    return code, out.getvalue()


def test_dry_run_with_scenario_config_skips_paused_and_excluded() -> None:
    code, out = run_dry_with_config(SCENARIO_CONFIG)
    lines = out.splitlines()
    assert code == 0
    assert lines.count("--- would send to group ---") == 2
    assert contains_contiguous(out, B_LINES)
    assert contains_contiguous(out, C_LINES)
    assert "Project E (paused in config): paused in config" in lines
    assert "Project F" not in out
    assert (
        "summary: 3 evaluated, 1 stale, 1 never ran, 2 not checked, 2 message(s) would be sent"
        in lines
    )


class RecordingGuard:
    def __init__(self, inner: GuardTools) -> None:
        self.inner = inner
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append((tool, args))
        return self.inner.call(tool, args)


def test_paused_and_excluded_projects_get_no_guard_calls() -> None:
    guard = RecordingGuard(FixtureGuardTools(FIXTURES))
    config = read_config(SCENARIO_CONFIG)
    deps = Deps(
        guard=guard,
        notifier=cli.PrintNotifier([]),
        config=config,
        clock=lambda: parse_guard_time(DEMO_NOW),
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
        code, out = run_dry_with_config(config)
    assert code == 0
    assert f"config names project id {ghost}, which Guard did not list" in caplog.text
    assert "summary: 5 evaluated, 2 stale, 1 never ran, 1 not checked" in out


def test_future_newest_run_is_treated_as_fresh_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        code, out = run_dry(now="2026-01-01T00:00:00Z")
    assert code == 0
    assert "is in the future" in caplog.text
    assert "treated as fresh" in caplog.text
    assert "Stale Guard scans" not in out
