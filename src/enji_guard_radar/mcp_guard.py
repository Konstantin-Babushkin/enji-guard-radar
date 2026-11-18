"""Guard MCP adapter: the sync `GuardTools` port over the official `mcp` SDK.

Shell module: it owns an event-loop thread (an anyio blocking portal) and one SDK client for the
whole run. Only the four read tools can reach the transport. Messages are fixed text written
here; nothing the server or an HTTP library says is copied into a message or a log line.
"""

import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Any, Self

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp.client.client import Client
from mcp.types import ContentBlock, TextContent

from enji_guard_radar.guard import GUARD_READ_TOOLS
from enji_guard_radar.ports import GuardCallError, GuardFailure

logger = logging.getLogger(__name__)

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


class McpGuardTools:
    """Implements `ports.GuardTools` and `ports.GuardSession`; enter it before calling."""

    def __init__(
        self,
        connect: Callable[[], AbstractAsyncContextManager[Client]],
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._connect = connect
        self._monotonic = monotonic
        self._portal_manager: AbstractContextManager[BlockingPortal] | None = None
        self._portal: BlockingPortal | None = None
        self._session: Any = None
        self._client: Client | None = None
        self._arguments: dict[str, tuple[str, ...]] = {}
        self._shapes: Counter[str] = Counter()

    def __enter__(self) -> Self:
        portal_manager = start_blocking_portal()
        portal = portal_manager.__enter__()
        try:
            session = portal.wrap_async_context_manager(self._connect())
            client = session.__enter__()
            try:
                self._list_tools(portal, client)
            except BaseException:
                session.__exit__(None, None, None)
                raise
        except BaseException:
            portal_manager.__exit__(None, None, None)
            raise
        self._portal_manager, self._portal = portal_manager, portal
        self._session, self._client = session, client
        logger.info("guard session open: %d tools listed", len(self._arguments))
        return self

    def __exit__(self, *exc: object) -> None:
        # Reverse order of entry: the SDK context first, the portal second, or the process hangs.
        try:
            if self._session is not None:
                self._session.__exit__(None, None, None)
        finally:
            if self._portal_manager is not None:
                self._portal_manager.__exit__(None, None, None)
            self._portal_manager = self._portal = self._session = self._client = None

    def _list_tools(self, portal: BlockingPortal, client: Client) -> None:
        listed = portal.call(client.list_tools)
        self._arguments = {
            tool.name: tuple(sorted(tool.input_schema.get("properties", {})))
            for tool in listed.tools
        }

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
        result = self._portal.call(self._client.call_tool, tool, dict(args))
        if result.is_error:
            message = f"Guard tool {tool} returned an error"
            raise GuardCallError(message, cause=GuardFailure.DATA)
        payload, shape = extract_result(result.structured_content, result.content)
        self._shapes[shape] += 1
        return payload
