"""McpGuardTools over an in-process fake Guard and httpx2.MockTransport: no sockets."""

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fake_oauth import (
    FAKE_ACCESS_TOKEN,
    ROTATED_ACCESS_TOKEN,
    ROTATED_REFRESH_TOKEN,
    FakeOAuthTransport,
    make_credentials,
    token_body,
)
from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent

from enji_guard_radar.cli import PrintNotifier, read_config
from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard_auth import GUARD_MCP_URL, auth_failure_text
from enji_guard_radar.guard_credentials import CredentialStore
from enji_guard_radar.mcp_guard import McpGuardTools, classify_status, extract_result, open_guard
from enji_guard_radar.pipeline import Deps, RunReport, run_once
from enji_guard_radar.ports import (
    AuthFailureKind,
    GuardAuthError,
    GuardCallError,
    GuardFailure,
    GuardTools,
)
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
        self, project_id: str | None = None, repo_id: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        return self._answer(
            "list_audit_runs", project_id=project_id, repo_id=repo_id, cursor=cursor
        )

    def _get_audit_report(
        self, project_id: str | None = None, repo_id: str | None = None, task_id: str | None = None
    ) -> dict[str, Any]:
        return self._answer(
            "get_audit_report", project_id=project_id, repo_id=repo_id, task_id=task_id
        )

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


# --- HTTP layer: MockTransport, no handshake needed because every request fails ---------------

URL = "https://guard.example/mcp"
TOKEN = "fake-bearer-token-0123456789abcdefghij"
SERVER_SECRET = "https://guard.example/secret"


class RecordingSource:
    """AccessTokenSource that records every force_refresh flag it is asked for."""

    def __init__(self, error: GuardCallError | None = None) -> None:
        self.forced: list[bool] = []
        self._error = error

    def access_token(self, *, force_refresh: bool = False) -> str:
        self.forced.append(force_refresh)
        if self._error is not None:
            raise self._error
        return TOKEN


class Wire:
    """A MockTransport handler that answers from a script and records the requests."""

    def __init__(self, *answers: int | Exception) -> None:
        self._answers = list(answers)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self._answers[0] if len(self._answers) == 1 else self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx2.Response(answer, text=SERVER_SECRET)


def open_over(wire: Wire, source: RecordingSource | None = None) -> McpGuardTools:
    adapter = McpGuardTools.for_url(
        URL, source or RecordingSource(), http_transport=httpx2.MockTransport(wire)
    )
    with adapter:  # pragma: no cover - every caller expects the entry to fail
        pass
    return adapter


def failure_of(wire: Wire, source: RecordingSource | None = None) -> GuardCallError:
    with pytest.raises(GuardCallError) as raised:
        open_over(wire, source)
    return raised.value


@pytest.mark.parametrize(
    ("statuses", "kind", "cause", "text"),
    [
        ([], None, None, None),
        ([200], None, None, None),
        ([401], AuthFailureKind.AUTH_REJECTED, GuardFailure.AUTH, None),
        ([401, 401], AuthFailureKind.AUTH_REJECTED, GuardFailure.AUTH, None),
        ([403], AuthFailureKind.AUTH_FORBIDDEN, GuardFailure.AUTH, None),
        ([429], None, GuardFailure.UNAVAILABLE, "Guard unavailable (HTTP 429)"),
        ([500], None, GuardFailure.UNAVAILABLE, "Guard unavailable (HTTP 500)"),
        ([503], None, GuardFailure.UNAVAILABLE, "Guard unavailable (HTTP 503)"),
    ],
)
def test_classify_status_table(
    statuses: list[int], kind: AuthFailureKind | None, cause: GuardFailure | None, text: str | None
) -> None:
    error = classify_status(statuses)
    if cause is None:
        assert error is None
        return
    assert error is not None
    assert error.cause is cause
    if kind is not None:
        assert isinstance(error, GuardAuthError)
        assert error.kind is kind
        assert str(error) == auth_failure_text(kind)
    if text is not None:
        assert str(error) == text


