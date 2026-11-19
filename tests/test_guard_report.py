"""Guard reports: the fixture scenario, paging and the report parser."""

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from guard_fakes import InMemoryGuardTools, Key, call_key

from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard import (
    GuardProject,
    ListingOk,
    ProjectOk,
    fetch_project,
    list_projects,
)
from enji_guard_radar.guard_report import (
    MAX_REPORT_CHARS,
    MAX_REPORT_PAGES,
    AuditReport,
    ReportUnknown,
    parse_report_markdown,
    parse_severity,
    read_report,
)
from enji_guard_radar.model import Severity
from enji_guard_radar.ports import GuardCallError
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
        {
            call_key(
                "get_audit_report", project_id="p-1", repo_id="repo_1", task_id=task_id
            ): _page(task_id, text, "v1", None)
        }
    )
    paged = InMemoryGuardTools(
        {
            call_key(
                "get_audit_report", project_id="p-1", repo_id="repo_1", task_id=task_id
            ): _page(task_id, text[:first], "v1", "p2"),
            call_key(
                "get_audit_report", project_id="p-1", repo_id="repo_1", task_id=task_id, cursor="p2"
            ): _page(task_id, text[first:second], "v1", "p3"),
            call_key(
                "get_audit_report", project_id="p-1", repo_id="repo_1", task_id=task_id, cursor="p3"
            ): _page(task_id, text[second:], "v1", None),
        }
    )
    expected = read_report(single, task_id, "audit.security", project_id="p-1", repo_id="repo_1")
    assert isinstance(expected, AuditReport)
    assert (
        read_report(paged, task_id, "audit.security", project_id="p-1", repo_id="repo_1")
        == expected
    )
    assert len(paged.calls) == 3


def test_parsed_report_holds_no_evidence() -> None:
    dumped = _report("c001").model_dump_json()
    assert "src/example/file_one.py" not in dumped
    assert "Why it matters" not in dumped


# --- parser and paging tables -------------------------------------------------------------

TITLE_TEXT = "Secret title of the audit"
EVIDENCE_PATH = "src/private/evidence_file.py:42"


def _doc(
    *,
    title: str | None = f"# {TITLE_TEXT}",
    score: str | None = "**Score: 50 / 100** · label",
    heading: str | None = "## Findings",
    findings: str = "",
) -> str:
    parts = [title, "## Executive summary\n\nprose", score, heading, findings]
    return "\n\n".join(part for part in parts if part is not None) + "\n"


def _finding(
    heading: str = "### `F-1` — Secret finding title", severity: str | None = "High"
) -> str:
    lines = [heading, ""]
    if severity is not None:
        lines += [f"**Severity:** {severity} · **Confidence:** High", ""]
    lines += ["**Evidence:**", "", f"- `{EVIDENCE_PATH}`", ""]
    return "\n".join(lines)


def _parse(content: str, action_key: str = "audit.security") -> AuditReport | ReportUnknown:
    return parse_report_markdown(
        content, task_id="task-1", action_key=action_key, content_version="v"
    )


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        pytest.param("High", Severity.HIGH, id="high"),
        pytest.param("HIGH", Severity.HIGH, id="upper"),
        pytest.param(" high ", Severity.HIGH, id="padded"),
        pytest.param("Medium", Severity.MEDIUM, id="medium"),
        pytest.param("low", Severity.LOW, id="low"),
        pytest.param("Critical", Severity.UNKNOWN, id="critical"),
        pytest.param("Info", Severity.UNKNOWN, id="info"),
        pytest.param("", Severity.UNKNOWN, id="empty"),
    ],
)
def test_parse_severity(word: str, expected: Severity) -> None:
    assert parse_severity(word) is expected


@pytest.mark.parametrize("score", [0, 100])
def test_score_bounds_parse(score: int) -> None:
    report = _parse(_doc(score=f"**Score: {score} / 100**", findings=_finding()))
    assert isinstance(report, AuditReport)
    assert report.score == score


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(_doc(score="**Score: 101 / 100**"), id="score_101"),
        pytest.param(_doc(score="**Score: 5 / 100**\n\n**Score: 6 / 100**"), id="two_scores"),
        pytest.param(_doc(score=None), id="no_score"),
        pytest.param(_doc(title=None), id="no_title"),
        pytest.param(_doc(title="Title without hash"), id="title_without_hash"),
        pytest.param("", id="empty_content"),
        pytest.param("   \n\n", id="blank_content"),
        pytest.param(_doc(heading=None), id="no_findings_section"),
        pytest.param(
            _doc(heading="## Findings\n\n## Security findings"), id="both_findings_headings"
        ),
        pytest.param(_doc(heading="## Findings\n\n## Findings"), id="findings_heading_twice"),
    ],
)
def test_ambiguous_report_is_unknown_not_empty(content: str) -> None:
    assert isinstance(_parse(content), ReportUnknown)


