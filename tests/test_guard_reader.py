"""Failed, empty and partial Guard fetches are unknown, never data (SRC-04)."""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from guard_fakes import InMemoryGuardTools, Key, call_key

from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard import (
    GUARD_READ_TOOLS,
    MAX_PAGES,
    GuardProject,
    ListingOk,
    ListingUnknown,
    ProjectOk,
    ProjectUnknown,
    fetch_project,
    list_projects,
)
from enji_guard_radar.guard_report import ReportUnknown
from enji_guard_radar.ports import GuardCallError
from enji_guard_radar.stale import StaleVerdict, evaluate_stale

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
PROJECT = GuardProject(id="p1", name="P")
REPO = {"id": "r1", "project_id": "p1", "name": "repo", "provider": "git", "connected": True}
Response = Mapping[str, object] | Exception


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _run(
    task_id: str,
    linked: datetime,
    completed: datetime | None,
    *,
    status: str = "completed",
    action_key: str = "audit.tests",
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "action_key": action_key,
        "status": status,
        "report_status": "ready" if completed else "pending",
        "created_at": _stamp(linked),
        "linked_at": _stamp(linked),
        "completed_at": _stamp(completed) if completed else None,
    }


def _runs(items: list[dict[str, object]], cursor: str | None = None) -> dict[str, object]:
    page: dict[str, object] = {"runs": items, "has_more": cursor is not None}
    if cursor is not None:
        page["next_cursor"] = cursor
    return page


def _repos(items: list[dict[str, object]], cursor: str | None = None) -> dict[str, object]:
    page: dict[str, object] = {"repositories": items, "has_more": cursor is not None}
    if cursor is not None:
        page["next_cursor"] = cursor
    return page


def _project_tools(*run_pages: Response) -> InMemoryGuardTools:
    """One repository r1 whose run pages answer in order (cursors c2, c3, ...)."""
    responses: dict[Key, Response] = {
        call_key("list_repositories", project_id="p1"): _repos([REPO])
    }
    for index, page in enumerate(run_pages):
        key = call_key("list_audit_runs", project_id="p1", repo_id="r1")
        if index:
            key = call_key("list_audit_runs", project_id="p1", repo_id="r1", cursor=f"c{index + 1}")
        responses[key] = page
    return InMemoryGuardTools(responses)


# --- list_projects ------------------------------------------------------------------------

PROJECT_ITEM = {"id": "p1", "name": "P"}
OTHER_ITEM = {"id": "p2", "name": "Q"}


def _listing_tools(*pages: Response) -> InMemoryGuardTools:
    responses: dict[Key, Response] = {}
    for index, page in enumerate(pages):
        key = call_key("list_projects")
        if index:
            key = call_key("list_projects", cursor=f"c{index + 1}")
        responses[key] = page
    return InMemoryGuardTools(responses)


