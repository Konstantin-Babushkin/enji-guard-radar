"""Guard MCP adapter: the sync `GuardTools` port over the official `mcp` SDK.

Shell module: it owns an event-loop thread (an anyio blocking portal) and one SDK client for the
whole run. Only the four read tools can reach the transport. Messages are fixed text written
here; nothing the server or an HTTP library says is copied into a message or a log line.
"""

import json
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Generator, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, Self

import httpx2
from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ContentBlock, TextContent

from enji_guard_radar.guard import GUARD_READ_TOOLS
from enji_guard_radar.guard_auth import GUARD_MCP_URL, auth_failure_text
from enji_guard_radar.guard_credentials import (
    CredentialStore,
    FormTransport,
    RefreshingTokenSource,
    UrllibOAuthTransport,
)
from enji_guard_radar.ports import (
    AccessTokenSource,
    AuthFailureKind,
    Clock,
    GuardAuthError,
    GuardCallError,
    GuardFailure,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_READ_TIMEOUT_S = 60.0
DEFAULT_RUN_BUDGET_S = 600.0
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_ERROR_FROM = 400

SHAPE_STRUCTURED = "structured"
SHAPE_TEXT_JSON = "text_json"
SHAPE_WRAPPED = "wrapped"
_WRAPPER_KEY = "result"


def _json_object(text: str) -> Mapping[str, object] | None:
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _unwrap(payload: Mapping[str, object]) -> Mapping[str, object] | None:
    """The object inside a lone {"result": ...} wrapper (a mapping or JSON object text)."""
    inner = payload[_WRAPPER_KEY]
    if isinstance(inner, dict):
        return inner
    if isinstance(inner, str):
        return _json_object(inner)
    return None


def extract_result(
    structured: Mapping[str, object] | None, contents: Sequence[ContentBlock]
) -> tuple[Mapping[str, object], str]:
    """The result object of a tool call and the shape it arrived in.

    ASSUMPTION: Guard answers with structured content or one JSON text block (probe checks).
    """
    payload: Mapping[str, object] | None = None
    shape = SHAPE_STRUCTURED
    if isinstance(structured, dict):
        payload = structured
    elif len(contents) == 1 and isinstance(contents[0], TextContent):
        payload = _json_object(contents[0].text)
        shape = SHAPE_TEXT_JSON
    if payload is not None and list(payload) == [_WRAPPER_KEY]:
        payload = _unwrap(payload)
        shape = SHAPE_WRAPPED
    if payload is None:
        message = "unrecognised tool result shape"
        raise GuardCallError(message, cause=GuardFailure.DATA)
    return payload, shape


def classify_status(statuses: Sequence[int]) -> GuardCallError | None:
    """The failure the HTTP statuses of one attempt stand for; None when none was an error."""
    if HTTP_UNAUTHORIZED in statuses:
        kind = AuthFailureKind.AUTH_REJECTED
        return GuardAuthError(kind, auth_failure_text(kind))
    if HTTP_FORBIDDEN in statuses:
        kind = AuthFailureKind.AUTH_FORBIDDEN
        return GuardAuthError(kind, auth_failure_text(kind))
    failed = [status for status in statuses if status >= HTTP_ERROR_FROM]
    if failed:
        message = f"Guard unavailable (HTTP {failed[-1]})"
        return GuardCallError(message, cause=GuardFailure.UNAVAILABLE)
    return None


class RefreshingBearer(httpx2.Auth):
    """Puts a bearer token from the token source on every request, never stored on the client."""

    def __init__(self, source: AccessTokenSource) -> None:
        self._source = source
        self._force_next = False

    def force_next_refresh(self) -> None:
        self._force_next = True

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response]:
        force, self._force_next = self._force_next, False
        token = self._source.access_token(force_refresh=force)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


def _chain(failure: BaseException) -> Iterator[BaseException]:
    """Every exception inside a (nested) exception group, each followed by its causes."""
    if isinstance(failure, BaseExceptionGroup):
        for inner in failure.exceptions:
            yield from _chain(inner)
        return
    seen: set[int] = set()
    link: BaseException | None = failure
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        yield link
        link = link.__cause__ or link.__context__