def test_401_forces_exactly_one_refresh_then_reports_auth_rejected() -> None:
    wire, source = Wire(401), RecordingSource()
    error = failure_of(wire, source)
    assert isinstance(error, GuardAuthError)
    assert error.kind is AuthFailureKind.AUTH_REJECTED
    assert source.forced.count(True) == 1
    assert source.forced.index(True) > 0  # the first attempt asked without forcing
    assert {r.headers["Authorization"] for r in wire.requests} == {f"Bearer {TOKEN}"}


def test_401_then_503_is_unavailable_after_one_retry() -> None:
    wire, source = Wire(401, 401, 503), RecordingSource()
    error = failure_of(wire, source)
    assert not isinstance(error, GuardAuthError)
    assert error.cause is GuardFailure.UNAVAILABLE
    assert str(error) == "Guard unavailable (HTTP 503)"
    assert source.forced.count(True) == 1


def test_403_is_auth_forbidden_without_a_refresh() -> None:
    source = RecordingSource()
    error = failure_of(Wire(403), source)
    assert isinstance(error, GuardAuthError)
    assert error.kind is AuthFailureKind.AUTH_FORBIDDEN
    assert True not in source.forced


@pytest.mark.parametrize("status", [429, 500, 502])
def test_429_and_5xx_are_unavailable_with_fixed_text(status: int) -> None:
    error = failure_of(Wire(status))
    assert error.cause is GuardFailure.UNAVAILABLE
    assert str(error) == f"Guard unavailable (HTTP {status})"
    assert "guard.example" not in str(error)


@pytest.mark.parametrize(
    ("raised", "text"),
    [
        (httpx2.ConnectTimeout("timed out at https://guard.example/mcp"), "(timeout)"),
        (httpx2.ReadTimeout("slow https://guard.example/mcp"), "(timeout)"),
        (httpx2.ConnectError("refused https://guard.example/mcp"), "(network)"),
    ],
)
def test_timeout_and_network_errors_are_unavailable_without_a_url(
    raised: Exception, text: str
) -> None:
    error = failure_of(Wire(raised))
    assert error.cause is GuardFailure.UNAVAILABLE
    assert str(error) == f"Guard unreachable {text}"
    assert "http" not in str(error)


def test_token_source_failure_reaches_the_caller_unwrapped() -> None:
    expired = GuardAuthError(
        AuthFailureKind.AUTH_EXPIRED, auth_failure_text(AuthFailureKind.AUTH_EXPIRED)
    )
    wire = Wire(200)
    error = failure_of(wire, RecordingSource(error=expired))
    assert isinstance(error, GuardAuthError)
    assert error.kind is AuthFailureKind.AUTH_EXPIRED
    assert wire.requests == []


def test_failures_log_class_and_status_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    failure_of(Wire(httpx2.ConnectError("refused https://guard.example/mcp")))
    failure_of(Wire(503))
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("enji_guard_radar")]
    assert any("ConnectError" in line for line in ours)
    assert not any(secret in line for line in ours for secret in ("guard.example", TOKEN))


def test_disallowed_tool_never_reaches_the_transport() -> None:
    wire = Wire(200)
    adapter = McpGuardTools.for_url(
        URL, RecordingSource(), http_transport=httpx2.MockTransport(wire)
    )
    with pytest.raises(GuardCallError, match="tool not allowed: run_audit") as raised:
        adapter.call("run_audit", {"repository_id": "x"})
    assert raised.value.cause is GuardFailure.DATA
    assert wire.requests == []


def test_call_before_entering_the_session_is_refused() -> None:
    adapter = McpGuardTools.for_url(URL, RecordingSource())
    with pytest.raises(GuardCallError, match="not open"):
        adapter.call("list_projects", {})


# --- result shapes ------------------------------------------------------------------------------

OBJECT = {"projects": [{"id": "a"}]}
TEXT = TextContent(type="text", text=json.dumps(OBJECT))


@pytest.mark.parametrize(
    ("structured", "contents", "shape"),
    [
        (OBJECT, [TEXT], "structured"),
        (None, [TEXT], "text_json"),
        ({"result": json.dumps(OBJECT)}, [], "wrapped"),
        ({"result": OBJECT}, [], "wrapped"),
        (None, [TextContent(type="text", text=json.dumps({"result": OBJECT}))], "wrapped"),
    ],
)
def test_extract_result_shape_accepts_known_shapes(
    structured: dict[str, object] | None, contents: list[Any], shape: str
) -> None:
    assert extract_result(structured, contents) == (OBJECT, shape)


