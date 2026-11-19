"""Consistency of the synthetic Guard scenario in fixtures/guard (standard library only)."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
READ_TOOLS = {"list_projects", "list_repositories", "list_audit_runs", "get_audit_report"}
# b001 page 3 is deliberately not recorded: after page 2 the oldest linked_at plus 24 h falls
# before the newest completion, so the run-paging bound (plan 01-02) never requests it.
DELIBERATELY_UNRECORDED = {
    ("list_audit_runs", "repo_00000000-0000-4000-8000-00000000b001", "<opaque-cursor-2>"),
}
# Newest completed and ready completed_at per project; None means no repositories ("never ran").
EXPECTED_NEWEST: dict[str, str | None] = {
    "00000000-0000-4000-8000-00000000a001": "2026-11-10T15:12:40.004417Z",
    "00000000-0000-4000-8000-00000000a002": "2026-10-20T09:47:09.330000Z",
    "00000000-0000-4000-8000-00000000a003": None,
    "00000000-0000-4000-8000-00000000a005": "2026-08-04T07:12:45.000001Z",
    "00000000-0000-4000-8000-00000000a006": "2026-11-14T16:30:00.250Z",
}
# Project D: its list_repositories call is a recorded error ("unknown", no data).
ERROR_PROJECT = "00000000-0000-4000-8000-00000000a004"

Json = dict[str, Any]


def _load(path: Path) -> Json:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _calls() -> list[Json]:
    calls = _load(FIXTURES / "calls.json")["calls"]
    assert isinstance(calls, list)
    return calls


def _key(tool: str, args: dict[str, str]) -> tuple[str, str]:
    return tool, json.dumps(args, sort_keys=True)


def _find(tool: str, **args: str) -> Json | None:
    hits = [c for c in _calls() if _key(c["tool"], c["args"]) == _key(tool, args)]
    assert len(hits) <= 1
    return hits[0] if hits else None


def _ts(value: str) -> datetime:
    # Python 3.12 parses "Z" and fractions of any length up to 6 digits.
    return datetime.fromisoformat(value)


def _scope(tool: str, ident: str) -> dict[str, str]:
    """The arguments that name the thing a list tool is about (Guard wants project and repo ids)."""
    if tool == "list_repositories":
        return {"project_id": ident}
    for project in _load(FIXTURES / "list_projects.json")["projects"]:
        if not _is_error_project(project["id"]) and any(
            r["id"] == ident for r in _repositories(project["id"])
        ):
            return {"project_id": project["id"], "repo_id": ident}
    return {"project_id": "", "repo_id": ident}


def _pages(tool: str, ident: str) -> list[Json]:
    """Follow has_more from the first page, stopping only at a deliberately unrecorded page."""
    scope = _scope(tool, ident)
    args = dict(scope)
    pages: list[Json] = []
    while True:
        call = _find(tool, **args)
        if call is None:
            assert pages, f"no recorded first page for {tool} {ident}"
            assert (tool, ident, args["cursor"]) in DELIBERATELY_UNRECORDED
            return pages
        assert "response" in call, f"{tool} {ident} is a recorded error"
        page = _load(FIXTURES / call["response"])
        pages.append(page)
        if not page["has_more"]:
            return pages
        args = {**scope, "cursor": page["next_cursor"]}


def _projects() -> list[Json]:
    projects = _load(FIXTURES / "list_projects.json")["projects"]
    assert isinstance(projects, list)
    return projects


def _is_error_project(project_id: str) -> bool:
    call = _find("list_repositories", project_id=project_id)
    return call is not None and "error" in call


def _repositories(project_id: str) -> list[Json]:
    return [r for page in _pages("list_repositories", project_id) for r in page["repositories"]]


def test_manifest_entries_are_well_formed() -> None:
    for call in _calls():
        assert call["tool"] in READ_TOOLS
        args = call["args"]
        assert isinstance(args, dict)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in args.items())
        assert ("response" in call) != ("error" in call)
        if "response" in call:
            rel = Path(call["response"])
            assert not rel.is_absolute()
            assert ".." not in rel.parts
            assert (FIXTURES / rel).is_file()
            _load(FIXTURES / rel)
        else:
            assert isinstance(call["error"], str)
            assert call["error"]
    reports = [c for c in _calls() if c["tool"] == "get_audit_report"]
    assert len(reports) == 4
    for call in reports:
        assert sorted(call["args"]) == ["project_id", "repo_id", "task_id"]
        assert _load(FIXTURES / call["response"])["task_id"] == call["args"]["task_id"]


def test_manifest_has_no_duplicate_calls() -> None:
    keys = [_key(c["tool"], c["args"]) for c in _calls()]
    assert len(keys) == len(set(keys))


def test_every_listed_page_and_repository_has_a_recorded_call() -> None:
    assert _find("list_projects") is not None
    for project in _projects():
        if _is_error_project(project["id"]):
            continue  # a recorded error has no pages to follow
        for repo in _repositories(project["id"]):
            assert _find("list_audit_runs", **_scope("list_audit_runs", repo["id"])) is not None
            assert _pages("list_audit_runs", repo["id"])


def test_run_pages_are_newest_first_by_linked_at() -> None:
    for project in _projects():
        if _is_error_project(project["id"]):
            continue
        for repo in _repositories(project["id"]):
            previous_oldest: datetime | None = None
            for page in _pages("list_audit_runs", repo["id"]):
                linked = [_ts(r["linked_at"]) for r in page["runs"]]
                assert linked == sorted(linked, reverse=True)
                if previous_oldest is not None:
                    assert max(linked) < previous_oldest
                previous_oldest = min(linked)


def _counts(run: Json) -> bool:
    return bool(run["status"] == "completed" and run["report_status"] == "ready")


def test_scenario_newest_completed_runs() -> None:
    assert {p["id"] for p in _projects() if not _is_error_project(p["id"])} == set(EXPECTED_NEWEST)
    assert _is_error_project(ERROR_PROJECT)
    assert ERROR_PROJECT not in EXPECTED_NEWEST
    for project_id, expected in EXPECTED_NEWEST.items():
        repos = _repositories(project_id)
        completed = [
            run["completed_at"]
            for repo in repos
            for page in _pages("list_audit_runs", repo["id"])
            for run in page["runs"]
            if _counts(run)
        ]
        if expected is None:
            assert not repos
            assert not completed
        else:
            assert max(completed, key=_ts) == expected
    # b001 page 2 was linked earlier than page 1 yet completed later.
    page1, page2 = _pages("list_audit_runs", "repo_00000000-0000-4000-8000-00000000b001")
    assert max(_ts(r["completed_at"]) for r in page2["runs"]) > max(
        _ts(r["completed_at"]) for r in page1["runs"]
    )


_SYNTHETIC_ID = re.compile(r"(repo_)?00000000-0000-4000-8000-0000000[0-9a-f]{5}")


def _check_synthetic(node: Any, source: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("id", "project_id", "task_id", "repo_id"):
                assert _SYNTHETIC_ID.fullmatch(value), (source, key, value)
            if key == "name" and "/" in value:
                assert value.startswith("acme/"), (source, value)
            if key == "report_url":
                assert value.startswith("https://guard.example/"), (source, value)
            _check_synthetic(value, source)
    elif isinstance(node, list):
        for item in node:
            _check_synthetic(item, source)


def test_fixture_values_are_synthetic() -> None:
    files = sorted(FIXTURES.glob("*.json"))
    assert files
    for path in files:
        _check_synthetic(_load(path), path.name)
    calls = _load(FIXTURES / "calls.json")["calls"]
    for call in calls:
        _check_synthetic(call["args"], "calls.json")