def test_no_findings_with_full_score_is_zero_findings() -> None:
    report = _parse(_doc(score="**Score: 100 / 100** · label", findings="Nothing was found."))
    assert isinstance(report, AuditReport)
    assert report.findings == ()
    assert report.high_count == 0


def test_no_findings_with_a_lower_score_is_unknown() -> None:
    result = _parse(_doc(findings="Nothing was found."))
    assert isinstance(result, ReportUnknown)
    assert result.reason == "score below 100 but no findings"


def test_security_findings_heading_is_accepted() -> None:
    report = _parse(_doc(heading="## Security findings", findings=_finding()))
    assert isinstance(report, AuditReport)
    assert len(report.findings) == 1


def test_findings_in_two_sections_are_unknown() -> None:
    content = _doc(findings=_finding() + "\n## Appendix\n\n" + _finding("### `F-9` — late"))
    result = _parse(content)
    assert isinstance(result, ReportUnknown)
    assert result.reason == "findings in more than one section"


def test_findings_heading_is_recognised_by_shape_not_by_name() -> None:
    report = _parse(_doc(heading="## Подтверждённые замечания", findings=_finding()))
    assert isinstance(report, AuditReport)
    assert report.high_count == 1


def test_finding_without_backticks_gets_unknown_id() -> None:
    report = _parse(_doc(findings=_finding("### Plain heading")))
    assert isinstance(report, AuditReport)
    assert report.findings[0].finding_id == "unknown"


def test_heading_without_a_severity_line_is_not_a_finding() -> None:
    # Recommendations and criteria are `###` blocks too; only a severity line makes a finding.
    report = _parse(_doc(score="**Score: 100 / 100**", findings=_finding(severity=None)))
    assert isinstance(report, AuditReport)
    assert report.findings == ()


def test_severity_line_outside_a_finding_block_is_unknown() -> None:
    content = _doc(findings=_finding()) + "\n**Severity:** High\n"
    result = _parse(content)
    assert isinstance(result, ReportUnknown)
    assert result.reason == "severity line outside a finding"


def test_critical_severity_is_kept_as_unknown_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        report = _parse(_doc(findings=_finding(severity="Critical")))
    assert isinstance(report, AuditReport)
    assert report.findings[0].severity is Severity.UNKNOWN
    assert report.findings[0].severity_raw == "Critical"
    assert "task-1" in caplog.text
    assert "F-1" in caplog.text
    assert "Critical" in caplog.text
    assert "Secret finding title" not in caplog.text
    assert EVIDENCE_PATH not in caplog.text


def test_findings_keep_report_order_and_duplicate_ids() -> None:
    findings = _finding("### `F-3` — a", "Medium") + _finding("### `F-1` — b", "Low")
    findings += _finding("### `F-1` — c", "High")
    report = _parse(_doc(findings=findings))
    assert isinstance(report, AuditReport)
    assert [(f.finding_id, f.severity) for f in report.findings] == [
        ("F-3", Severity.MEDIUM),
        ("F-1", Severity.LOW),
        ("F-1", Severity.HIGH),
    ]
    assert report.high_count == 1


RUSSIAN_REPORT = """# Заголовок отчёта

## Краткий вывод

текст

**Оценка: 76 / 100** · метка · Уверенность: Средняя

## Подтверждённые проблемы

### `F-003` — заголовок

**Серьёзность:** Высокая · **Уверенность:** Средняя

### `F-001` — заголовок

**Серьёзность:** Средняя · **Уверенность:** Высокая

### `F-004` — заголовок

**Серьёзность:** Информация · **Уверенность:** Высокая

## Как рассчитана оценка

**Оценка: 76 / 100**
"""


def test_russian_report_with_the_score_repeated_is_read() -> None:
    report = _parse(RUSSIAN_REPORT)
    assert isinstance(report, AuditReport)
    assert report.score == 76
    assert [f.severity for f in report.findings] == [Severity.HIGH, Severity.MEDIUM, Severity.LOW]
    assert report.high_count == 1


