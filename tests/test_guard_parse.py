"""Defensive parsing of Guard timestamps, statuses and run pages (SRC-03)."""

import logging
from datetime import UTC, datetime

import pytest

from enji_guard_radar.guard import (
    AuditRun,
    GuardDataError,
    GuardProject,
    GuardRepository,
    ProjectOk,
    ReportStatus,
    RepoRuns,
    RunStatus,
    parse_guard_time,
    parse_runs_page,
)

DONE = "2026-11-09T01:00:00Z"


@pytest.mark.parametrize(
    ("value", "expected", "microsecond"),
    [
        pytest.param(
            "2026-11-09T23:04:23.399Z",
            datetime(2026, 11, 9, 23, 4, 23, 399000, tzinfo=UTC),
            399000,
            id="fraction_3_digits",
        ),
        pytest.param(
            "2026-11-10T14:04:55.0961Z",
            datetime(2026, 11, 10, 14, 4, 55, 96100, tzinfo=UTC),
            96100,
            id="fraction_4_digits",
        ),
        pytest.param(
            "2026-11-09T23:04:21.836491Z",
            datetime(2026, 11, 9, 23, 4, 21, 836491, tzinfo=UTC),
            836491,
            id="fraction_6_digits",
        ),
        pytest.param(
            "2026-11-09T23:04:21Z",
            datetime(2026, 11, 9, 23, 4, 21, tzinfo=UTC),
            0,
            id="fraction_none",
        ),
        pytest.param(
            "2026-11-09T23:04:21.5+02:00",
            datetime(2026, 11, 9, 21, 4, 21, 500000, tzinfo=UTC),
            500000,
            id="offset_converted_to_utc",
        ),
    ],
)
def test_parse_guard_time_accepts(value: str, expected: datetime, microsecond: int) -> None:
    parsed = parse_guard_time(value)
    assert parsed == expected
    assert parsed.utcoffset() == expected.utcoffset()
    assert parsed.microsecond == microsecond


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("2026-11-09T23:04:21.8364911Z", id="seven_digit_fraction"),
        pytest.param("2026-11-09T23:04:21", id="naive_no_zone"),
        pytest.param("2026-11-09", id="date_only"),
        pytest.param("2026-02-30T00:00:00Z", id="impossible_date"),
        pytest.param("2026-11-09T23:04:21z", id="lowercase_z"),
        pytest.param("", id="empty"),
    ],
)
def test_parse_guard_time_rejects(value: str) -> None:
    with pytest.raises(GuardDataError):
        parse_guard_time(value)


def _run(
    *,
    status: str = "completed",
    report: str = "ready",
    completed: str | None = DONE,
    task_id: str = "t1",
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "action_key": "audit.tests",
        "status": status,
        "report_status": report,
        "created_at": "2026-11-09T00:00:00Z",
        "linked_at": "2026-11-09T00:00:00Z",
        "completed_at": completed,
        "unexpected_extra_field": 1,
    }


def _parse(item: dict[str, object]) -> AuditRun:
    return parse_runs_page({"runs": [item], "has_more": False}).items[0]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param("completed", RunStatus.COMPLETED, id="completed"),
        pytest.param(" Completed ", RunStatus.COMPLETED, id="padded_mixed_case"),
        pytest.param("running", RunStatus.UNKNOWN, id="running"),
        pytest.param("failed", RunStatus.UNKNOWN, id="failed"),
        pytest.param("queued", RunStatus.UNKNOWN, id="queued"),
        pytest.param("cancelled", RunStatus.UNKNOWN, id="cancelled"),
        pytest.param("", RunStatus.UNKNOWN, id="empty"),
    ],
)
def test_run_status(status: str, expected: RunStatus) -> None:
    run = _parse(_run(status=status))
    assert run.status is expected
    assert run.status_raw == status


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        pytest.param("ready", ReportStatus.READY, id="ready"),
        pytest.param("pending", ReportStatus.PENDING, id="pending"),
        pytest.param("failed", ReportStatus.UNKNOWN, id="other"),
    ],
)
def test_report_status(report: str, expected: ReportStatus) -> None:
    run = _parse(_run(report=report))
    assert run.report_status is expected
    assert run.report_status_raw == report


@pytest.mark.parametrize(
    ("item", "counts"),
    [
        pytest.param(_run(), True, id="completed_and_ready"),
        pytest.param(_run(report="pending"), False, id="report_pending"),
        pytest.param(_run(completed=None), False, id="no_completed_at"),
        pytest.param(_run(status="running", report="pending", completed=None), False, id="running"),
        pytest.param(_run(status="failed"), False, id="failed"),
    ],
)
def test_only_completed_ready_runs_count(item: dict[str, object], counts: bool) -> None:
    assert _parse(item).counts is counts


def test_identical_completion_times_both_count() -> None:
    repository = GuardRepository(id="r", project_id="p", name="n", provider="git", connected=True)
    page = parse_runs_page({"runs": [_run(task_id="a"), _run(task_id="b")], "has_more": False})
    assert all(run.counts for run in page.items)
    data = ProjectOk(
        project=GuardProject(id="p", name="P"),
        repositories=(RepoRuns(repository=repository, runs=page.items),),
    )
    assert data.newest_counting_run_at == datetime(2026, 11, 9, 1, tzinfo=UTC)


@pytest.mark.parametrize("missing", ["task_id", "action_key", "status", "created_at"])
def test_run_page_missing_field_is_rejected(missing: str) -> None:
    item = _run()
    del item[missing]
    with pytest.raises(GuardDataError):
        parse_runs_page({"runs": [item], "has_more": False})


def test_unknown_run_status_logs_a_warning_and_keeps_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        run = _parse(_run(status="cancelled", task_id="task-77"))
    assert run.status_raw == "cancelled"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "task-77" in warnings[0].getMessage()
    assert "cancelled" in warnings[0].getMessage()


def test_completed_run_without_completion_time_logs_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        run = _parse(_run(completed=None, task_id="task-78"))
    assert not run.counts
    assert any("task-78" in r.getMessage() for r in caplog.records)


def test_unknown_report_status_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _parse(_run(report="weird", task_id="task-79"))
    assert any("task-79" in r.getMessage() and "weird" in r.getMessage() for r in caplog.records)
