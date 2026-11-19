"""Test doubles for the Guard tool boundary (no network, no files)."""

from collections.abc import Mapping, Sequence

from enji_guard_radar.guard import (
    ARG_PROJECT_ID,
    ARG_REPOSITORY_ID,
    TOOL_LIST_AUDIT_RUNS,
    TOOL_LIST_PROJECTS,
    TOOL_LIST_REPOSITORIES,
)
from enji_guard_radar.guard_report import ARG_TASK_ID, TOOL_GET_AUDIT_REPORT
from enji_guard_radar.ports import GuardCallError

Key = tuple[str, tuple[tuple[str, str], ...]]


def call_key(tool: str, **args: str) -> Key:
    """The lookup key of one call: the tool plus its arguments sorted by name."""
    return tool, tuple(sorted(args.items()))


class InMemoryGuardTools:
    """Answers from a dict and records every request, answered or not."""

    def __init__(self, responses: Mapping[Key, Mapping[str, object] | Exception]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append((tool, dict(args)))
        key = call_key(tool, **args)
        if key not in self._responses:
            message = f"no recorded response for {tool}"
            raise GuardCallError(message)
        response = self._responses[key]
        if isinstance(response, Exception):
            raise response
        return response


Response = Mapping[str, object] | Exception


def projects_page(*projects: tuple[str, str], has_more: bool = False) -> dict[str, object]:
    """Projects as (id, name) pairs."""
    return {
        "projects": [{"id": pid, "name": name} for pid, name in projects],
        "has_more": has_more,
    }


def repos_page(
    project_id: str, *repos: tuple[str, str], has_more: bool = False
) -> dict[str, object]:
    """Repositories of one project as (id, name) pairs."""
    return {
        "repositories": [
            {
                "id": rid,
                "project_id": project_id,
                "name": name,
                "provider": "github",
                "connected": True,
            }
            for rid, name in repos
        ],
        "has_more": has_more,
    }


def run_item(
    task_id: str,
    action_key: str,
    completed_at: str,
    *,
    status: str = "completed",
    report_status: str = "ready",
) -> dict[str, object]:
    """One run of list_audit_runs; completed_at is an ISO string with a 6-digit fraction."""
    return {
        "task_id": task_id,
        "action_key": action_key,
        "linked_at": completed_at,
        "task_available": True,
        "status": status,
        "created_at": completed_at,
        "completed_at": completed_at,
        "report_status": report_status,
    }


def runs_page(*runs: Mapping[str, object], has_more: bool = False) -> dict[str, object]:
    return {"runs": list(runs), "has_more": has_more}


def report_markdown(*, high: int, medium: int = 0, score: int = 50) -> str:
    """A scored report body with the given number of High and Medium findings."""
    lines = ["# Placeholder report title", "", f"**Score: {score} / 100** · Placeholder", ""]
    lines += ["## Security findings", ""]
    # Guard deducts points only for findings, and the parser relies on it: a score below 100
    # always comes with at least one finding (a Low one here, which no rule counts).
    lows = 1 if score < 100 and high + medium == 0 else 0
    for number in range(high + medium + lows):
        severity = "High" if number < high else "Medium" if number < high + medium else "Low"
        lines += [f"### `F-{number + 1:03d}` — Placeholder finding", ""]
        lines += [f"**Severity:** {severity} · **Confidence:** High", ""]
    return "\n".join(lines)


def report_page(task_id: str, *, high: int, medium: int = 0, score: int = 50) -> dict[str, object]:
    return {
        "task_id": task_id,
        "report_status": "ready",
        "content": report_markdown(high=high, medium=medium, score=score),
        "content_version": "v1",
        "has_more": False,
    }


def world(
    projects: Sequence[tuple[str, str]],
    repos_by_project: Mapping[str, Sequence[tuple[str, str]]],
    runs_by_repo: Mapping[str, Sequence[Mapping[str, object]]],
    reports_by_task: Mapping[str, Mapping[str, object]],
    failures: Mapping[Key, Response] | None = None,
) -> dict[Key, Response]:
    """The response map of one poll for InMemoryGuardTools.

    A task without a report entry has no recorded response, so reading it fails (an unknown
    report). `failures` replaces or adds responses, for example a failing list call or a page that
    promises a second page.
    """
    responses: dict[Key, Response] = {call_key(TOOL_LIST_PROJECTS): projects_page(*projects)}
    for pid, repos in repos_by_project.items():
        responses[call_key(TOOL_LIST_REPOSITORIES, **{ARG_PROJECT_ID: pid})] = repos_page(
            pid, *repos
        )
    project_of = {repo[0]: pid for pid, repos in repos_by_project.items() for repo in repos}
    repo_of: dict[str, str] = {}
    for rid, runs in runs_by_repo.items():
        scope = {ARG_PROJECT_ID: project_of[rid], ARG_REPOSITORY_ID: rid}
        responses[call_key(TOOL_LIST_AUDIT_RUNS, **scope)] = runs_page(*runs)
        repo_of.update({str(run["task_id"]): rid for run in runs})
    for task_id, page in reports_by_task.items():
        if task_id not in repo_of:
            continue  # its run is not listed, so nothing can ask for this report
        rid = repo_of[task_id]
        scope = {ARG_PROJECT_ID: project_of[rid], ARG_REPOSITORY_ID: rid, ARG_TASK_ID: task_id}
        responses[call_key(TOOL_GET_AUDIT_REPORT, **scope)] = page
    responses.update(failures or {})
    return responses
