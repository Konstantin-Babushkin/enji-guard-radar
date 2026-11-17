"""Guard reports: the fixture scenario, paging and the report parser."""

import json
from datetime import UTC, datetime
from pathlib import Path

from guard_fakes import InMemoryGuardTools, call_key

from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard import (
    GuardProject,
    ListingOk,
    ProjectOk,
    fetch_project,
    list_projects,
)
from enji_guard_radar.guard_report import AuditReport, ReportUnknown, read_report
from enji_guard_radar.model import Severity
from enji_guard_radar.stale import StaleVerdict, evaluate_stale

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
PROJECT_A_ID = "00000000-0000-4000-8000-00000000a001"
SECURITY = "get_audit_report_security.json"


def _project_a() -> ProjectOk:
    data = fetch_project(
        FixtureGuardTools(FIXTURES), GuardProject(id=PROJECT_A_ID, name="Project A")
    )
    assert isinstance(data, ProjectOk)
    return data


def _reports_by_run() -> dict[str, AuditReport | ReportUnknown]:
    return {
        report.task_id[-4:]: report for repo in _project_a().repositories for report in repo.reports
    }


def _report(run: str) -> AuditReport:
    report = _reports_by_run()[run]
    assert isinstance(report, AuditReport)
    return report


def test_scenario_project_a_reads_reports() -> None:
    reports = _reports_by_run()
    security = _report("c001")
    assert security.score == 31
    assert [(f.finding_id, f.severity) for f in security.findings] == [
        ("F-001", Severity.HIGH),
        ("F-002", Severity.MEDIUM),
    ]
    assert security.high_count == 1
    for run in ("c002", "c003", "c012", "c013"):
        assert isinstance(reports[run], ReportUnknown)
    readiness = _report("c004")
    assert readiness.score == 81
    assert [(f.finding_id, f.severity) for f in readiness.findings] == [
        ("F-003", Severity.MEDIUM),
        ("F-001", Severity.LOW),
    ]
    tests = _report("c005")
    assert tests.score == 79
    assert [f.finding_id for f in tests.findings] == [
        "SERVICE-CALLBACK-IDENTITY",
        "TESTS-OUTPUT-PATH-FIXTURE",
    ]
    recon = _report("c006")
    assert recon.layout == "overview"
    assert recon.score is None
    assert recon.findings == ()
    assert "c011" not in reports


def test_stale_verdicts_unchanged_by_reports() -> None:
    tools = FixtureGuardTools(FIXTURES)
    listing = list_projects(tools)
    assert isinstance(listing, ListingOk)
    now = datetime(2026, 11, 16, 12, tzinfo=UTC)
    verdicts = {
        project.id[-4:]: evaluate_stale(fetch_project(tools, project), 14, now).verdict
        for project in listing.projects
    }
    assert verdicts == {
        "a001": StaleVerdict.FRESH,
        "a002": StaleVerdict.STALE,
        "a003": StaleVerdict.NEVER_RAN,
        "a004": StaleVerdict.UNKNOWN,
        "a005": StaleVerdict.STALE,
        "a006": StaleVerdict.FRESH,
    }


def _page(task_id: str, content: str, version: str, next_cursor: str | None) -> dict[str, object]:
    page: dict[str, object] = {
        "task_id": task_id,
        "report_status": "ready",
        "content": content,
        "content_version": version,
        "has_more": next_cursor is not None,
    }
    if next_cursor is not None:
        page["next_cursor"] = next_cursor
    return page


def test_report_read_across_three_pages_equals_single_page() -> None:
    document = json.loads((FIXTURES / SECURITY).read_text(encoding="utf-8"))
    task_id, text = document["task_id"], document["content"]
    first, second = len(text) // 3, 2 * len(text) // 3
    single = InMemoryGuardTools(
        {call_key("get_audit_report", task_id=task_id): _page(task_id, text, "v1", None)}
    )
    paged = InMemoryGuardTools(
        {
            call_key("get_audit_report", task_id=task_id): _page(task_id, text[:first], "v1", "p2"),
            call_key("get_audit_report", task_id=task_id, cursor="p2"): _page(
                task_id, text[first:second], "v1", "p3"
            ),
            call_key("get_audit_report", task_id=task_id, cursor="p3"): _page(
                task_id, text[second:], "v1", None
            ),
        }
    )
    expected = read_report(single, task_id, "audit.security")
    assert isinstance(expected, AuditReport)
    assert read_report(paged, task_id, "audit.security") == expected
    assert len(paged.calls) == 3


def test_parsed_report_holds_no_evidence() -> None:
    dumped = _report("c001").model_dump_json()
    assert "src/example/file_one.py" not in dumped
    assert "Why it matters" not in dumped
