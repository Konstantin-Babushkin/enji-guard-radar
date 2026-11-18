"""Contract over recorded real Guard fixtures (`fixtures/guard-real*`), made by `probe`.

Until plan 02-11 records a real directory, the contract code runs on a directory that `probe`
makes from a dirty fake Guard, so CI proves the checks themselves. `test_a_real_recording_exists`
skips until a recording exists; plan 02-11 sets REQUIRE_RECORDING = True and the skip becomes a
failure.
"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_probe import dirty_session, run_probe

from enji_guard_radar.fixture_source import MANIFEST_FILE, FixtureGuardTools
from enji_guard_radar.guard import ListingOk, ProjectOk, fetch_project, list_projects
from enji_guard_radar.guard_report import AuditReport
from enji_guard_radar.probe_sanitize import scan_for_leaks

# Plan 02-11 records the first real directory and flips this to True.
REQUIRE_RECORDING = False

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECORDED = sorted(path for path in FIXTURES.glob("guard-real*") if (path / MANIFEST_FILE).is_file())
KNOWN_SEVERITY_WORDS = frozenset({"High", "Medium", "Low"})
MAX_SCORE = 100


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """`probe` installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def check_recorded_directory(directory: Path) -> None:
    """Hold one recorded directory to the parser and the leak rules; fail with a plain reason."""
    manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
    errors = [call for call in manifest["calls"] if "error" in call]
    failed_reports = {
        call["args"].get("task_id") for call in errors if call["tool"] == "get_audit_report"
    }
    tools = FixtureGuardTools(directory)
    listing = list_projects(tools)
    assert isinstance(listing, ListingOk), "recorded project listing is not readable"
    for project in listing.projects:
        data = fetch_project(tools, project)
        if not isinstance(data, ProjectOk):
            assert errors, f"unknown project {project.id}: {data.reason}"
            continue
        for repo in data.repositories:
            for report in repo.reports:
                if not isinstance(report, AuditReport):
                    assert report.task_id in failed_reports, (
                        f"report {report.task_id} does not parse: {report.reason}"
                    )
                    continue
                if report.score is not None:
                    assert 0 <= report.score <= MAX_SCORE, f"score out of range in {report.task_id}"
                for finding in report.findings:
                    assert finding.severity_raw in KNOWN_SEVERITY_WORDS, (
                        f"severity word {finding.severity_raw!r} in {report.task_id}: "
                        "decide in guard_report.parse_severity"
                    )
    texts = {
        path.name: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.json"))
    }
    assert scan_for_leaks(texts) == []


def test_a_real_recording_exists() -> None:
    if RECORDED:
        return
    if REQUIRE_RECORDING:
        pytest.fail("no fixtures/guard-real* recording: run `enji-guard-radar probe` (plan 02-11)")
    pytest.skip(
        "no real Guard recording yet; plan 02-11 records one and sets REQUIRE_RECORDING = True"
    )


def test_every_recorded_directory_meets_the_contract() -> None:
    for directory in RECORDED:
        check_recorded_directory(directory)


def test_a_probe_made_directory_meets_the_contract(tmp_path: Path) -> None:
    out_dir = tmp_path / "guard-real-synthetic"
    code, _output = run_probe(out_dir, dirty_session(), tmp_path)
    assert code == 0
    check_recorded_directory(out_dir)


def test_the_contract_fails_on_a_severity_word_it_does_not_know(tmp_path: Path) -> None:
    out_dir = tmp_path / "guard-real-synthetic"
    run_probe(out_dir, dirty_session(), tmp_path)
    report = next(p for p in out_dir.glob("get_audit_report_*.json") if "Security" in p.read_text())
    report.write_text(report.read_text().replace("**Severity:** High", "**Severity:** Critical"))
    with pytest.raises(AssertionError, match=r"'Critical'.*decide in guard_report.parse_severity"):
        check_recorded_directory(out_dir)


def test_the_contract_fails_on_a_leak(tmp_path: Path) -> None:
    out_dir = tmp_path / "guard-real-synthetic"
    run_probe(out_dir, dirty_session(), tmp_path)
    (out_dir / "list_projects_1.json").write_text('{"projects": [], "owner": "jane@corp.com"}')
    with pytest.raises(AssertionError):
        check_recorded_directory(out_dir)