def test_score_repeated_with_the_same_value_is_one_score() -> None:
    content = _doc(score="**Score: 50 / 100** · a\n\n**Score: 50 / 100**", findings=_finding())
    report = _parse(content)
    assert isinstance(report, AuditReport)
    assert report.score == 50


def test_score_lines_that_differ_are_unknown() -> None:
    result = _parse(_doc(score="**Score: 5 / 100**\n\n**Score: 6 / 100**", findings=_finding()))
    assert isinstance(result, ReportUnknown)
    assert result.reason == "score lines differ"


def test_recon_is_an_overview_without_score_or_findings() -> None:
    report = _parse(
        "# Repository Overview\n\n## Main Findings\n\n### What Looks Good\n", "audit.recon"
    )
    assert isinstance(report, AuditReport)
    assert (report.layout, report.score, report.findings) == ("overview", None, ())


def test_unknown_report_logs_ids_and_reason_but_no_report_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        result = _parse(_doc(score="**Score: 101 / 100**", findings=_finding()))
    assert isinstance(result, ReportUnknown)
    assert "task-1" in caplog.text
    assert "audit.security" in caplog.text
    assert TITLE_TEXT not in caplog.text
    assert EVIDENCE_PATH not in caplog.text
    assert TITLE_TEXT not in result.reason


# --- read_report --------------------------------------------------------------------------

TASK = "task-1"
GOOD = _doc(findings=_finding())


def _tools(*pages: dict[str, object] | Exception) -> InMemoryGuardTools:
    """Pages answer in order: the first without a cursor, then cursors p2, p3, ..."""
    responses: dict[Key, dict[str, object] | Exception] = {}
    for index, page in enumerate(pages):
        key = call_key("get_audit_report", task_id=TASK)
        if index:
            key = call_key("get_audit_report", task_id=TASK, cursor=f"p{index + 1}")
        responses[key] = page
    return InMemoryGuardTools(responses)


def _more(index: int, text: str = "", **changes: object) -> dict[str, object]:
    page = _page(TASK, text, "v1", f"p{index + 2}")
    page.update(changes)
    return page


def _last(text: str, **changes: object) -> dict[str, object]:
    page = _page(TASK, text, "v1", None)
    page.update(changes)
    return page


@pytest.mark.parametrize(
    "tools",
    [
        pytest.param(
            _tools(_more(0, GOOD[:20]), _last(GOOD[20:], content_version="v2")),
            id="content_version_changes",
        ),
        pytest.param(_tools(_more(0, GOOD, next_cursor=None)), id="has_more_without_cursor"),
        pytest.param(_tools(_last(GOOD, report_status="pending")), id="report_pending"),
        pytest.param(_tools(_last(GOOD, task_id="other")), id="task_id_differs"),
        pytest.param(_tools(_more(0, GOOD[:20]), GuardCallError("boom")), id="page_2_call_fails"),
        pytest.param(_tools(GuardCallError("boom")), id="page_1_call_fails"),
        pytest.param(_tools(_last("", content=None)), id="content_not_a_string"),
        pytest.param(_tools(_last(GOOD, has_more="no")), id="has_more_not_a_bool"),
        pytest.param(_tools(_last(GOOD, content_version=7)), id="content_version_not_a_string"),
        pytest.param(_tools(), id="no_page_recorded"),
    ],
)
def test_read_report_failures_are_unknown(
    tools: InMemoryGuardTools, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = read_report(tools, TASK, "audit.security", project_id="p-1", repo_id="repo_1")
    assert isinstance(result, ReportUnknown)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]
    assert TASK in caplog.text


def test_read_report_more_than_50_pages_is_unknown() -> None:
    class Forever:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
            self.calls += 1
            return _page(TASK, "x", "v1", "next")

    tools = Forever()
    assert isinstance(
        read_report(tools, TASK, "audit.security", project_id="p-1", repo_id="repo_1"),
        ReportUnknown,
    )
    assert tools.calls == MAX_REPORT_PAGES


def test_read_report_too_many_characters_is_unknown() -> None:
    big = "x" * (MAX_REPORT_CHARS // 2 + 1)
    tools = _tools(_more(0, big), _last(big))
    assert isinstance(
        read_report(tools, TASK, "audit.security", project_id="p-1", repo_id="repo_1"),
        ReportUnknown,
    )


def test_read_report_non_object_page_is_unknown() -> None:
    class ListTools:
        def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
            return cast("Mapping[str, object]", ["not", "an", "object"])

    assert isinstance(
        read_report(ListTools(), TASK, "audit.security", project_id="p-1", repo_id="repo_1"),
        ReportUnknown,
    )