@pytest.mark.parametrize(
    "contents",
    [
        [TEXT, TEXT],
        [TextContent(type="text", text="[1, 2]")],
        [TextContent(type="text", text="plain words")],
        [ImageContent(type="image", data="AAAA", mime_type="image/png")],
        [],
    ],
)
def test_extract_result_shape_rejects_anything_else_as_data(contents: list[Any]) -> None:
    with pytest.raises(GuardCallError, match="unrecognised tool result shape") as raised:
        extract_result(None, contents)
    assert raised.value.cause is GuardFailure.DATA


@pytest.mark.parametrize("structured", [{"result": "not json"}, {"result": [1, 2]}])
def test_extract_result_shape_rejects_a_wrapper_without_an_object(
    structured: dict[str, object],
) -> None:
    with pytest.raises(GuardCallError, match="unrecognised tool result shape"):
        extract_result(structured, [])


def test_is_error_result_uses_fixed_text_and_hides_the_server_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = MCPServer("failing-guard")

    @server.tool(name="list_projects")
    def list_projects() -> dict[str, Any]:
        raise ToolError(SERVER_SECRET)

    caplog.set_level(logging.DEBUG)
    with McpGuardTools(lambda: Client(server)) as adapter, pytest.raises(GuardCallError) as raised:
        adapter.call("list_projects", {})
    assert str(raised.value) == "Guard tool list_projects returned an error"
    assert raised.value.cause is GuardFailure.DATA
    assert SERVER_SECRET not in str(raised.value)
    assert "guard.example/secret" not in "".join(
        record.getMessage() for record in caplog.records if record.name.startswith("enji_guard")
    )


def test_result_shapes_counts_each_shape_seen() -> None:
    fake = FakeGuard()
    with McpGuardTools(lambda: Client(fake.server)) as adapter:
        adapter.call("list_projects", {})
        adapter.call("list_projects", {})
        assert adapter.result_shapes() == {"structured": 2}


def test_entering_and_leaving_twice_in_one_process_does_not_hang() -> None:
    fake = FakeGuard()
    for _ in range(2):
        with McpGuardTools(lambda: Client(fake.server)) as adapter:
            assert "list_projects" in adapter.tool_names()


# --- run budget ---------------------------------------------------------------------------------


def test_budget_exceeded_stops_before_any_sdk_call() -> None:
    fake = FakeGuard()
    clock = {"now": 100.0}
    with McpGuardTools(
        lambda: Client(fake.server), run_budget_s=600.0, monotonic=lambda: clock["now"]
    ) as adapter:
        assert adapter.call("list_projects", {})
        clock["now"] = 100.0 + 601.0
        with pytest.raises(GuardCallError) as raised:
            adapter.call("list_projects", {})
    assert str(raised.value) == "Guard run budget of 600 s exceeded"
    assert raised.value.cause is GuardFailure.UNAVAILABLE
    assert adapter.result_shapes() == {"structured": 1}


# --- open_guard ---------------------------------------------------------------------------------

NOW_UTC = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def seed_login(state: Path, *, expires_in: timedelta) -> None:
    CredentialStore(state).save(
        make_credentials(expires_at=NOW_UTC + expires_in, obtained_at=NOW_UTC - timedelta(hours=1))
    )


def test_open_guard_sends_the_stored_token_to_the_guard_url(tmp_path: Path) -> None:
    seed_login(tmp_path, expires_in=timedelta(hours=1))
    wire, oauth = Wire(503), FakeOAuthTransport([])
    session = open_guard(
        tmp_path,
        clock=lambda: NOW_UTC,
        oauth_transport=oauth,
        http_transport=httpx2.MockTransport(wire),
    )
    with pytest.raises(GuardCallError), session:
        pass
    assert {str(r.url) for r in wire.requests} == {GUARD_MCP_URL}
    assert {r.headers["Authorization"] for r in wire.requests} == {f"Bearer {FAKE_ACCESS_TOKEN}"}
    assert oauth.requests == []


