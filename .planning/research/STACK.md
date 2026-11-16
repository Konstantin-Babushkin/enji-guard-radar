# Stack Research

**Domain:** Scheduled project-health monitor and alerting service (poll an MCP server's tools deterministically, apply pure rules, Claude triage, Telegram alerts, file state, no database)
**Researched:** 2026-11-16
**Confidence:** HIGH on the Claude, Telegram, scheduling and typing/state choices (official docs plus local probing). MEDIUM on the MCP client, because the Guard server's transport, auth and protocol version are unknown and only an in-process and a fake-401 probe were possible.

## Headline Decisions

1. **MCP client:** official `mcp` SDK **2.x** (`mcp>=2.3,<3`). It is the stable line, and `pip install mcp` now installs 2.x. v1.x is in maintenance only.
2. **One HTTP stack: `httpx2`.** In Nov 2026 both `mcp` 2.x and `anthropic` 2.x depend on `httpx2` (Pydantic's continuation of httpx). The plain `httpx` package is not installed by either, and its last stable release is 0.28.1 (Dec 2024). Use `httpx2` directly for Telegram. Do not add `httpx`.
3. **Claude triage:** the **Anthropic Python SDK**, `client.messages.parse(..., output_format=PydanticModel)`, behind a small `Triager` boundary. Not the Agent SDK. `claude -p` is a documented drop-in alternative, but only if the operator wants subscription billing instead of an API key.
4. **Telegram:** `httpx2` calling `sendMessage` directly with `parse_mode="HTML"`. Not `python-telegram-bot`, and not MarkdownV2.
5. **Scheduling:** a one-shot CLI run by a **systemd timer** (`Type=oneshot`, `Persistent=true`). Cron with `flock -n` is the fallback. No in-process scheduler.
6. **Config and state:** Pydantic v2 models everywhere. YAML for human-edited config (`yaml.safe_load` into models). `pydantic-settings` for env and `.env` secrets (`SecretStr`). JSON state files written atomically (tmp, fsync, `os.replace`) under an `fcntl.flock` lock. All stdlib.

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | 3.12 (pin `.python-version` to 3.12) | Runtime | The scaffold already targets py312 in ruff and mypy. Pinning 3.12 stops tests passing on the dev box (uv currently picks 3.14.7) while prod runs 3.12. All libs below support 3.12 through 3.14. |
| uv | 0.12.x (installed 0.12.21) | Env, lock, run | Already in the scaffold. `uv_build>=0.8,<0.13` is compatible with 0.12.21. |
| pydantic | `>=2.12` (locked 2.14.3) | Typed snapshot, findings, state, Claude output schema, config | One model system for MCP result parsing, state files, the Claude output schema (`model_json_schema()`) and config. `mcp` requires `>=2.12`. The mypy plugin is already enabled. |
| mcp (official MCP Python SDK) | `>=2.3,<3` (2.3.0, 2026-10-02) | Call Guard tools from plain code, no LLM | Official SDK. v2 is the stable line (docs: "`pip install mcp` installs 2.x"). `Client(url)` or `Client(StdioServerParameters)` gives `list_tools()` and `call_tool()` with typed results (`structured_content`, `content`, `is_error`). The client auto-negotiates and falls back to the classic handshake on older servers. Ships `py.typed`. |
| httpx2 | `>=2.16` (2.16.2, 2026-11-16) | HTTP for Telegram, and the transport under `mcp` and `anthropic` | Pydantic-stewarded continuation of httpx 0.28.1 with the same API, under the import name `httpx2`. It is already a transitive dependency of `mcp` and `anthropic`, so Telegram adds no new HTTP stack. It provides `Client`, `AsyncClient` and `MockTransport` for tests. |
| anthropic | `>=2.8,<3` (2.8.0, 2026-11-10) | Claude triage with schema-validated output | Single-turn, no-tools triage fits the plain Messages API. `client.messages.parse(output_format=Model)` sends `output_config.format`, validates, and returns `parsed_output`. It has built-in retries and timeouts, needs no subprocess or bundled binary, and is trivially fakeable. |
| Claude model | `claude-sonnet-5-5` default, config-overridable (`claude-haiku-5-5` cheaper) | Triage and explanation | Sonnet 5.5 is $2 / $10 per MTok, retirement not before 2027-09-28. Volume is tiny, so cost is irrelevant and quality matters more. Haiku 5.5 ("classification, extraction, routing") is a fine downgrade. Model IDs are pinned snapshots, so behavior will not drift. |
| systemd timer + oneshot service | host systemd | Scheduling | Gives `Persistent=true` catch-up after downtime, `RandomizedDelaySec`, journald logs, `OnFailure=`, and no overlap (a running service is not restarted by the timer). |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pydantic-settings | `>=2.16` (2.16.3) | Load `TELEGRAM_BOT_TOKEN`, Guard credentials and the Anthropic key from env or `.env`. Also `YamlConfigSettingsSource` for the project config. | Always. It validates at startup, `SecretStr` keeps secrets out of `repr` and logs, and it bundles `python-dotenv`. |
| pyyaml | `>=6.0.3` | Per-project thresholds config. Use `yaml.safe_load` only. | Always (already a dependency). Pair with `types-pyyaml` in dev. |
| anyio | transitive (4.16.0) | Async runtime under `mcp` | Do not add it directly. Wrap the async MCP session in one sync function that calls `asyncio.run(...)`, so the rest of the codebase stays synchronous. |
| python-dotenv | transitive (via pydantic-settings) | `.env` loading | Do not add it directly. |

Standard-library pieces that replace dependencies: `os.replace`, `tempfile`, `fcntl.flock` (atomic state and single-instance lock), `logging` (with a redaction filter), `argparse` (CLI with `poll` and `digest` subcommands), `zoneinfo`, `json`.

### Development Tools

| Tool | Version | Purpose | Notes |
|------|---------|---------|-------|
| ruff | `>=0.17` (locked 0.17.5) | Lint and format | Existing rule set is fine. Raise the floor from `>=0.8`. |
| mypy | `>=2.4` (locked 2.4.1) | Strict typing | `strict = true` plus the pydantic plugin is already configured. `mcp`, `anthropic`, `httpx2` and `pydantic-settings` all ship `py.typed`. |
| pytest | `>=9.2` (locked 9.2.0) | Tests | Table tests with `@pytest.mark.parametrize`. `filterwarnings = ["error"]` is already set (see Pitfalls below). |
| `httpx2.MockTransport` | bundled with httpx2 | Fake Telegram in tests | Inject `transport=` into the Telegram client. No third-party mock library needed. |
| `mcp.server.mcpserver.MCPServer` | bundled with mcp | In-process fake Guard | `Client(server)` runs the real client wrapper against an in-process server, with no network. Verified working. |

Pure functions take `now: datetime` as a parameter, so no `freezegun` or `time-machine` is needed. The core stays sync, so no `pytest-asyncio` is needed (call the sync wrapper, or use `asyncio.run` in the few boundary tests).

## Verified Findings per Question

### (1) MCP client with no LLM (HIGH for API shape, MEDIUM for fit with the real Guard server)

Verified against py.sdk.modelcontextprotocol.io (client, transports, OAuth, what's new, migration) and by running mcp 2.3.0 locally.

```python
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

# Remote Streamable HTTP with a static bearer token / API key header
async with httpx2.AsyncClient(
    headers={"Authorization": f"Bearer {token}"},
    timeout=httpx2.Timeout(30.0, read=300.0),   # the default httpx2 timeout is 5s
) as http:
    transport = streamable_http_client(url, http_client=http)
    async with Client(transport) as client:
        tools = await client.list_tools()                 # .tools[].name
        result = await client.call_tool("name", {"k": "v"})
        if result.is_error:                               # always check before trusting data
            ...
        data = result.structured_content                  # dict | None; fall back to result.content text

# Local stdio (the child gets only a minimal env; pass secrets via env=)
async with Client(StdioServerParameters(command="uvx", args=[...], env={"GUARD_TOKEN": token})) as client:
    ...
```

Facts that shape the design:

- **v2 API:** `Client` is the entry point. `streamablehttp_client` and `get_session_id` are removed. All attributes are snake_case (`is_error`, `structured_content`, `input_schema`). `streamable_http_client` no longer takes `headers=` or `timeout=`. Pass your own `httpx2.AsyncClient` as `http_client=` and you own its lifecycle.
- **Errors:**
  - A tool failure does not raise. It returns `is_error=True`, with the message in `content` and `structured_content=None`. An unknown tool is also `is_error=True`.
  - `MCPError` is raised only when the server replies with a JSON-RPC error.
  - **Verified locally:** an HTTP 401 surfaces as a nested `ExceptionGroup` wrapping `MCPError(-32603, "Server returned an error response")`. The status code is lost. The boundary must unwrap groups (`except* MCPError`) and map them to one intent-named `GuardUnavailable` or `GuardAuthFailed`. When the HTTP client is yours, you can add an `event_hooks={"response": [...]}` hook to capture the 401 status.
- **Auth options:**
  - Static bearer or API key: headers on your `httpx2.AsyncClient`. This is the simplest and best for unattended runs.
  - Unattended OAuth: `ClientCredentialsOAuthProvider` from `mcp.client.auth.extensions.client_credentials` (or `PrivateKeyJWTOAuthProvider`). Pass `issuer=` explicitly, because omitting it is deprecated and becomes required in 3.0. The provider is an `httpx2.Auth` attached via `AsyncClient(auth=...)`. `streamable_http_client` has no `auth=` keyword.
  - `OAuthClientProvider` is for interactive browser flows. Avoid it unless a persisted `TokenStorage` file (tokens and client_info) is already seeded once by the operator.
- **Protocol negotiation:** the default is `mode="auto"`: probe, then fall back to the classic handshake on older servers. The latest version is 2026-07-28. This is documented but untested against a real old server (see Gaps).
- **TLS:** httpx2 uses the OS trust store via `truststore`. A minimal container or private CA may need `SSL_CERT_FILE` or `SSL_CERT_DIR`.
- **Fallback:** `mcp>=1.28,<2` (1.30.0 is the latest 1.x) stays a supported maintenance line, with the old `ClientSession` plus `streamablehttp_client` API on plain `httpx`. Use it only if a Phase 1 connection spike shows 2.x cannot talk to Guard.

### (2) Claude headless with structured output (HIGH)

Verified against the Claude Code headless and Agent SDK docs, the platform structured-outputs and models docs, `claude --help` (local v2.1.322), and the installed anthropic 2.8.0 `messages.parse` signature.

| Option | Verdict | Reason |
|--------|---------|--------|
| **Anthropic API (`anthropic`)** | **Use** | Triage is one prompt in, one typed object out. Structured outputs are GA on all models with no beta header. `output_config.format` constrains decoding via a compiled grammar, and the SDK validates into your Pydantic model. Auth is a single `ANTHROPIC_API_KEY` env var. Retries and timeouts are built in. |
| `claude -p` | Alternative only | Spawns the full Claude Code agent loop, with tools, settings and CLAUDE.md discovery, to answer a single-shot question. Only worth it for subscription billing. |
| Claude Agent SDK (`claude-agent-sdk` 0.2.182) | Avoid | It is a 160 MB wheel that bundles the Claude Code binary, is pre-1.0, and is async with an agent loop. The `structured_output` plus retry-on-mismatch machinery is nice but unnecessary here, and the SDK is worse to fake in tests. |

Recommended call shape:

```python
class ProjectTriage(BaseModel):          # all fields required, flat, no dict fields
    finding_id: str
    summary: str
    probable_cause: str
    priority: Literal["p1", "p2", "p3"]   # lowercase single tokens (enum casing is not guaranteed)

class TriageResult(BaseModel):
    items: list[ProjectTriage]

resp = client.messages.parse(
    model="claude-sonnet-5-5", max_tokens=4096, timeout=60,
    system=SYSTEM_PROMPT, messages=[{"role": "user", "content": json_payload}],
    output_format=TriageResult,
)
if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
    ...  # fall back to the rule-only message
```

Constraints from the docs that shape the schema:

- No recursive schemas.
- No numeric or string-length constraints. The SDK moves them into descriptions, so enforce limits in your own post-validation.
- Objects need `additionalProperties: false`. The SDK handles this for Pydantic models.
- Dict or map fields become `{}`. Use a list of `{key, value}` instead.
- At most 24 optional params and 16 union-typed params per request. Make everything required.
- `stop_reason == "refusal"` can return HTTP 200 with non-conforming output. `max_tokens` can truncate. Treat both as fallback triggers.
- Enum and const casing is not guaranteed. Compare case-insensitively, or normalise.
- The first request with a new schema pays grammar compile latency. Grammars are cached for 24 h.
- Sonnet 5.5 does not accept `thinking: disabled`. Leave thinking at the default and give `max_tokens` headroom.

Always cross-check the output against the input. Every `finding_id` Claude returns must exist in the rule output, and it may only attach text and priority. It can never add or remove an alert. This matches the project rule that rules decide and Claude explains.

`claude -p` alternative recipe. All flags below are confirmed in the local `claude --help` and the headless docs:

```bash
cd "$(mktemp -d)"   # empty cwd: no CLAUDE.md, no project .mcp.json, no hooks
claude -p --output-format json --json-schema "$SCHEMA_JSON" \
  --tools "" --no-session-persistence --max-budget-usd 0.50 \
  --permission-prompts none --model sonnet < prompt.txt
# parse stdout JSON: .structured_output (key absent => treat as failure); also check .is_error
```

- Auth: `--bare` is the officially recommended scripted mode, but it ignores OAuth, so it needs `ANTHROPIC_API_KEY`, which defeats the point of this alternative. Without `--bare`, `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`, one-year, subscription-billed) works, but the CLI then auto-discovers settings and hooks, so run it from an empty directory.
- Exit code: 0 on success, non-zero on failure, 143 on SIGTERM.
- Policy: the docs say Anthropic does not permit third parties to offer claude.ai login to their own users. For the operator's own unattended use this is a question for the operator (flag as ASSUMPTION), so default to the API key.
- Keep both implementations behind one `Triager` protocol (`triage(findings) -> TriageResult | None`), so the choice stays a one-file swap.

### (3) Telegram Bot API (HIGH, verified against core.telegram.org/bots/api and /bots/faq)

- **Use `httpx2` directly.** One `POST https://api.telegram.org/bot<TOKEN>/sendMessage` with a JSON body `{chat_id, text, parse_mode, link_preview_options: {is_disabled: true}}`. `python-telegram-bot` 22.8 depends on `httpx<0.29`, which is a second HTTP stack that nothing else here uses. It is an async bot framework (updates, handlers, job queue) and we only send.
- **Response contract:** always JSON with `ok`. On failure: `description`, `error_code`, and optional `parameters.retry_after` (flood control) and `parameters.migrate_to_chat_id` (group upgraded to supergroup). Parse this yourself and do not use `raise_for_status()` (see Pitfalls: it embeds the token-bearing URL in the exception text).
- **Limits:** `text` is 1-4096 characters after entities parsing. A group allows about 20 messages per minute, a single chat about 1 per second, and bulk about 30 per second. We send a handful of messages per run, so a simple strategy is enough: on 429 sleep `retry_after` and retry once or twice, then fail loudly. Cap or split at about 4000 characters on line boundaries, which matters for the weekly digest.
- **Use HTML parse mode, not MarkdownV2.** HTML needs only `&`, `<` and `>` escaped (`html.escape(s, quote=False)`). MarkdownV2 requires escaping `_ * [ ] ( ) ~ ` > # + - = | { } . !` plus `\` outside entities, with separate rules inside code and link URLs. Finding titles, repo names and Claude prose are full of `.`, `-`, `(` and `_`. A single missed escape returns 400 "can't parse entities" and the alert is lost. HTML failure is rarer and simpler to prevent. Always escape every dynamic string, including Claude's output.
- **Defence in depth:** if `sendMessage` fails with 400 and a parse error, resend the same text as plain text with `parse_mode` omitted and the tags stripped. An unformatted alert is better than none.
- **Group specifics:** group ids are negative. Keep `chat_id` as `int | str` in config. A migrated group returns `migrate_to_chat_id`, so log it loudly and tell the operator to update config, rather than silently rewriting config. Support an optional `message_thread_id` for forum topics.

### (4) Scheduling (HIGH for semantics, host unknown)

Verified `OnCalendar`, `Persistent`, `RandomizedDelaySec`, `AccuracySec` and overlap behavior against systemd.timer(5). Caveat: the dev box has no `systemctl`, so the unit files are untested here.

- **Shape:** the program is an idempotent one-shot CLI: `enji-guard-radar poll` (rules, triage, dedup, immediate alerts) and `enji-guard-radar digest` (weekly). Each invocation takes the lock, reads state, does its work, writes state and exits.
- **systemd (recommended):** `radar-poll.timer` with `OnCalendar=hourly` (or the operator's choice) and `Persistent=true`, plus `RandomizedDelaySec=120`. `radar-digest.timer` with `OnCalendar=Mon 09:00` and `Persistent=true`. The service is `Type=oneshot`, with `EnvironmentFile=/etc/enji-guard-radar.env` (mode 0600), `WorkingDirectory=`, `ExecStart=uv run --frozen enji-guard-radar poll` and `OnFailure=radar-failed.service`. If a service is still running when the timer elapses, systemd does not start a second copy, and it fires again right after the run finishes.
- **cron fallback:** `*/60 * * * * flock -n /run/user/$UID/radar.lock uv run --frozen enji-guard-radar poll`. Cron has no catch-up and no failure hook, so the program must supply them.
- **In-process scheduler (APScheduler, `schedule`, a sleep loop): reject.** It needs a supervised long-lived process, loses missed runs on crash, and complicates tests, and a state file already gives restart safety.
- **The radar dying must not look like "silence means healthy".**
  - Record `last_success_at` in state.
  - Have `OnFailure=` (or a `try/except` at the top level) send a rate-limited "radar failed" message.
  - Show `last successful poll` in the weekly digest.
  - Optionally ping an external dead-man's-switch URL after each successful run (operator's choice).
- **Single-instance lock inside the program too:** `fcntl.flock(LOCK_EX | LOCK_NB)` on `state/.lock`, so a manual run and a timer run cannot interleave.

### (5) Typed models, config and atomic state (HIGH)

- **Models:** frozen Pydantic models (`ConfigDict(frozen=True, extra="forbid")`) for `Snapshot`, `Finding`, `Alert`, `State`. Parse the MCP payload into the snapshot model at the boundary with `model_validate`. Unknown or missing fields then fail fast and are logged, rather than flowing into rules. Because the Guard data shape is unknown, keep the raw payload as an untyped `dict` at the edge and validate once.
- **Config split:**
  - `config.yaml` (committed, no secrets): projects in scope, default thresholds, per-project overrides, chat id, timezone and quiet hours. Load with `yaml.safe_load`, then `Config.model_validate(...)`. A bad file stops the run before any alert goes out.
  - Env or `.env` (git-ignored, already in `.gitignore`): `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, Guard credentials, as `SecretStr`. `pydantic-settings` handles this. Secrets are never accepted from YAML.
- **State:** `state/state.json` with `schema_version`, `last_success_at`, per-finding records (`key = project|rule|subject`, `first_seen`, `last_alerted`, `status`), and `last_digest_at`. Choose JSON for machine-written state (stdlib, deterministic, diff-friendly). Reserve YAML for human-edited files.
- **Atomic write recipe** (stdlib only): `tempfile.NamedTemporaryFile(dir=state_dir, delete=False)`, then write, `flush()`, `os.fsync(fd)`, `os.replace(tmp, final)`, then `fsync` the directory. The temp file must be in the same directory so `os.replace` stays atomic on one filesystem. Write state after the Telegram send succeeds for that alert, so a crash causes at worst a duplicate, never a lost alert (decision to confirm in the roadmap: at-least-once vs at-most-once).
- **Corrupt or unknown-version state:** refuse to run, send an operator alert, and keep a `.bak` of the previous generation. Never silently reset, because a reset would re-alert everything.

## Installation

```bash
# Runtime
uv add "mcp>=2.3,<3" "anthropic>=2.8,<3" "httpx2>=2.16" "pydantic>=2.12" "pydantic-settings>=2.16" "pyyaml>=6.0.3"

# Dev (ruff, mypy, pytest and types-pyyaml already present; raise floors)
uv add --dev "ruff>=0.17" "mypy>=2.4" "pytest>=9.2" "types-pyyaml>=6.0.12"

# Pin the interpreter to match ruff/mypy targets
uv python pin 3.12
```

Resulting direct dependencies are 6 runtime and 4 dev. Everything else (anyio, python-dotenv, truststore, starlette and uvicorn pulled in by `mcp`) is transitive. `mcp` drags in server-side packages (`starlette`, `uvicorn`, `sse-starlette`, `pyjwt[crypto]`, `python-multipart`, `opentelemetry-api`) even for client use. Accept that cost, because it is the official client.

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| `mcp` 2.x | `mcp>=1.28,<2` | The Phase 1 spike shows 2.x cannot connect to or negotiate with Guard. v1 uses `ClientSession` plus `streamablehttp_client` on plain `httpx`. |
| `mcp` SDK in code | Spawn `claude -p` with `--mcp-config` and let Claude call Guard tools | Never. The project decision is a deterministic poller, and an LLM in the fetch path makes data and rules non-reproducible. |
| `anthropic` `messages.parse` | `claude -p --json-schema` | The operator wants subscription billing and no API key, and accepts the CLI dependency and the ToS question. Keep the `Triager` boundary so the swap is one file. |
| `anthropic` | Claude Agent SDK | Triage later needs tools or multi-step investigation (for example, Claude reading repo files). Not in scope. |
| `httpx2` direct for Telegram | `python-telegram-bot` 22.8 | The bot ever needs to receive updates, commands or inline buttons (acknowledge or snooze). Revisit then, as a separate component. |
| HTML parse mode | MarkdownV2 | Never worth it here. Use HTML. |
| systemd timer | cron plus `flock` | The host has no systemd. Add heartbeat and failure notification in code. |
| JSON state files | SQLite | State outgrows files (queries across history). `docs/ideas/` item, not now. The project forbids a database. |
| `pydantic-settings` | `os.environ` plus manual validation | A tiny env-only config. Not recommended, since you lose `SecretStr` and fail-fast validation. |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `httpx` (plain), `pytest-httpx`, `respx` | `mcp` 2.x and `anthropic` 2.x no longer install `httpx`. `pytest-httpx` pins `httpx==0.28.*` and `respx` needs `httpx>=0.25`, so they would patch the wrong library and reintroduce a second HTTP stack. Passing an `httpx` client to `mcp` as `http_client=` degrades silently, or raises `TypeError` with `auth=`. | `httpx2` with `httpx2.MockTransport` (verified present). `httpx2-pytest` is third-party and unverified, so skip it. |
| `python-telegram-bot` | Async framework for receiving updates. Pulls in `httpx<0.29`. One `sendMessage` does not need it. | `httpx2` direct. |
| MarkdownV2 | 18 special characters, context-dependent escaping, and one miss drops the whole alert. | `parse_mode="HTML"` with `html.escape`. |
| `claude-agent-sdk` | 160 MB bundled binary, 0.x API churn, async agent loop for a one-shot prompt. | `anthropic` SDK. |
| `streamablehttp_client`, `ClientSession` with v1 idioms, `headers=` or `timeout=` on `streamable_http_client` | Removed or changed in v2. They raise `TypeError`. | `Client(...)` plus your own `httpx2.AsyncClient`. |
| `raise_for_status()` on Telegram calls | The exception text includes the full URL, which contains the bot token. A logged traceback leaks it. | Parse the `ok` and `description` JSON yourself and raise your own exception that never includes the URL. |
| Default INFO logging of httpx2 | Verified locally: `INFO:httpx2:HTTP Request: POST https://api.telegram.org/bot123456:SECRET-TOKEN/sendMessage "HTTP/1.1 200 OK"`. The token is in clear text. | `logging.getLogger("httpx2").setLevel(WARNING)`, same for `httpcore2`, plus a redaction filter on token patterns. |
| `yaml.load`, or secrets in YAML | `yaml.load` can build arbitrary objects. YAML is committed, and secrets never go in git. | `yaml.safe_load`, with secrets from env only. |
| APScheduler or a `while True: sleep` loop | Needs a supervised long-lived process, loses missed runs, harder to test. | systemd timer, or cron. |
| `tenacity` and similar retry libraries | Speculative dependency. `anthropic` has built-in retries, and Telegram needs a 10-line 429 or 5xx loop. | Small hand-written loop in the Telegram boundary. |
| `freezegun`, `time-machine`, `pytest-asyncio` | Not needed if rules take `now` as an argument and the core is sync. | Pass `now`, and keep async confined to one `asyncio.run` boundary. |
| `structlog` | Extra dependency. The AGENTS rule is a log line on important branches. | stdlib `logging` with a redaction filter. |
| `filelock` | stdlib `fcntl.flock` is enough on Linux. | `fcntl.flock`. |
| `jsonschema` direct use | Pydantic already validates. `jsonschema` is transitive via `mcp`. | `model_validate`, `TypeAdapter`. |

## Stack Patterns by Variant

**If Guard exposes a static token (API key or PAT) over Streamable HTTP:**
- Use `httpx2.AsyncClient(headers={"Authorization": ...})` passed as `http_client=`.
- Because it is the only fully unattended, zero-state auth path.

**If Guard requires OAuth client credentials:**
- Use `ClientCredentialsOAuthProvider(..., issuer=...)` attached to the `httpx2.AsyncClient(auth=...)`.
- Implement `TokenStorage` as a small file-backed class (0600) so tokens and `client_info` persist across runs. Without `client_info` the provider re-registers on every run.

**If Guard needs interactive OAuth (authorization code):**
- The operator performs a one-off login that seeds the token file, and the poller reuses and refreshes it.
- Treat refresh failure as a reportable "radar blind" condition (alert the operator), not as healthy silence.

**If Guard is a local stdio server (`npx` or `uvx` command):**
- Use `StdioServerParameters(command, args, env={...})`. Pass credentials explicitly via `env=`, since the child inherits only a minimal allow-list. Use `stdio_client(server, errlog=file)` to keep its stderr out of alert text.

**If the operator wants subscription billing instead of an API key:**
- Implement `Triager` via `claude -p` per the recipe in section (2). Use `CLAUDE_CODE_OAUTH_TOKEN` without `--bare`, run from an empty temp directory, and treat the missing `structured_output` key as failure.

## Version Compatibility

| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| `mcp` 2.3.0 | `httpx2>=2.10`, `pydantic>=2.12`, `anyio>=4.9`, Python >=3.10 | It depends on `mcp-types==2.3.0`, so keep them in lockstep (uv does this). It does not install `httpx`. |
| `anthropic` 2.8.0 | `httpx2>=2.0,<3`, `pydantic>=1.10,<3` | Same HTTP stack as `mcp`, so `uv lock` resolves a single `httpx2`. Verified together in one venv: `httpx2 2.16.2`, `httpcore2 2.16.2`, `anyio 4.16.0`. |
| `pydantic-settings` 2.16.3 | Python >=3.11, pydantic v2 | The scaffold's `>=3.12` is fine. |
| `httpx2` 2.16.2 | Python >=3.11 | `import httpx2`. Logger names are `httpx2` and `httpcore2`. User-Agent is `python-httpx2/<ver>`. Uses the OS trust store, with `SSL_CERT_FILE` and `SSL_CERT_DIR` still honored first. |
| `claude-agent-sdk` 0.2.182 | `mcp<3,>=1.23` | Only relevant if added later. It would coexist with `mcp` 2.x. |
| pytest `filterwarnings = ["error"]` | `import mcp, anthropic, httpx2, pydantic_settings` | Verified clean under `python -W error`. Runtime warnings are still possible (`MCPDeprecationWarning`, `HTTPXDeprecationWarning` as `UserWarning`, unclosed-resource warnings). Pass `issuer=` to OAuth providers and always close clients with `async with`. |
| mypy 2.4.1 strict + pydantic plugin | `mcp`, `anthropic`, `httpx2`, `pydantic-settings` | All ship `py.typed`. `content` blocks are a union, so narrow with `isinstance(block, TextContent)` before reading `.text`. |

## Gaps and Assumptions

- **ASSUMPTION:** Guard is reachable as an MCP server over Streamable HTTP or stdio. Its tool names, auth scheme, protocol version and payload shape are unknown. The `Client` was verified in-process and against a fake 401 server only. **Phase 1 should start with a connection spike** that records `client.protocol_version`, `server_info`, the `list_tools()` output (names and `input_schema`) and a sample `call_tool` payload, saved as test fixtures.
- **ASSUMPTION:** the deployment host has systemd. The dev box does not, so unit files are unverified here.
- **Unverified:** the `effort` setting on `output_config` for lowering triage cost or latency (the parameter exists on `messages.parse`, but its values were not checked). Not required.
- **Unverified:** whether `claude -p --json-schema` runs cleanly as a subscription-billed unattended job on the target host (flags confirmed via `--help` and docs, but not executed, because it would spend tokens and needs auth). Only matters if the operator picks that path.
- **Decision for the roadmap:** at-least-once vs at-most-once delivery (write state before or after the Telegram send). The recommended default is at-least-once, because duplicate alerts cost less than a missed high-risk alert.

## Sources

- https://py.sdk.modelcontextprotocol.io/ (and `/client/`, `/client/transports/`, `/client/oauth-clients/`, `/whats-new/`, `/migration/`): v2 client API, auth, error semantics, httpx2 switch. HIGH.
- PyPI JSON API (queried 2026-11-16): mcp 2.3.0, httpx2 2.16.2, anthropic 2.8.0, pydantic 2.14.3, pydantic-settings 2.16.3, python-telegram-bot 22.8, claude-agent-sdk 0.2.182 (wheel about 160 MB), ruff 0.17.5, mypy 2.4.1, pytest 9.2.0, plus dependency metadata (`requires_dist`) for each. HIGH.
- Local probe, mcp 2.3.0 and anthropic 2.8.0 in a scratch venv: in-process `Client(MCPServer)` result shapes (`is_error`, `structured_content`), 401 mapped to a nested `ExceptionGroup`/`MCPError(-32603)`, `httpx2` INFO log containing the bot token, `httpx2.MockTransport` present, clean imports under `-W error`, `messages.parse` signature. HIGH (executed).
- https://code.claude.com/docs/en/headless: `-p`, `--bare`, `--output-format json`, `--json-schema`, `structured_output` field, exit codes, `--permission-prompts none`. HIGH. Cross-checked with local `claude --help` (v2.1.322).
- https://code.claude.com/docs/en/agent-sdk/structured-outputs and `/agent-sdk/overview`: Agent SDK structured output, `error_max_structured_output_retries`, the third-party claude.ai login restriction. HIGH.
- https://code.claude.com/docs/en/authentication: `CLAUDE_CODE_OAUTH_TOKEN` and `claude setup-token`, auth precedence, `--bare` not reading OAuth. HIGH.
- https://platform.claude.com/docs/en/build-with-claude/structured-outputs: `client.messages.parse`, `output_config.format`, GA with no beta header, schema limits, refusal and `max_tokens` caveats, enum casing, grammar caching. HIGH.
- https://platform.claude.com/docs/en/about-claude/models/overview: model IDs, pricing, retirement dates. HIGH.
- https://core.telegram.org/bots/api: `sendMessage`, 4096 characters after entity parsing, MarkdownV2 and HTML escaping rules, `ResponseParameters` (`retry_after`, `migrate_to_chat_id`). HIGH.
- https://core.telegram.org/bots/faq: flood limits (about 1 per second per chat, 20 per minute per group, about 30 per second bulk). HIGH.
- https://pydantic.dev/docs/httpx2/get-started/migration/ and https://github.com/pydantic/httpx2: httpx2 as the Pydantic-stewarded continuation, import rename, `truststore`, logger names. MEDIUM-HIGH (docs fetched, third-party mock-library compatibility unverified).
- https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html: `OnCalendar`, `Persistent`, `RandomizedDelaySec`, `AccuracySec`, overlap behavior. HIGH.

---
*Stack research for: scheduled MCP-polling project-health monitor with Claude triage and Telegram alerts*
*Researched: 2026-11-16*