def test_listing_call_failure_is_unknown(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = list_projects(_listing_tools(GuardCallError("down")))
    assert isinstance(result, ListingUnknown)
    assert "list_projects" in caplog.text
    assert "down" in caplog.text


def test_listing_with_no_projects_is_unknown(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        result = list_projects(_listing_tools({"projects": [], "has_more": False}))
    assert result == ListingUnknown(reason="list_projects returned no projects")
    assert "no projects" in caplog.text or "empty" in caplog.text


def test_listing_continues_past_an_empty_page_while_has_more() -> None:
    tools = _listing_tools(
        {"projects": [], "has_more": True, "next_cursor": "c2"},
        {"projects": [PROJECT_ITEM, OTHER_ITEM], "has_more": False},
    )
    result = list_projects(tools)
    assert isinstance(result, ListingOk)
    assert [p.id for p in result.projects] == ["p1", "p2"]


@pytest.mark.parametrize(
    "page",
    [
        pytest.param({"projects": [PROJECT_ITEM], "has_more": True}, id="has_more_without_cursor"),
        pytest.param({"projects": [{"id": "p1"}], "has_more": False}, id="project_without_name"),
        pytest.param({"has_more": False}, id="no_projects_key"),
        pytest.param({"projects": [PROJECT_ITEM]}, id="no_has_more"),
    ],
)
def test_malformed_listing_is_unknown(page: dict[str, object]) -> None:
    assert isinstance(list_projects(_listing_tools(page)), ListingUnknown)


def test_listing_that_never_ends_is_unknown_after_50_pages() -> None:
    class Forever:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
            self.calls += 1
            return {"projects": [PROJECT_ITEM], "has_more": True, "next_cursor": "again"}

    tools = Forever()
    assert isinstance(list_projects(tools), ListingUnknown)
    assert tools.calls == MAX_PAGES


# --- fetch_project: failures are unknown --------------------------------------------------


def test_repositories_failing_on_page_2_is_interrupted_and_unknown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tools = InMemoryGuardTools(
        {
            call_key("list_repositories", project_id="p1"): _repos([REPO], "c2"),
            call_key("list_repositories", project_id="p1", cursor="c2"): GuardCallError("cut"),
        }
    )
    with caplog.at_level(logging.WARNING):
        result = fetch_project(tools, PROJECT)
    assert isinstance(result, ProjectUnknown)
    assert result.reason.startswith("list_repositories failed")
    assert "list_repositories failed" in caplog.text


def test_runs_page_failing_midway_is_interrupted_and_never_a_partial_ok() -> None:
    linked = NOW - timedelta(days=1)
    tools = _project_tools(
        _runs([_run("x", linked, linked + timedelta(minutes=5))], "c2"),
        GuardCallError("cut"),
    )
    result = fetch_project(tools, PROJECT)
    assert isinstance(result, ProjectUnknown)
    assert result.reason.startswith("list_audit_runs failed")


def test_malformed_runs_page_is_unknown() -> None:
    bad = _run("x", NOW, NOW)
    del bad["created_at"]
    assert isinstance(fetch_project(_project_tools(_runs([bad])), PROJECT), ProjectUnknown)


def test_report_failure_leaves_the_project_ok_with_an_unknown_report() -> None:
    linked = NOW - timedelta(days=2)
    tools = _project_tools(_runs([_run("x", linked, linked + timedelta(minutes=5))]))
    result = fetch_project(tools, PROJECT)
    assert isinstance(result, ProjectOk)
    (report,) = result.repositories[0].reports
    assert isinstance(report, ReportUnknown)
    assert evaluate_stale(result, 14, NOW).verdict is StaleVerdict.FRESH


def test_only_the_newest_counting_run_per_audit_type_gets_a_report() -> None:
    linked = NOW - timedelta(days=5)
    done = linked + timedelta(minutes=5)
    runs = [
        _run("newest", linked, done + timedelta(days=1)),
        _run("older", linked, done),
        _run("running", linked, None, status="running"),
        _run("other-type", linked, done, action_key="audit.security"),
    ]
    result = fetch_project(_project_tools(_runs(runs)), PROJECT)
    assert isinstance(result, ProjectOk)
    assert [r.task_id for r in result.repositories[0].reports] == [
        "other-type",
        "newest",
    ]  # sorted by action_key


def _skip_world() -> InMemoryGuardTools:
    linked = NOW - timedelta(days=5)
    done = linked + timedelta(minutes=5)
    runs = [
        _run("t1", linked, done, action_key="audit.security"),
        _run("t2", linked, done, action_key="audit.tests"),
    ]
    return _project_tools(_runs(runs))


def _report_reads(tools: InMemoryGuardTools) -> list[str]:
    return [args["task_id"] for tool, args in tools.calls if tool == "get_audit_report"]


def test_skip_report_true_makes_no_read_for_that_task() -> None:
    tools = _skip_world()
    asked: list[tuple[str, str, str]] = []

    def skip(repo_id: str, action_key: str, task_id: str) -> bool:
        asked.append((repo_id, action_key, task_id))
        return task_id == "t1"

    result = fetch_project(tools, PROJECT, skip_report=skip)
    assert isinstance(result, ProjectOk)
    assert _report_reads(tools) == ["t2"]
    assert [r.task_id for r in result.repositories[0].reports] == ["t2"]
    assert ("r1", "audit.security", "t1") in asked


def test_skip_report_absent_reads_every_newest_report() -> None:
    tools = _skip_world()
    result = fetch_project(tools, PROJECT)
    assert isinstance(result, ProjectOk)
    assert _report_reads(tools) == ["t1", "t2"]
    assert [r.task_id for r in result.repositories[0].reports] == ["t1", "t2"]


def test_equal_completion_times_pick_the_first_run_in_guard_order() -> None:
    linked = NOW - timedelta(days=5)
    done = linked + timedelta(minutes=5)
    result = fetch_project(
        _project_tools(_runs([_run("first", linked, done), _run("second", linked, done)])),
        PROJECT,
    )
    assert isinstance(result, ProjectOk)
    assert [r.task_id for r in result.repositories[0].reports] == ["first"]
    assert result.newest_counting_run_at == done


# --- zero items are real, malformed answers are not ---------------------------------------


@pytest.mark.parametrize(
    ("repositories_page", "ok"),
    [
        pytest.param({"repositories": [], "has_more": False}, True, id="zero_items"),
        pytest.param({}, False, id="empty_object"),
        pytest.param({"repositories": None, "has_more": False}, False, id="null_list"),
        pytest.param({"repositories": "x", "has_more": False}, False, id="string_list"),
        pytest.param({"repositories": []}, False, id="no_has_more"),
        pytest.param({"repositories": [], "has_more": True}, False, id="has_more_without_cursor"),
        pytest.param(
            {"repositories": [], "has_more": True, "next_cursor": "c"},
            False,
            id="later_page_not_recorded",
        ),
    ],
)
def test_zero_repositories_is_real_but_malformed_is_unknown(
    repositories_page: dict[str, object], ok: bool
) -> None:
    tools = InMemoryGuardTools({call_key("list_repositories", project_id="p1"): repositories_page})
    result = fetch_project(tools, PROJECT)
    verdict = evaluate_stale(result, 14, NOW).verdict
    if ok:
        assert isinstance(result, ProjectOk)
        assert result.repositories == ()
        assert verdict is StaleVerdict.NEVER_RAN
    else:
        assert isinstance(result, ProjectUnknown)
        assert verdict is StaleVerdict.UNKNOWN


@pytest.mark.parametrize(
    ("runs_page", "ok"),
    [
        pytest.param({"runs": [], "has_more": False}, True, id="zero_runs"),
        pytest.param({}, False, id="empty_object"),
        pytest.param({"runs": None, "has_more": False}, False, id="null_list"),
        pytest.param({"runs": []}, False, id="no_has_more"),
    ],
)
def test_zero_runs_is_real_but_malformed_is_unknown(runs_page: dict[str, object], ok: bool) -> None:
    result = fetch_project(_project_tools(runs_page), PROJECT)
    verdict = evaluate_stale(result, 14, NOW).verdict
    if ok:
        assert isinstance(result, ProjectOk)
        assert result.repositories[0].runs == ()
        assert verdict is StaleVerdict.NEVER_RAN
    else:
        assert isinstance(result, ProjectUnknown)
        assert verdict is StaleVerdict.UNKNOWN


# --- run paging ---------------------------------------------------------------------------

T = datetime(2026, 11, 10, 12, tzinfo=UTC)


def _requested_cursors(tools: InMemoryGuardTools) -> list[str | None]:
    return [args.get("cursor") for tool, args in tools.calls if tool == "list_audit_runs"]


def test_newest_completion_on_later_page_is_found_and_page_3_is_never_requested() -> None:
    tools = _project_tools(
        _runs([_run("x", T, T + timedelta(minutes=20))], "c2"),
        _runs(
            [
                _run("y", T - timedelta(hours=1), T + timedelta(hours=5)),
                _run("z", T - timedelta(days=3), T - timedelta(days=3) + timedelta(hours=1)),
            ],
            "c3",
        ),
    )
    result = fetch_project(tools, PROJECT)
    assert isinstance(result, ProjectOk)
    assert result.newest_counting_run_at == T + timedelta(hours=5)
    assert _requested_cursors(tools) == [None, "c2"]


def test_paging_stops_when_the_first_page_is_older_than_a_day_before_its_newest_completion() -> (
    None
):
    tools = _project_tools(
        _runs([_run("x", T - timedelta(hours=30), T)], "c2"),
    )
    result = fetch_project(tools, PROJECT)
    assert isinstance(result, ProjectOk)
    assert _requested_cursors(tools) == [None]


def test_page_with_only_non_counting_runs_leads_to_the_next_page() -> None:
    tools = _project_tools(
        _runs([_run("x", T, None, status="running")], "c2"),
        _runs([_run("y", T - timedelta(hours=2), T)]),
    )
    result = fetch_project(tools, PROJECT)
    assert isinstance(result, ProjectOk)
    assert _requested_cursors(tools) == [None, "c2"]
    assert result.newest_counting_run_at == T


def test_out_of_order_page_logs_a_warning_and_reads_every_page(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tools = _project_tools(
        _runs([_run("x", T - timedelta(days=5), T - timedelta(days=5))], "c2"),
        _runs([_run("y", T, T + timedelta(minutes=5))], "c3"),
        _runs([_run("z", T - timedelta(days=9), T - timedelta(days=9))]),
    )
    with caplog.at_level(logging.WARNING):
        result = fetch_project(tools, PROJECT)
    assert isinstance(result, ProjectOk)
    assert _requested_cursors(tools) == [None, "c2", "c3"]
    assert "not newest first" in caplog.text
    assert result.newest_counting_run_at == T + timedelta(minutes=5)


def test_runs_that_never_end_are_unknown_after_50_pages() -> None:
    class Forever:
        def __init__(self) -> None:
            self.run_calls = 0

        def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
            if tool == "list_repositories":
                return _repos([REPO])
            self.run_calls += 1
            return _runs([_run(f"r{self.run_calls}", T, None, status="running")], "again")

    tools = Forever()
    assert isinstance(fetch_project(tools, PROJECT), ProjectUnknown)
    assert tools.run_calls == MAX_PAGES


# --- the fixture scenario -----------------------------------------------------------------


class Recording:
    def __init__(self, inner: FixtureGuardTools) -> None:
        self.inner = inner
        self.calls: list[tuple[str, dict[str, str]]] = []

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append((tool, dict(args)))
        return self.inner.call(tool, args)


def test_scenario_project_a_requests_two_run_pages_and_finds_c012() -> None:
    tools = Recording(FixtureGuardTools(FIXTURES))
    project = GuardProject(id="00000000-0000-4000-8000-00000000a001", name="Project A")
    result = fetch_project(tools, project)
    assert isinstance(result, ProjectOk)
    assert result.newest_counting_run_at == datetime(2026, 11, 10, 15, 12, 40, 4417, tzinfo=UTC)
    cursors = [
        args.get("cursor")
        for tool, args in tools.calls
        if tool == "list_audit_runs" and args["repo_id"].endswith("b001")
    ]
    assert cursors == [None, "<opaque-cursor>"]
    assert {tool for tool, _ in tools.calls} <= GUARD_READ_TOOLS


def test_guard_read_tools_are_the_four_used_tools() -> None:
    assert sorted(GUARD_READ_TOOLS) == [
        "get_audit_report",
        "list_audit_runs",
        "list_projects",
        "list_repositories",
    ]


def _runs_complete(*pages: dict[str, object]) -> bool:
    result = fetch_project(_project_tools(*pages), PROJECT)
    assert isinstance(result, ProjectOk)
    return result.repositories[0].runs_complete


def test_runs_complete_for_a_single_last_page() -> None:
    assert _runs_complete(_runs([_run("x", T, T)])) is True


def test_runs_complete_when_two_pages_are_read_to_the_end() -> None:
    pages = (_runs([_run("x", T, T)], "c2"), _runs([_run("y", T - timedelta(hours=2), T)]))
    assert _runs_complete(*pages) is True


def test_runs_complete_for_a_well_formed_empty_run_list() -> None:
    assert _runs_complete({"runs": [], "has_more": False}) is True


def test_runs_not_complete_when_paging_stopped_early() -> None:
    assert _runs_complete(_runs([_run("x", T - timedelta(hours=30), T)], "c2")) is False