def test_open_guard_refreshes_an_expired_login_once_and_saves_the_rotation(tmp_path: Path) -> None:
    seed_login(tmp_path, expires_in=-timedelta(minutes=1))
    wire = Wire(503)
    oauth = FakeOAuthTransport([token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)])
    session = open_guard(
        tmp_path,
        clock=lambda: NOW_UTC,
        oauth_transport=oauth,
        http_transport=httpx2.MockTransport(wire),
    )
    with pytest.raises(GuardCallError), session:
        pass
    assert len(oauth.requests) == 1
    assert {r.headers["Authorization"] for r in wire.requests} == {f"Bearer {ROTATED_ACCESS_TOKEN}"}
    saved = CredentialStore(tmp_path).load()
    assert saved.refresh_token.get_secret_value() == ROTATED_REFRESH_TOKEN


def test_open_guard_without_a_login_reports_not_authenticated(tmp_path: Path) -> None:
    wire = Wire(200)
    session = open_guard(
        tmp_path,
        clock=lambda: NOW_UTC,
        oauth_transport=FakeOAuthTransport([]),
        http_transport=httpx2.MockTransport(wire),
    )
    with pytest.raises(GuardAuthError) as raised, session:
        pass
    assert raised.value.kind is AuthFailureKind.NOT_AUTHENTICATED
    assert wire.requests == []


# --- the real connect(), session open failures and the error fallbacks ---------------------------


class HandshakeWire:
    """A MockTransport handler that speaks just enough streamable-HTTP MCP to open a session."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.methods: list[str] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.method != "POST":
            return httpx2.Response(405)
        message = json.loads(request.content)
        method = message.get("method", "")
        self.methods.append(method)
        if "id" not in message:  # a notification: acknowledged, no body
            return httpx2.Response(202)
        if method == "server/discover":  # a legacy server: the SDK falls back to initialize
            error = {"code": -32601, "message": "Method not found"}
            return httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": message["id"], "error": error}
            )
        result: dict[str, Any] = {}
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-guard", "version": "0"},
            }
        elif method == "tools/list":
            properties: dict[str, Any] = {"project_id": {}, "cursor": {}}
            result = {
                "tools": [
                    {
                        "name": "list_repositories",
                        "inputSchema": {"type": "object", "properties": properties},
                    },
                    {"name": "list_projects", "inputSchema": {"type": "object"}},
                ]
            }
        body = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        return httpx2.Response(200, json=body)


def test_for_url_opens_a_real_session_with_the_bearer_token() -> None:
    wire, source = HandshakeWire(), RecordingSource()
    adapter = McpGuardTools.for_url(URL, source, http_transport=httpx2.MockTransport(wire))
    with adapter:
        assert adapter.tool_names() == {"list_projects", "list_repositories"}
        assert adapter.tool_arguments()["list_repositories"] == ("cursor", "project_id")
        assert adapter.tool_arguments()["list_projects"] == ()
    assert wire.methods[:3] == ["server/discover", "initialize", "notifications/initialized"]
    assert "tools/list" in wire.methods
    assert {str(r.url) for r in wire.requests} == {URL}
    assert {r.headers["Authorization"] for r in wire.requests} == {f"Bearer {TOKEN}"}


class FailingListTools:
    """A fake SDK client whose tools/list fails with an exception that carries secrets."""

    def __init__(self, failure: Exception, events: list[str]) -> None:
        self._failure = failure
        self._events = events

    async def __aenter__(self) -> "FailingListTools":
        self._events.append("entered")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._events.append("exited")

    async def list_tools(self) -> None:
        raise self._failure


def failing_list_tools(failure: Exception, events: list[str]) -> McpGuardTools:
    # The adapter only needs an async context manager yielding an object with list_tools.
    return McpGuardTools(lambda: FailingListTools(failure, events))  # type: ignore[arg-type,return-value]


LEAKS = (SERVER_SECRET, "guard.example", TOKEN, FAKE_ACCESS_TOKEN)


def assert_nothing_leaks(error: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    """No token, URL or server text in the exception text or in the radar's own log lines."""
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("enji_guard_radar")]
    assert not any(leak in str(error) for leak in LEAKS)
    assert not any(leak in line for line in ours for leak in LEAKS)