class McpGuardTools:
    """Implements `ports.GuardTools` and `ports.GuardSession`; enter it before calling."""

    def __init__(
        self,
        connect: Callable[[], AbstractAsyncContextManager[Client]],
        *,
        statuses: list[int] | None = None,
        bearer: RefreshingBearer | None = None,
        run_budget_s: float = DEFAULT_RUN_BUDGET_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connect = connect
        self._statuses = statuses if statuses is not None else []
        self._bearer = bearer
        self._run_budget_s = run_budget_s
        self._monotonic = monotonic
        self._started = 0.0
        self._portal_manager: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._session: Any = None
        self._client: Client | None = None
        self._arguments: dict[str, tuple[str, ...]] = {}
        self._shapes: Counter[str] = Counter()

    @classmethod
    def for_url(
        cls,
        url: str,
        token_source: AccessTokenSource,
        *,
        http_transport: httpx2.AsyncBaseTransport | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
        run_budget_s: float = DEFAULT_RUN_BUDGET_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Self:
        statuses: list[int] = []
        bearer = RefreshingBearer(token_source)

        async def record(response: httpx2.Response) -> None:
            statuses.append(response.status_code)

        @asynccontextmanager
        async def connect() -> AsyncIterator[Client]:
            async with (
                httpx2.AsyncClient(
                    auth=bearer,
                    event_hooks={"response": [record]},
                    timeout=httpx2.Timeout(timeout_s, read=read_timeout_s),
                    transport=http_transport,
                ) as http,
                Client(streamable_http_client(url, http_client=http)) as client,
            ):
                yield client

        return cls(
            connect,
            statuses=statuses,
            bearer=bearer,
            run_budget_s=run_budget_s,
            monotonic=monotonic,
        )

    def __enter__(self) -> Self:
        self._started = self._monotonic()
        portal_manager = start_blocking_portal()
        self._portal = portal_manager.__enter__()
        self._portal_manager = portal_manager
        try:
            self._guarded(self._open_session)
        except BaseException:
            self._close_portal()
            raise
        logger.info("guard session open: %d tools listed", len(self._arguments))
        return self

    def __exit__(self, *exc: object) -> None:
        # Reverse order of entry: the SDK context first, the portal second, or the process hangs.
        try:
            if self._session is not None:
                self._session.__exit__(None, None, None)
        finally:
            self._session = self._client = None
            self._close_portal()

    def _close_portal(self) -> None:
        manager, self._portal_manager, self._portal = self._portal_manager, None, None
        if manager is not None:
            manager.__exit__(None, None, None)

    def _open_session(self) -> None:
        portal = self._portal
        if portal is None:  # unreachable: __enter__ sets the portal first
            message = "Guard session is not open"
            raise GuardCallError(message)
        session = portal.wrap_async_context_manager(self._connect())
        client = session.__enter__()
        try:
            listed = portal.call(client.list_tools)
        except BaseException:
            session.__exit__(None, None, None)
            raise
        self._session, self._client = session, client
        self._arguments = {
            tool.name: tuple(sorted(tool.input_schema.get("properties", {})))
            for tool in listed.tools
        }

    def _guarded[T](self, operation: Callable[[], T]) -> T:
        """Run one SDK operation; map any failure to a fixed-text GuardCallError.

        An HTTP 401 forces one token refresh and one retry; a second 401 is final.
        """
        retried = False
        while True:
            self._statuses.clear()
            try:
                return operation()
            except GuardCallError:
                raise
            except Exception as failure:
                error = self._classify(failure)
            rejected = (
                isinstance(error, GuardAuthError) and error.kind is AuthFailureKind.AUTH_REJECTED
            )
            if rejected and not retried and self._bearer is not None and self._statuses:
                retried = True
                logger.info("guard answered 401: refreshing the token once and retrying")
                self._bearer.force_next_refresh()
                continue
            raise error

    def _classify(self, failure: Exception) -> GuardCallError:
        links = list(_chain(failure))
        # Not `raise ... from failure`: SDK and httpx2 exception text can contain URLs.
        logger.warning(
            "guard call failed: %s, http statuses %s",
            type(links[0]).__name__ if links else "unknown",
            list(self._statuses),
        )
        for link in links:
            if isinstance(link, GuardCallError):
                return link
        by_status = classify_status(self._statuses)
        if by_status is not None:
            return by_status
        for link in links:
            if isinstance(link, httpx2.TimeoutException):
                return GuardCallError("Guard unreachable (timeout)", cause=GuardFailure.UNAVAILABLE)
        for link in links:
            if isinstance(link, httpx2.TransportError):
                return GuardCallError("Guard unreachable (network)", cause=GuardFailure.UNAVAILABLE)
        message = f"Guard call failed ({type(links[0]).__name__ if links else 'unknown'})"
        return GuardCallError(message, cause=GuardFailure.UNAVAILABLE)

    def tool_names(self) -> frozenset[str]:
        return frozenset(self._arguments)

    def tool_arguments(self) -> Mapping[str, tuple[str, ...]]:
        return dict(self._arguments)

    def result_shapes(self) -> Mapping[str, int]:
        return dict(self._shapes)

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        if tool not in GUARD_READ_TOOLS:
            message = f"tool not allowed: {tool}"
            raise GuardCallError(message, cause=GuardFailure.DATA)
        if self._portal is None or self._client is None:
            message = "Guard session is not open"
            raise GuardCallError(message, cause=GuardFailure.UNAVAILABLE)
        if self._monotonic() - self._started > self._run_budget_s:
            logger.error("guard run budget of %d s exceeded", int(self._run_budget_s))
            message = f"Guard run budget of {int(self._run_budget_s)} s exceeded"
            raise GuardCallError(message, cause=GuardFailure.UNAVAILABLE)
        portal, client = self._portal, self._client
        result = self._guarded(lambda: portal.call(client.call_tool, tool, dict(args)))
        if result.is_error:
            message = f"Guard tool {tool} returned an error"
            raise GuardCallError(message, cause=GuardFailure.DATA)
        payload, shape = extract_result(result.structured_content, result.content)
        self._shapes[shape] += 1
        return payload


def open_guard(
    state_dir: Path,
    *,
    clock: Clock,
    oauth_transport: FormTransport | None = None,
    http_transport: httpx2.AsyncBaseTransport | None = None,
    run_budget_s: float = DEFAULT_RUN_BUDGET_S,
) -> McpGuardTools:
    """An unentered Guard session that logs in from the credential file in `state_dir`."""
    store = CredentialStore(state_dir)
    source = RefreshingTokenSource(store, oauth_transport or UrllibOAuthTransport(), clock)
    return McpGuardTools.for_url(
        GUARD_MCP_URL, source, http_transport=http_transport, run_budget_s=run_budget_s
    )
