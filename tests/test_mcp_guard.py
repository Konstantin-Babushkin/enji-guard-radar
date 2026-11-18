"""McpGuardTools over an in-process fake Guard and httpx2.MockTransport: no sockets."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer

from enji_guard_radar.cli import PrintNotifier, read_config
from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.mcp_guard import McpGuardTools
from enji_guard_radar.pipeline import Deps, RunReport, run_once
from enji_guard_radar.ports import GuardCallError, GuardFailure, GuardTools
from enji_guard_radar.state_store import DryRunStateStore, FileStateStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


class FakeGuard:
    """An in-process MCP server that answers from the recorded fixtures and spies on run_audit."""

    def __init__(self) -> None:
        self.run_audit_calls: list[str] = []
        self._source = FixtureGuardTools(FIXTURES)
        self.server = MCPServer("fake-guard")
        self.server.tool(name="list_projects")(self._list_projects)
        self.server.tool(name="list_repositories")(self._list_repositories)
        self.server.tool(name="list_audit_runs")(self._list_audit_runs)
        self.server.tool(name="get_audit_report")(self._get_audit_report)
        self.server.tool(name="run_audit")(self._run_audit)

    def _answer(self, tool: str, **args: str | None) -> dict[str, Any]:
        wanted = {key: value for key, value in args.items() if value is not None}
        return dict(self._source.call(tool, wanted))

    def _list_projects(self) -> dict[str, Any]:
        return self._answer("list_projects")

    def _list_repositories(
        self, project_id: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        return self._answer("list_repositories", project_id=project_id, cursor=cursor)

    def _list_audit_runs(
        self, repository_id: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        return self._answer("list_audit_runs", repository_id=repository_id, cursor=cursor)

    def _get_audit_report(
        self, repository_id: str | None = None, task_id: str | None = None
    ) -> dict[str, Any]:
        return self._answer("get_audit_report", repository_id=repository_id, task_id=task_id)

    def _run_audit(self, repository_id: str) -> dict[str, Any]:
        self.run_audit_calls.append(repository_id)
        return {}


def report_over(guard: GuardTools, state: Path) -> RunReport:
    deps = Deps(
        guard=guard,
        notifier=PrintNotifier(),
        config=read_config(FIXTURES / "radar.yaml"),
        clock=lambda: NOW,
        store=DryRunStateStore(FileStateStore(state)),
    )
    return run_once(deps)


def outcome(report: RunReport) -> tuple[object, ...]:
    return (
        report.outcome,
        [
            (
                r.project_id,
                r.status,
                r.stale.model_copy(update={"reason": None}) if r.stale else None,
            )
            for r in report.results
        ],
        report.baselined,
        report.messages,
    )


def test_pipeline_over_the_sdk_matches_the_fixture_source(tmp_path: Path) -> None:
    fake = FakeGuard()
    expected = report_over(FixtureGuardTools(FIXTURES), tmp_path / "fixture-state")
    with McpGuardTools(lambda: Client(fake.server)) as adapter:
        assert {
            "list_projects",
            "list_repositories",
            "list_audit_runs",
            "get_audit_report",
        } <= adapter.tool_names()
        assert {"cursor", "project_id"} <= set(adapter.tool_arguments()["list_repositories"])
        listed = json.loads((FIXTURES / "list_projects.json").read_text(encoding="utf-8"))
        assert adapter.call("list_projects", {}) == listed
        actual = report_over(adapter, tmp_path / "sdk-state")
        with pytest.raises(GuardCallError) as refused:
            adapter.call("run_audit", {"repository_id": "x"})
    assert refused.value.cause is GuardFailure.DATA
    assert fake.run_audit_calls == []
    assert expected.results
    # Only the wording of an unknown project's reason may differ: it carries the adapter's text.
    assert outcome(actual) == outcome(expected)