def test_list_tools_failure_closes_the_session_and_reports_a_fixed_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    events: list[str] = []
    adapter = failing_list_tools(ValueError(f"boom {SERVER_SECRET} {TOKEN}"), events)
    with pytest.raises(GuardCallError) as raised, adapter:
        pytest.fail("entering the session must fail")  # pragma: no cover
    assert str(raised.value) == "Guard call failed (ValueError)"
    assert raised.value.cause is GuardFailure.UNAVAILABLE
    assert raised.value.__cause__ is None
    assert events == ["entered", "exited"]
    assert adapter.tool_names() == frozenset()
    with pytest.raises(GuardCallError, match="not open"):
        adapter.call("list_projects", {})
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("enji_guard_radar")]
    assert any("ValueError" in line for line in ours)
    assert_nothing_leaks(raised.value, caplog)


def test_list_tools_failure_leaves_the_adapter_enterable_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    events: list[str] = []
    adapter = failing_list_tools(RuntimeError(f"down {SERVER_SECRET} {TOKEN}"), events)
    for _ in range(2):
        with pytest.raises(GuardCallError) as raised, adapter:
            pytest.fail("entering the session must fail")  # pragma: no cover
        assert str(raised.value) == "Guard call failed (RuntimeError)"
        assert raised.value.cause is GuardFailure.UNAVAILABLE
        assert raised.value.__cause__ is None
        assert_nothing_leaks(raised.value, caplog)
    assert events == ["entered", "exited", "entered", "exited"]


def test_a_guard_call_error_from_the_session_is_re_raised_as_is_without_a_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # `_guarded` must not classify, wrap or retry a GuardCallError the operation raised itself.
    caplog.set_level(logging.DEBUG)
    fixed = GuardCallError("Guard said no", cause=GuardFailure.DATA)
    events: list[str] = []
    adapter = failing_list_tools(fixed, events)
    with pytest.raises(GuardCallError) as raised, adapter:
        pytest.fail("entering the session must fail")  # pragma: no cover
    assert raised.value is fixed
    assert str(raised.value) == "Guard said no"
    assert raised.value.cause is GuardFailure.DATA
    assert events == ["entered", "exited"]  # opened once: no retry
    assert not [
        r for r in caplog.records if "guard call failed" in r.getMessage()
    ]  # not classified
    assert_nothing_leaks(raised.value, caplog)


def test_a_401_with_a_guard_call_error_does_not_force_a_refresh(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    broken = GuardCallError("login broken", cause=GuardFailure.AUTH)
    source = RecordingSource(error=broken)
    wire = Wire(401)
    error = failure_of(wire, source)
    assert error is broken
    assert str(error) == "login broken"
    assert error.cause is GuardFailure.AUTH
    assert source.forced == [False]  # asked once, never forced, never retried
    assert wire.requests == []
    assert_nothing_leaks(error, caplog)


def test_open_session_without_a_portal_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    adapter = McpGuardTools.for_url(URL, RecordingSource())
    with pytest.raises(GuardCallError, match="Guard session is not open") as raised:
        adapter._open_session()
    assert adapter.tool_names() == frozenset()
    with pytest.raises(GuardCallError, match="not open"):
        adapter.call("list_projects", {})
    assert_nothing_leaks(raised.value, caplog)


def test_an_unclassified_failure_during_a_call_is_a_fixed_unavailable_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeGuard()
    with McpGuardTools(lambda: Client(fake.server)) as adapter:
        assert adapter.tool_names()
        failure = KeyError(f"{SERVER_SECRET} {TOKEN}")
        adapter._client = FailingCallClient(failure)  # type: ignore[assignment]
        with pytest.raises(GuardCallError) as raised:
            adapter.call("list_projects", {})
    assert str(raised.value) == "Guard call failed (KeyError)"
    assert raised.value.cause is GuardFailure.UNAVAILABLE
    assert raised.value.__cause__ is None
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("enji_guard_radar")]
    assert any("KeyError" in line for line in ours)
    assert_nothing_leaks(raised.value, caplog)
    # Leaving the block closed the session: the adapter is back to refusing calls.
    with pytest.raises(GuardCallError, match="not open"):
        adapter.call("list_projects", {})


class FailingCallClient:
    def __init__(self, failure: Exception) -> None:
        self._failure = failure

    async def call_tool(self, name: str, arguments: dict[str, str]) -> None:
        raise self._failure
