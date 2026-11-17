"""Consistency of the synthetic Guard scenario in fixtures/guard (standard library only)."""

import json
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
}

Json = dict[str, Any]

_ID_ARG = {"list_repositories": "project_id", "list_audit_runs": "repository_id"}


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


def _pages(tool: str, ident: str) -> list[Json]:
    """Follow has_more from the first page, stopping only at a deliberately unrecorded page."""
    id_arg = _ID_ARG[tool]
    args = {id_arg: ident}
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
        args = {id_arg: ident, "cursor": page["next_cursor"]}


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


def test_manifest_has_no_duplicate_calls() -> None:
    keys = [_key(c["tool"], c["args"]) for c in _calls()]
    assert len(keys) == len(set(keys))


def test_every_listed_page_and_repository_has_a_recorded_call() -> None:
    assert _find("list_projects") is not None
    for project in _projects():
        if _is_error_project(project["id"]):
            continue  # a recorded error has no pages to follow
        for repo in _repositories(project["id"]):
            assert _find("list_audit_runs", repository_id=repo["id"]) is not None
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
