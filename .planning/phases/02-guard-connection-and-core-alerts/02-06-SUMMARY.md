# 02-06 Summary: MCP adapter for the Guard port

- **Dependencies** (operator choice "mcp-2x", "packages verified"): `mcp>=2.3,<3`, `httpx2>=2.16`, `pydantic>=2.12`; `.python-version` 3.12. Names added to `uv.lock` (25 new; pydantic and pyyaml were already locked; the other names of the 28-package research list were not new or are platform-only): anyio attrs cffi click cryptography h11 httpcore2 httpx2 httpx2-jsfetch idna jsonschema jsonschema-specifications mcp mcp-types opentelemetry-api pycparser pyjwt python-multipart pywin32 referencing rpds-py sse-starlette starlette truststore uvicorn 
- `ports.py`: `GuardSession` protocol, `GuardConnector`.
- `mcp_guard.py` (shell): `McpGuardTools` (sync bridge over an anyio blocking portal; SDK context exited before the portal), `RefreshingBearer`, `classify_status`, `extract_result`, `open_guard`. Allowlist `GUARD_READ_TOOLS` is checked before any I/O; `run_audit` cannot reach the transport. 401 forces exactly one refresh and one retry; 403, 429, 5xx, timeouts and network errors map to fixed texts; exception text from httpx2 or the SDK never reaches a message or a log. Run budget 600 s, HTTP timeouts 30 s (read 60 s).
- `secret_guard.py`: `"mcp"` in `QUIET_LOGGERS`; `jwt` secret pattern.
- Tests: `tests/test_mcp_guard.py` (in-process `MCPServer` as fake Guard; `httpx2.MockTransport` for status and transport failures; no sockets), `tests/test_secret_guard.py`.

## Differences from the plan text
- `McpGuardTools.__init__` takes `bearer: RefreshingBearer | None` instead of `token_source`: the adapter only needs to ask the bearer for one forced refresh; `for_url` still takes the token source.
- The SDK matched the research spikes (mcp 2.3.0: `Client`, `streamable_http_client`, `mcp.server.mcpserver.MCPServer`, `mcp.types`). A 401 arrives as `ExceptionGroup` with the statuses `[401, 401]` in the hook (discover probe, then initialize); a token-source error raised in the auth flow is found inside the group and re-raised unwrapped. In the fake-Guard tests the tools need an explicit `name=` on `MCPServer.tool`.
- The tracer compares the run summary, per-project verdicts, baselined alerts and messages with the fixture source; only the wording of an unknown project's reason differs (adapter text vs fixture error text).
- `list_tools` reads the first page only (Guard has a handful of tools).

## Left for the operator
- First real connection to Guard (plan 02-11), after `enji-guard-radar auth`. `probe` (plan 02-09) records which result shape Guard really uses (assumption A6 in 02-RESEARCH.md). No Guard credentials or real data were available; nothing here opened a socket.
- Verification: `make verify`.
