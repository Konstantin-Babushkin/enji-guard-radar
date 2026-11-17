# Phase 2: Guard Connection and Core Alerts - Research

**Researched:** 2026-11-17
**Domain:** Unattended OAuth (public client, PKCE, refresh rotation) to a remote MCP server over streamable HTTP; accumulated High-count history and alert lifecycle; ops-chat health; `probe`, `auth`, `doctor`; systemd timer
**Confidence:** MEDIUM overall. HIGH for the SDK, token-layer, Telegram and systemd facts (read from package source, official docs and local spikes). MEDIUM for Guard/Fleet behaviour, because no authenticated call was allowed. Everything about refresh-token lifetime, rotation, tool argument names and result shape is `ASSUMPTION:` until the first real `auth` and `probe`.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** The radar accumulates history in its own state. Each poll records the High count and the Guard `task_id` per `(repo, audit type)`. It does not read older Guard reports to reconstruct history. Age ("days since the first High run") and the M-clean-runs clear rule are computed from this accumulated history, counted by Guard `task_id`, not by radar polls. — **Reversibility:** costly — the state schema (schema 1 already has an optional `ConditionRecord.count`) and every rule that reads history depend on it; changing to backfill later needs a state migration.
- **D-02:** Consequence accepted: age starts at the first poll, so a long-standing High finding shows as young. Keep that visible in the alert wording or docs rather than hiding it. Backfill from older reports is a possible later improvement (see Deferred).
- **D-03:** If the radar's Guard token cannot be refreshed unattended (expired, revoked, dead refresh token), treat it like any failed Guard fetch: the poll is "unknown" and never creates or resolves findings. After K consecutive failed polls the ops chat gets one message saying the login needs re-auth (naming the `auth` command, never a token or code). One recovery message follows after re-auth (OPS-02).
- **D-04:** The radar requests read scopes only. It has its own login, separate from Claude Code's, created once by the operator. OAuth codes, tokens and callback URLs are never pasted into chat or logs.
- **D-05:** Every member of the executive group may see security-finding summaries, counts only: repo, audit type, High count (e.g. "High rose from 1 to 3 in audit.security"), age. Group messages never carry finding titles, file paths, secrets or raw evidence (RULE-03). Existing secret gate and escaping from Phase 1 still apply.

### Claude's Discretion
- `probe` design and sanitization rules for real responses written to `fixtures/` (placeholders for names, emails, secrets, code snippets, finding evidence).
- Timer mechanism (systemd or cron), where it runs, the values of K (failures before ops message) and M (clean runs to clear), and how poll cost is measured and logged. These depend on the deployment host and real Guard data; defaults live in validated config, thresholds are finalised from real data.
- Exact `auth` command UX and where the refresh token is stored (`.env` or `state/`, never in git).

### Deferred Ideas (OUT OF SCOPE)
- Backfill High-count history from older Guard reports on first sight of a `(repo, audit type)` for accurate age from day one (revisit after real data shows how often age matters).
- Phase 1 open points carried forward, not closed here: `>=` N days vs "older than" wording, zero-project listing treated as "Guard unavailable", never-ran immediate alert vs ALERT-01, STATE-01 count part.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SRC-01 | The radar connects to the Enji Guard MCP non-interactively and lists the projects in scope | Pattern 1 (SDK transport behind the existing `GuardTools` port, sync bridge), Pattern 2 (own token layer), Pattern 4 (failure classes). Live proof waits for the login |
| SRC-02 | A `probe` command records real Guard responses as sanitized fixtures | Pattern 7 (allowlist sanitizer, taint-set leak check, round-trip parse invariant, recorded-fixture tests) |
| RULE-02 | A `(repo, audit type)` with at least one High in every completed run for longer than a threshold is flagged, with age | Pattern 5 (per-series history, `opened_at`, age wording "seen by radar since") |
| RULE-03 | A rise in High count in `audit.security` / `audit.public-api-access-safety` between two completed runs is flagged, no raw evidence | Pattern 5 (rise detection, counts-only message per D-05) |
| RULE-09 | Clear only after M consecutive completed runs with zero High | Pattern 5 (debounced `clean_runs` counted by `task_id`) |
| STATE-07 | A rise in High count re-arms its alert | Pattern 5 (`alerted_count` high-water mark in `ConditionRecord`, re-arm in `reconcile`) |
| ALERT-01 | Immediate alert only for a new high-risk finding or newly stale project | Pattern 5 (only open/rise are immediate; aged flag is informational until the Phase 3 digest), Open Question 2 |
| OPS-01 | Unattended schedule with a one-shot command and a timer | Pattern 8 (systemd oneshot + timer, `TimeoutStartSec`, no `flock` wrapper on `radar.lock`; cron fallback) |
| OPS-02 | After K consecutive failed polls one ops message, one more on recovery | Pattern 6 (pure `advance_health`, ack-gated marking, exactly-once sequences) |
| OPS-03 | `doctor` checks Guard reachability, Telegram token, bot membership | Pattern 8 (`getMe`, `getChatMember`, Guard check through the port) |
</phase_requirements>

## Project Constraints (from CLAUDE.md / AGENTS.md)

- Stack is Python 3.12 + uv, mypy strict, no database, state in files under `state/` (git-ignored). `make verify` (ruff, format check, mypy strict, fake-done marker scan, pytest) is the only definition of done. [VERIFIED: /home/factory/enji-guard-radar/CLAUDE.md]
- Simplest thing that works, surgical edits. Rules, dedup and message formatting are pure functions with table tests. I/O (Guard MCP, Claude, Telegram, files) sits behind small intent-named boundaries and is faked in tests.
- Secrets (`TELEGRAM_BOT_TOKEN`, Guard credentials) come from env or `.env`, never from git, logs or alert text. Unverified claims about Guard MCP tools, Telegram API or CLI flags are checked for real or flagged `ASSUMPTION:`.
- New behaviour ships with tests and a log line on the important branches. Conventional Commits, one concern each. `check-markers` forbids `TODO`, `FIXME`, `XXX` and `NotImplementedError` anywhere under `src/`. [VERIFIED: Makefile `check-markers` target]
- Hard constraints for this phase: never request `audits:run`, never call `run_audit`; no token, code or callback URL in chat or logs (D-04).

## Summary

The plans can be split cleanly into work that needs no live Guard (about 80% of the phase) and a short gated tail. Everything on the Guard side hangs off one decision: **use the official `mcp` SDK (2.3.x) only as the streamable-HTTP/MCP transport, and write a small own OAuth token layer (refresh, rotation, classification, atomic persistence) instead of the SDK's `OAuthClientProvider`.** Two reasons were verified locally against mcp 2.3.0 source and spikes. First, in a fresh process the provider sends the refresh request to `<resource origin>/token` (here `https://guard.enji.ai/token`) instead of the authorization server's token endpoint, because it only learns the real endpoint after a 401-triggered discovery; Guard's authorization server is on a different origin (`https://fleet.enji.ai`), so unattended refresh would fail and fall into the interactive flow. Second, the provider treats every non-200 refresh answer the same way (clear tokens, start interactive login), so `invalid_grant` (re-auth needed) cannot be told apart from a 503 (just retry). Our own layer fixes both, is a pure function over (status, body), and makes D-03 testable.

The public authorization-server metadata (fetched unauthenticated) shows a public client (`token_endpoint_auth_methods_supported: ["none"]`), dynamic client registration, PKCE S256 only, grants `authorization_code` and `refresh_token`, RFC 9207 `iss` support, and a revocation endpoint; no device-code endpoint. So the one-time `auth` command is a hand-rolled authorization-code + PKCE flow with a **paste-the-redirect-URL-into-a-hidden-terminal-prompt** step (or an SSH-forwarded loopback listener), never through chat. Refresh-token rotation for public clients is required by RFC 9700, so the design assumes rotation (`ASSUMPTION:` for Guard) and persists the new refresh token atomically before using it, under a file lock, from every command that can refresh (`run`, `doctor`, `probe`, and a `--dry-run` that talks to real Guard).

For High-count history (D-01), keep per-`(project, repo, audit type)` series in State schema 2 (bump from 1, with a pure 1-to-2 migration), updated by a pure fold over newly seen completed runs keyed by `task_id`. An "open" episode starts at the first observed High run and ends only after M consecutive clean runs (RULE-09). Re-arm (STATE-07) is a high-water mark `alerted_count` on the condition record: an alert is re-sent only when the count exceeds the highest count already alerted in this episode, which also stops 3-2-3 flapping. An unknown or failed report never advances a series and never removes its alert (otherwise the ledger would "resolve" and then "re-alert", the double flood from PITFALLS 2). Ops health (K failures, one outage message, one recovery message) is a second pure state machine stored in the same State and marked only after Telegram acknowledges.

**Primary recommendation:** Build the pure cores first (State v2, High history, health), then the token layer and `auth`, then the MCP adapter, then `doctor`/`probe`/deploy; use `mcp>=2.3,<3` only as transport with a per-request bearer from our own `TokenSource`; hold the live-login tail (auth, probe, dry-run review, soak) as one gated human plan.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| OAuth token refresh, rotation, classification | Pure core (`guard_auth`) | Shell (credential file, HTTP form POST) | Classification is a pure function of (status, body); persistence and HTTP are I/O |
| Interactive `auth` login | Shell CLI | Pure core (PKCE, URL build, redirect parse) | Needs a terminal and a human; its logic is testable with fakes |
| MCP transport to Guard | Shell adapter (`mcp` SDK) | Pure core (result-shape extraction) | Network and async; hidden behind the sync `GuardTools` port |
| High-count history, rise, age, debounce | Pure core | State file | Deterministic fold over runs; tested by tables |
| Alert dedup and re-arm | Pure core (`ledger.reconcile`) | State file | Existing pattern; extended with `alerted_count` |
| Poll health, K failures, outage/recovery message | Pure core (`advance_health`) | Shell (Telegram ops notifier) | Decision is pure; send and mark-after-ack are the existing pipeline pattern |
| `probe` sanitization and leak scan | Pure core (sanitizer) | Shell (recorder, file write) | Sanitizer is table-testable; recording is I/O |
| `doctor` checks | Shell | Pure core (verdicts from responses) | Calls Telegram and Guard; verdict mapping is pure |
| Timer, logging, secrets file | Host (systemd or cron) | App (lock, exit codes) | Scheduling is not application code; the app supplies idempotence and exit codes |

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `mcp` | `>=2.3,<3` (2.3.0, 2026-10-02) | Streamable-HTTP MCP client (`Client`, `streamable_http_client`) | Official SDK; negotiates 2026-07-28 vs legacy handshake itself; ships `py.typed` [VERIFIED: PyPI JSON `requires_dist`, wheel read this session] |
| `httpx2` | `>=2.16` (2.16.2, 2026-11-16) | HTTP client under `mcp`; our per-request bearer `httpx2.Auth` and `MockTransport` in tests | Required by `mcp` 2.x (`httpx2>=2.10.0`); declare it directly because our adapter imports it [VERIFIED: PyPI JSON] |
| `pydantic` | `>=2.12` (raise the floor from 2.9) | Typed credentials, state v2, config | `mcp` requires `pydantic>=2.12.0` [VERIFIED: PyPI `requires_dist`; pyproject.toml currently `pydantic>=2.9`] |
| stdlib `urllib`, `secrets`, `hashlib`, `base64`, `getpass`, `fcntl`, `os` | 3.12 | Token endpoint form POST, PKCE, hidden prompt, file lock, atomic write | Already the project pattern (`telegram.py` uses `urllib`; `state_store.py` uses `fcntl`/`os.replace`) |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `anyio` | transitive (4.16.0) | `anyio.from_thread.start_blocking_portal` sync bridge | Inside the MCP adapter only. Do not add as a direct dependency unless mypy needs it |
| `pytest` `MockTransport` via `httpx2` | bundled | Fake 401/403/429/5xx at HTTP level without sockets | Adapter classification tests |
| `mcp.server.mcpserver.MCPServer` | bundled | In-process fake Guard serving fixture JSON | Adapter happy-path and shape tests (no sockets) |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Own token layer + SDK transport | SDK `OAuthClientProvider` end to end | Fewer lines, but refresh goes to the wrong URL in a fresh process (spike), failure classes are lost, and `invalid_grant` is indistinguishable from 503. Only viable if `provider.context.oauth_metadata` is pre-seeded (internal state); rejected |
| SDK transport | Hand-rolled streamable HTTP over `urllib` | No new dependencies, but we would own SSE parsing, `Mcp-Session-Id`, `MCP-Protocol-Version` and the 2026-07-28 stateless path with no live server to test against. Rejected for Phase 2; revisit only if the 28-package tree is vetoed |
| `mcp` 2.x | `mcp>=1.28,<2` (1.30.0, `ClientSession` + `streamablehttp_client`, plain `httpx`) | Maintenance line; use only if a live connection spike shows 2.x cannot talk to Guard [CITED: .planning/research/STACK.md] |
| Paste-the-redirect `auth` | Loopback listener + SSH `-L` port forward | No pasting, but needs an SSH session set up; offer as `--listen` option, paste stays default |

**Installation:**
```bash
uv add "mcp>=2.3,<3" "httpx2>=2.16" "pydantic>=2.12"
```

**Version verification:** `uv pip compile --python-version 3.12` on `mcp>=2.3,<3` resolved 28 packages (anyio 4.16.0, cryptography 50.0.2, httpx2 2.16.2, jsonschema 4.26.0, mcp 2.3.0, mcp-types 2.3.0, opentelemetry-api 1.45.2, pydantic 2.14.3, pyjwt 2.15.1, starlette 1.10.0, uvicorn 0.54.0, and others). [VERIFIED: local `uv pip compile`, 2026-11-17]. The existing `.venv` is Python 3.14 while `requires-python` is `>=3.12` and there is no `.python-version`; the spikes ran on 3.12.14. Pin `.python-version` to 3.12 so tests match production. [VERIFIED: `ls .venv/lib` shows `python3.14`]

## Package Legitimacy Audit

| Package | Registry | Age | Downloads | Source Repo | Verdict | Disposition |
|---------|----------|-----|-----------|-------------|---------|-------------|
| mcp | PyPI | released 2025, 2.3.0 on 2026-10-02 | not reported by the seam | modelcontextprotocol.io / github.com/modelcontextprotocol/python-sdk | SUS (reason `unknown-downloads` only) | Approved with one `checkpoint:human-verify` before `uv add`. It is the official SDK documented at py.sdk.modelcontextprotocol.io (read this session) |
| httpx2 | PyPI | 2.16.2 on 2026-11-16 | not reported | github.com/pydantic/httpx2 | SUS (`too-new`, `unknown-downloads`) | Same checkpoint. Required by `mcp` 2.x and `anthropic` 2.x [CITED: .planning/research/STACK.md]; repo under the pydantic org per PyPI `project_urls` |
| mcp-types | PyPI | 2.3.0 on 2026-10-02 | not reported | modelcontextprotocol.io | SUS (`unknown-downloads`) | Transitive of `mcp`; covered by the same checkpoint |
| anyio | PyPI | 4.16.0 on 2026-11-01 | not reported | none listed | SUS (`too-new`, `no-repository`) | Transitive of `mcp` and `httpx2`; covered by the same checkpoint |

**Packages removed due to [SLOP] verdict:** none
**Packages flagged as suspicious [SUS]:** mcp, httpx2, mcp-types, anyio. The seam cannot read PyPI download counts, so `unknown-downloads` fires for every PyPI package (`httpx`, `authlib` and `httpx-sse` returned the same reason). The planner inserts one `checkpoint:human-verify` before the dependency is added: the operator confirms the names `mcp` and `httpx2` against the official docs page and the lockfile diff. `postinstall` is `null` for all four (PyPI has no install scripts; wheels only).

*No package was added from WebSearch or training data alone. `mcp` is tagged by the official docs; everything else is a transitive dependency resolved by `uv`.*

## Architecture Patterns

### System Architecture Diagram

```
 timer (systemd oneshot / cron) ──► enji-guard-radar run
                                         │
              ┌──────────────────────────┴───────────────────────────┐
              ▼                                                       ▼
   state lock (radar.lock, non-blocking)                    settings (.env / env)
              │ not acquired ─► exit 0 "LOCKED"
              ▼
   load State v2 ─ corrupt ─► halt, ops message (existing)
              │
              ▼
   TokenSource ── credential lock ── load guard-auth.json ── expiring? ──yes──► POST refresh_token grant
        │                                                                              │
        │                                          classify (status, body) ◄──────────┘
        │          ok ─► persist rotated tokens atomically BEFORE use      fail ─► AuthFailure(kind)
        ▼
   McpGuardTools (sync GuardTools port)
     sync bridge ► anyio portal ► mcp Client ► streamable_http_client ► httpx2 (Auth: per-request bearer,
                                                                          response hook records HTTP status)
        │  read-only allowlist: list_projects, list_repositories, list_audit_runs, get_audit_report
        ▼
   guard.list_projects / fetch_project (skip reports for task_ids already in history)
        │                                │ GuardAuthError / GuardCallError ─► ListingUnknown / ProjectUnknown (cause)
        ▼
   evaluate: stale (existing) + High series fold (new) ──► ProjectEvaluation(alerts, series updates)
        │
        ▼
   reconcile(State, evaluations) ──► to_send / baselined / resolved / re-armed ──► save State
        │
        ▼
   render ─► group notifier (Telegram) ─ ack ─► apply_delivery (alerted_at, alerted_count) ─► save
        │
        ▼
   PollResult(ok | failed(cause)) ─► advance_health(K) ─► ops notifier ─ ack ─► mark outage_reported / recovered
```

### Recommended Project Structure

```
src/enji_guard_radar/
├── guard_auth.py        # core: token model, expiry math, classify_token_response, PKCE, authorize URL, redirect parse
├── guard_credentials.py # shell: credential file (0600, atomic, lock), urllib form POST, discovery GET
├── mcp_guard.py         # shell: McpGuardTools (sync bridge), RefreshingBearer httpx2.Auth, result extraction
├── high_history.py      # core: SeriesHistory, advance_series, evaluate_series (pure fold)
├── high_rules.py        # core: series -> Alert (open, rise), wording inputs (counts only)
├── health.py            # core: PollHealth, advance_health, ops message text
├── doctor.py            # shell: checks, verdict mapping
├── probe.py             # shell recorder + core sanitizer split into probe_sanitize.py (core)
├── cli_auth.py / cli_doctor.py / cli_probe.py   # one module per command, 3-line registration in cli.py
deploy/
├── systemd/enji-guard-radar.service, enji-guard-radar.timer
└── cron.example
fixtures/guard-real/     # sanitized recorded fixtures (same calls.json manifest format as fixtures/guard)
```
New shell modules must be added to `SHELL_MODULES` in `tests/test_architecture.py:15`. `urllib` is in `FORBIDDEN_CORE_IMPORTS` (line 18), so URL building and redirect parsing (`urllib.parse`) must either live in a shell module or the test must allow `urllib.parse` explicitly (a pure string module). Recommended: allow `urllib.parse` only, with a comment.

### Pattern 1: Guard MCP behind the existing port (SRC-01)

**What:** `McpGuardTools` implements the sync `GuardTools.call(tool, args) -> Mapping[str, object]` port. It owns an `anyio` blocking portal and one `mcp.Client` for the whole run. It is a context manager (enter connects and lists tools; exit closes).

**Verified facts (spikes in this session, mcp 2.3.0, Python 3.12):**
- Sync bridge: `start_blocking_portal()` plus `portal.wrap_async_context_manager(Client(...))` gives a sync context manager; each `call` is `portal.call(client.call_tool, tool, dict(args))`. Both context managers must be exited in reverse order or the process hangs at exit (my first spike hung until the wrapped manager was exited). Works under the repo's `block_network` fixture and `filterwarnings=error`.
- A tool failure or unknown tool returns `is_error=True` and does not raise; `structured_content` is `None` then. A tool that returns a dict gives `structured_content == that dict` and a pretty-printed JSON `TextContent`; a tool that returns a str gives `structured_content == {'result': '<text>'}`.
- HTTP 401 surfaces as a nested `ExceptionGroup` ending in `MCPError("Server returned an error response")` with the status lost. An `httpx2.AsyncClient(event_hooks={"response": [hook]})` hook sees the real status codes (`[401, 401]`: the SDK probes `server/discover` then falls back to `initialize`). The same works with `httpx2.MockTransport`, so classification tests need no sockets.
- `streamable_http_client(url, http_client=...)` takes no `auth=`, `headers=` or `timeout=`; those live on the `httpx2.AsyncClient`. A custom client without a timeout gets httpx2's 5 s default; set `httpx2.Timeout(30.0, read=60.0)` or similar. [CITED: py.sdk.modelcontextprotocol.io/client/transports/]
- `Client.call_tool` on a 2026-07-28 connection retries once after re-listing tools if the server answers `HEADER_MISMATCH` (tool not listed); without a prior `list_tools()` the session logs a warning that it cannot validate structured content (`client/session.py:1133`). So the adapter calls `list_tools()` once on entry, which also yields the tool names for the allowlist check and for `probe`/`doctor`. [VERIFIED: mcp/client/client.py `call_tool` docstring; mcp/client/session.py line 1133]

**Result extraction (`ASSUMPTION:` about Guard's shape):** return `structured_content` if it is a dict; else parse a single `TextContent` as a JSON object; unwrap a lone `{"result": ...}` wrapper; anything else raises `GuardCallError("unrecognised tool result shape")`. `is_error` raises `GuardCallError` with a fixed, sanitized text (never the server's content). `probe` records which representation Guard really uses; the adapter's extraction is then pinned by a test on the recorded shape.

**Allowlist:** `McpGuardTools.call` rejects any tool outside `GUARD_READ_TOOLS` before any I/O, exactly like `FixtureGuardTools.call` (`fixture_source.py:77-79`). `run_audit` can never reach the transport. A test with a spy transport proves it.

**Failure mapping inside the adapter:**
- hook status 401 → `GuardAuthError("Guard rejected the access token")`; 403 → `GuardAuthError("Guard refused access (insufficient scope or permission)")`
- 429, 5xx, timeout, connect error → `GuardCallError` (transient, text states the class and status only)
- unwrap `ExceptionGroup` with `except*` and never include `str(exception)` of httpx2 errors (can contain URLs); log type name only.

**Logging:** the SDK logs `Received session ID: ...` at INFO (`client/streamable_http.py:172`). Add `"mcp"` to `QUIET_LOGGERS` (`secret_guard.py:32`) at WARNING and test that no INFO line from `mcp` reaches the handler. [VERIFIED: grep of installed mcp 2.3.0; QUIET_LOGGERS quoted below]

### Pattern 2: Own token layer (SRC-01, D-03)

**What:** `TokenSource.access_token() -> str` is called per request from an `httpx2.Auth` subclass (`auth_flow` sets `Authorization: Bearer ...`; the base `Auth` runs sync `auth_flow` for async clients [VERIFIED: httpx2/_auth.py docstring]). The source works under a credential lock:

```
with credential_lock():                 # flock on guard-auth.lock, short wait then give up
    creds = store.load()                # re-read: another process may have refreshed already
    if creds.access_valid(now, margin=120s): return creds.access_token
    outcome = refresh(creds)            # POST grant_type=refresh_token to creds.token_endpoint
    match classify(outcome):
        Success(tokens) -> store.save(merge(creds, tokens))  # atomic, fsync, BEFORE returning
        Failure(kind)   -> raise GuardAuthError / GuardCallError(kind)
```

- **Persist before use.** If the AS rotates, the old refresh token is dead the moment the new one is issued; write the new one first. If the write itself fails, keep the token in memory for this run, log an error, and report `auth` class: the next run will find the old token dead (RFC 9700 §2.2.2: public-client refresh tokens MUST be sender-constrained or rotated [CITED: rfc-editor.org/rfc/rfc9700 §2.2.2]; whether Guard rotates is `ASSUMPTION:`).
- **Never fall back to `.bak`.** Using a stale refresh token after rotation can trip reuse detection and revoke the whole grant. No automatic backup retry.
- **Absent `refresh_token` in a refresh response** means not rotated: keep the old one (RFC 6749 §6: the server "MAY issue a new refresh token"; if it does, the client "MUST discard the old refresh token"). [CITED: rfc-editor.org/rfc/rfc6749 §6]
- **Absolute expiry.** Store `expires_at` (UTC) computed from `expires_in` at receipt with the injected clock, not the relative value.
- **Credential file location:** `state/guard-auth.json` (mode 0600, atomic write, fsync file and directory), not `.env`. `.env` and systemd `EnvironmentFile` are read-only inputs and cannot be rewritten safely on rotation. Reuse the `_atomic_write` recipe from `state_store.py` by extracting it into a small shared helper (surgical refactor; keep `FileStateStore` tests green).
- **Locking.** The existing run lock (`radar.lock`, non-blocking) stays. Add a separate credential lock around the refresh critical section so `doctor`/`probe` can refresh safely while a run is active. The double-checked reload under the lock prevents two refreshes.
- **Dry-run pitfall.** `DryRunStateStore` yields `True` from `lock()` without touching disk (`state_store.py:145-147`) and `--dry-run` must not write alert state. A dry-run against real Guard still refreshes and **must persist the rotated token**, or the login breaks. So the credential store and credential lock are never dry-run stubs; only `state.json` writes are suppressed. Document it and test it.
- **Classification (pure function):** see Pattern 4.

### Pattern 3: One-time `auth` command (headless, no codes in chat)

**Public metadata, fetched unauthenticated this session** (verbatim from `https://fleet.enji.ai/.well-known/oauth-authorization-server`):
`{"authorization_endpoint":"https://fleet.enji.ai/api/v1/oauth/authorize","authorization_response_iss_parameter_supported":true,"client_id_metadata_document_supported":true,"code_challenge_methods_supported":["S256"],"grant_types_supported":["authorization_code","refresh_token"],"issuer":"https://fleet.enji.ai","jwks_uri":"https://fleet.enji.ai/api/v1/oauth/jwks","registration_endpoint":"https://fleet.enji.ai/api/v1/oauth/register","response_types_supported":["code"],"revocation_endpoint":"https://fleet.enji.ai/api/v1/oauth/revoke","token_endpoint":"https://fleet.enji.ai/api/v1/oauth/token","token_endpoint_auth_methods_supported":["none"]}` [CITED: fleet.enji.ai/.well-known/oauth-authorization-server]

Protected-resource metadata at `https://guard.enji.ai/.well-known/oauth-protected-resource/mcp` (the root path returned 404) reported `resource` `https://guard.enji.ai/mcp`, `authorization_servers` `https://fleet.enji.ai`, `scopes_supported` `projects:read`, `audits:read`, `audits:run`, `bearer_methods_supported` `header`. [CITED: guard.enji.ai/.well-known/oauth-protected-resource/mcp, summarized by the fetch tool; HANDOVER.md line 40 independently lists the same three scopes]

**Flow (hand-rolled, stdlib, synchronous):**
1. Take the credential lock. Discover: GET PRM, then GET AS metadata at `<issuer>/.well-known/oauth-authorization-server`. Require: `https` URLs only, `metadata.issuer == authorization_servers[0]`, `S256` in `code_challenge_methods_supported`, `refresh_token` in `grant_types_supported`, `none` in `token_endpoint_auth_methods_supported`. Store the discovered endpoints in the credential file so unattended runs never discover.
2. Dynamic client registration (RFC 7591) POST to `registration_endpoint`: `client_name`, `redirect_uris` (one loopback URI, see assumption), `grant_types` `["authorization_code","refresh_token"]`, `response_types` `["code"]`, `token_endpoint_auth_method` `"none"`, `scope` `"projects:read audits:read"`. Store `client_id`. (CIMD is advertised but needs a hosted HTTPS client-metadata document; DCR is simpler.)
3. Build the authorize URL: `response_type=code`, `client_id`, `redirect_uri`, `scope=projects:read audits:read`, random `state`, `code_challenge` (S256 of a 43-128 char verifier from `secrets`), `resource=https://guard.enji.ai/mcp` (RFC 8707; `ASSUMPTION:` Fleet accepts it). Print it to stdout only (never the logger).
4. Operator opens the URL in a browser on their own machine, approves, and the browser is redirected to the loopback URI, which fails to load. The operator copies the address-bar URL and pastes it into a **`getpass` prompt** (no echo, not argv, not shell history, not chat). Alternative `--listen`: bind `127.0.0.1:<port>` and use `ssh -L`.
5. Parse the pasted URL: require `state` equals ours (`secrets.compare_digest`), `iss` equals the issuer (RFC 9207, the AS sets `authorization_response_iss_parameter_supported: true`), `code` present, no `error`. Discard the URL immediately.
6. POST the token endpoint (`application/x-www-form-urlencoded`): `grant_type=authorization_code`, `code`, `redirect_uri`, `client_id`, `code_verifier`, `resource`.
7. **Validate before storing:** the response has `access_token`; **has `refresh_token`** (if not, fail loudly: unattended runs are impossible, MVP acceptance 2 cannot be met); `scope` (if present) is a subset of `{projects:read, audits:read}` and does not contain `audits:run`. Print only non-secret facts: scope, `expires_in` seconds, whether a refresh token was issued. The operator records the lifetimes in `docs/guard-data-notes.md` (Auth section).
8. Save atomically 0600; print "stored"; never print a token, code, verifier or URL.

`auth` is a human-run step on the production host (code-factory cannot run it). Everything else in it is unit-tested with a fake HTTP transport and a fake prompt.

`ASSUMPTION:` Fleet's DCR accepts an `http://127.0.0.1:<port>/callback` (or `localhost`) redirect URI (RFC 8252 loopback, how most MCP clients log in); if it rejects, the AS error text (safe, from the AS) tells the operator. `ASSUMPTION:` a refresh token is issued without an `offline_access` scope.

### Pattern 4: Failure classification (D-03)

Pure `classify_token_response(status: int, body: bytes) -> TokenOutcome` and `classify_mcp_status(status)`. RFC 6749 §5.2: error responses are HTTP 400 unless specified, with a fixed `error` list; `invalid_grant` means the grant or refresh token "cannot be accepted" (expired, revoked, issued to another client); `invalid_client` means client authentication failed or the client is unknown. [CITED: rfc-editor.org/rfc/rfc6749 §5.2]

| Signal | Kind | Counts as failed poll | Ops wording |
|--------|------|----------------------|-------------|
| No credential file | `not_authenticated` | yes | "login missing: run `enji-guard-radar auth`" |
| Credential file unreadable or schema-invalid | `not_authenticated` | yes | same, plus "file unreadable"; never delete it |
| Token endpoint 400 `invalid_grant` | `auth_expired` | yes | "login expired or revoked: run `auth`" |
| Token endpoint 400/401 `invalid_client` | `auth_client_gone` | yes | "client registration is gone: run `auth`" (re-registers) |
| Token endpoint 400 other (`invalid_scope`, `unauthorized_client`, `invalid_request`, `unsupported_grant_type`) | `auth_rejected` | yes | "login rejected (<error code>): run `auth`" |
| Token endpoint 429, 5xx, timeout, network error, 400 with non-JSON body | `unavailable` | yes | "Guard login service unreachable" (no re-auth wording) |
| 200 with malformed JSON or no `access_token` | `unavailable` | yes | same (data problem, do not tell the operator to re-auth) |
| MCP 401 after a fresh token | `auth_rejected` | yes | "Guard refused the token: run `auth`" |
| MCP 403 | `auth_forbidden` | yes | "Guard refused access (scope or permission): run `auth`" |
| MCP 429, 5xx, timeout | `unavailable` | yes | "Guard unreachable" |
| Tool `is_error` or malformed result for one call | data | no (project becomes unknown) | log only |

An MCP 401 on a token that has not just been refreshed in this process triggers exactly one forced refresh and one retry (the token may have been revoked or rotated by another process); a second 401 is final. Auth kinds map to ops text naming the `auth` command; the text never contains a token, code or URL (`secret_guard.find_secret` already gates ops messages through `SecretGatedNotifier`).

**Plumbing in existing code:** `GuardCallError` is the port's failure type (`ports.py:13`). Add `GuardAuthError(GuardCallError)` carrying a `kind`. `guard._pages` currently catches `(GuardCallError, GuardDataError)` and re-raises `GuardDataError` with text only (`guard.py:296-298`), which drops the class. Give `GuardDataError`/`ListingUnknown`/`ProjectUnknown` an optional `cause` (`auth`, `unavailable`, `data`) so the pipeline can build a `PollResult`. Failed poll = listing unknown, or at least one active project and every active project unknown. Mixed results (some projects unknown) are not a failed poll; they stay "unknown" per project (Open Question 9).

### Pattern 5: High-count history, rules and re-arm (RULE-02/03/09, STATE-07, ALERT-01)

**State schema 2** (bump `SCHEMA_VERSION` from 1; accept 1 and migrate purely; a Phase 1 radar then refuses a v2 file loudly instead of tripping `extra="forbid"`):

```python
class RunPoint(_Frozen):            # one counted Guard run of one series
    task_id: str
    completed_at: AwareDatetime
    high: StrictInt = Field(ge=0)

class SeriesHistory(_Frozen):       # key: (project_id, repo_id, action_key)
    project_id: str; repo_id: str; action_key: str
    points: tuple[RunPoint, ...]    # newest last, bounded window (default 20, must be >= M and Phase 3 N)
    open: bool = False              # an episode is active (alert may exist)
    opened_at: AwareDatetime | None # completed_at of the first High run of this episode
    clean_runs: StrictInt = 0       # consecutive zero-High runs since the last High run, while open

class ConditionRecord:              # existing, add:
    alerted_count: StrictInt | None # highest High count already alerted in this episode
```
Plus `PollHealth` (Pattern 6). No names, titles, paths or evidence in state (matches the ledger docstring "ids, levels, flags and timestamps only", `ledger.py:3-4`).

**Pure fold** `advance_series(history | None, observation, m) -> SeriesHistory | None`, `observation = Known(task_id, completed_at, high)` or `Unknown`:
- `Unknown` returns the history unchanged and never creates one.
- `Known` whose `task_id` is already in `points` is ignored (idempotent: same run seen twice never double-counts clean runs).
- `Known` older than the last point's `completed_at` is ignored with a warning log (late arrival; documented limitation). Equal times order by `task_id`.
- `high > 0`: if not open, open it (`opened_at = completed_at`); `clean_runs = 0`.
- `high == 0` while open: `clean_runs += 1`; at `clean_runs >= m` the episode closes (`open=False`, `opened_at=None`, `clean_runs=0`). A zero-High run while not open only appends a point.
- Append the point, trim to the window.
- First-run baseline: the first observation for a series is just a point; with the project baselined by `reconcile`, no alert is sent for already-High series.

**Evaluation** `evaluate_series(history, now, thresholds) -> SeriesView(open, high_now, previous_high, age_days, aged, rose)`:
- `age_days = (now - opened_at).days`, labelled in text "seen by radar since <date>" (D-02, and PITFALLS 4: an own-first-seen age must be labelled or omitted). Age is measured from the **completed_at of the first observed High run**, which is at or before the first poll and is Guard's own timestamp; this is the one deliberate refinement of D-02's "age starts at the first poll" (Open Question 4).
- `aged = open and age_days >= high_age_days` (RULE-02 flag). Informational in Phase 2 (see below).
- `rose = high_now > previous_high` between the two newest points (RULE-03).

**Alert emission** (pure, `high_rules.py`): while `open`, emit an `Alert` with `AlertKey(project_id, repo_id, action_key, level="high")` and `count = last known high` (may be 0 during debounce; `Alert.count` allows `ge=0`, `model.py:55`). While the episode is open the ledger keeps the condition (no "resolved" noise); when it closes after M clean runs the alert disappears and `reconcile` resolves it. A later High then opens a new episode and alerts as new. This composes with the existing ledger with no new "resolve" logic.

**Unknown report inside an evaluated project:** a series with history and `open` must still emit its alert from stored history (count = last known). Omitting it would make `reconcile` delete the condition (`ledger.py:173-175`) and re-alert later, the double flood. With no history and an unknown report, emit nothing and create nothing; the next poll retries because the `task_id` is not in history.

**Re-arm (STATE-07)** in `reconcile`, for an existing record with `alerted_at` set: if `alert.count` is not `None`, `record.alerted_count` is not `None` and `alert.count > record.alerted_count`, treat it as unalerted again: set `alerted_at=None` (so a crash before the send retries through the existing retry branch at `ledger.py:170-172`). `apply_delivery(state, keys, now)` sets `alerted_count = condition.count` for acknowledged keys (the only place `alerted_at` is set, `ledger.py:187-196`). High-water semantics: 3 (alerted) → 2 → 3 does not alert again; 3 → 2 → 4 does. Reset when the episode closes (the record is deleted). `ASSUMPTION:` this peak rule is the intended reading of "rise ... re-arms"; per-run rise would alert on every 2→3 oscillation (Open Question 1).

**ALERT-01 mapping:** immediate = episode opened (0→>0 on a known project, or a series first seen on a known project) and re-armed rise. RULE-02's aged flag is computed and shown in the alert text and the dry-run report, but does **not** by itself create an immediate alert (a long-standing High is not "new"); the Phase 3 digest and reminders (STATE-02) consume it. Open Question 2. RULE-03's audit-type list (`audit.security`, `audit.public-api-access-safety`) controls the wording and a config allowlist `high.rise_audit_types`; RULE-02 and STATE-07 apply to every audit type with scored findings. `audit.recon` (overview layout, no findings) never forms a series.

**Message (D-05):** counts only, e.g. "High findings rose from 1 to 3 in audit.security (acme/service-a), seen by radar since 2026-11-10". `Alert` needs `repo_name`, `action_key` and `previous_count` fields for text; the group message never receives titles, ids or paths, and the report parser already keeps only ids, severity and score (`guard_report.py` module docstring).

**Cost control:** today `fetch_project` reads the newest report per audit type for every repo every poll (`guard.py:356-363`). With history, pass a `known: Callable[[repo_id, action_key, task_id], bool]` so a report is read only when its `task_id` is new. This turns steady-state poll cost into `list calls + reports for genuinely new runs`. The first poll still reads one report per `(repo, audit type)`; measure it in the first real dry-run before the timer is enabled (poll-cost gate). Unparseable reports are re-read each poll (no `task_id` recorded); log each time and surface in `doctor`.

**Edge cases (each a table-test row):** same `task_id` twice; run missing this poll (no change, alert stays); report unknown with and without history; partial fetch (project unknown, everything frozen by `reconcile`); count decrease then rise (peak rule); 0→0→High while closed; High→0 once then High (still open, `clean_runs` resets, no re-alert unless above peak); exactly M clean runs closes, M-1 does not; first-run baseline (no alert); late older run ignored; two new runs between polls (only the newest report is read, so intermediate clean runs are not counted: conservative, slower to clear); equal `completed_at` ties; severities `UNKNOWN` (see Pitfall 9); zero-finding report layout (Pitfall 8); series on a paused project (frozen, as today).

### Pattern 6: Poll health and ops chat (OPS-02)

```python
class PollHealth(_Frozen):
    consecutive_failures: StrictInt = 0
    first_failure_at: AwareDatetime | None = None
    last_failure_cause: Literal["auth", "unavailable", "data"] | None = None
    last_success_at: AwareDatetime | None = None      # also feeds DIG-03 in Phase 3
    outage_reported_at: AwareDatetime | None = None   # set only after the ops chat acknowledged
```
Pure `advance_health(health, poll, now, k) -> (health, OpsAction)` with `OpsAction` in `none`, `outage(cause, since, failures)`, `recovered(since, duration)`:
- success with `outage_reported_at` set → `recovered` (keep the flag until the recovery message is acknowledged, then clear); success without it resets the counters silently.
- failure: increment; if `>= k` and `outage_reported_at is None` → `outage`; if already reported → `none` (exactly one message per outage, even when the cause changes).
- Marking `outage_reported_at` happens only after the ops notifier returns `Delivered` (same at-least-once rule as STATE-04). With no ops chat configured: log an error, mark nothing.
- A failed poll must still **save** state. Today the `ListingUnknown` branch returns before any save (`pipeline.py:162-164`); Phase 2 saves the health update there. Auth-class failures use ops wording that names `auth`; other causes do not.
- Dry-run prints the would-be ops message and writes nothing.
- Property test: for any sequence of success/failure polls, outage messages ≤ number of outages reaching K, recovery messages ≤ outage messages, no message when failures < K.

Defaults (`ASSUMPTION:` to be tuned from real data): K = 3, M = 2, poll every 3 hours. Rationale: audits complete weekly to monthly (guard-data-notes Cadence), run durations are up to about 2 hours, so hourly polling adds cost but not information; K = 3 at 3 hours means an outage is reported after about 9 hours. A permanent `auth` failure could use K = 1, but D-03 says "like any failed Guard fetch"; keep one K and make it configurable (`ops.failures_before_alert`, strict config).

### Pattern 7: `probe` and sanitization (SRC-02)

`probe` runs the same read walk as `run` through a recording decorator around the real `GuardTools` plus one `tools/list` (read-only protocol metadata; resolves the `ASSUMPTION:` on argument names at `guard.py:29-33` and `guard_report.py:21-22`, and records whether results arrive as `structured_content` or text). It never calls `list_audits`/`get_audit`/`run_audit` and writes **no raw response to disk**: sanitize in memory, then write only sanitized files into `fixtures/guard-real/` in the existing `calls.json` manifest format so `run --fixtures fixtures/guard-real` replays them.

**Allowlist, not denylist.** Per tool, keep only fields known to be structural, replace all free text:
- ids (`project.id`, `repo_…`, `project_id`, `task_id`, `repository_id`) → stable placeholders by order of first appearance (`00000000-0000-4000-8000-00000000a001`, ...), consistently in args and responses; cursors → `<opaque-cursor-1>`, `-2` (numbered, because the manifest forbids two recordings with identical tool and args, `fixture_source.py:61-74`).
- names → `acme-project-N`, `acme/service-N`; `provider`, `connected`, `status`, `report_status`, `task_available`, booleans, `has_more` → keep (enums are exactly what we need to observe: first real non-`completed` statuses).
- `action_key` → keep only if it matches `^audit\.[a-z0-9-]+$`, else `audit.unknown-N`; timestamps keep (not secrets; needed for the 3/4/6 fraction-digit cases); `report_url` → `https://guard.example/<report_group>?run=<mapped task_id>`.
- Report Markdown → rebuild from structure: placeholder title, `## Executive summary`, the real `**Score: N / 100** · <label> · Confidence: <word>` line (label is free prose: replace with `<label>`), placeholder repo line (`` `acme/service-N` · `main` · `0000000000000000000000000000000000000000` ``), the findings heading as found (`## Findings` or `## Security findings`), and per finding `` ### `<ID>` — Placeholder title `` plus the real `**Severity:** <word> · **Confidence:** <word>` line and `<placeholder>` bodies. Finding ids matching `^F-\d+$` stay; any other id (slug text can describe a vulnerability) becomes `SLUG-N`, preserving "slug style" so the unstable-id case stays testable. `content_version` → sha256 of the sanitized content.
- **Round-trip invariant (refuse to write on mismatch):** run `parse_report_markdown` on the raw content (in memory) and on the sanitized content; score, finding count, severities, `high_count` and the unknown/known outcome must be equal. This is also the real-data parser test: fenced code blocks that contain `## ` or `### ` lines in the raw report would show up as a mismatch and fail loudly instead of being silently scrubbed away.

**Leftover verification (three layers):**
1. *Taint set (strongest):* collect every string value from the raw responses (names, emails, titles, ids, urls, paths, words from report prose of length ≥ 4, minus a small stoplist of structural words) and assert none occurs, case-insensitively, as a substring in any output file before writing.
2. *Generic detectors on the written files* (also run in CI by `tests/test_recorded_fixtures.py`, no raw data needed): emails; any `http(s)://` host not ending in `example`; UUIDs and `repo_` ids not in the placeholder pattern; 40-hex strings other than all zeros; `secret_guard.SECRET_PATTERNS` (reuse); long base64/hex tokens (20+ chars); file paths with a source-file extension; the Telegram token shape.
3. *Human review:* `probe` prints a summary (counts, severities seen, statuses seen, report layouts seen) and never runs `git add`; the operator reads `git diff fixtures/guard-real` before committing.

**Recorded-fixture tests (`tests/test_recorded_fixtures.py`):** parametrize over `fixtures/guard-real*/`; load through `FixtureGuardTools`, run `list_projects` and `fetch_project` for every project; assert no `ProjectUnknown`, every `AuditReport` parses (not `ReportUnknown`), scores within 0..100, and record the severity words seen (a new word fails with the word named so the parser decision is explicit). A separate phase-gate test asserts that at least one recorded directory exists; before the login exists it is the single allowed `skip` and the verification plan must flip it to a hard assertion.

### Pattern 8: Timer, logging, secrets, `doctor` (OPS-01, OPS-03)

**systemd (recommended; the host is unknown, this container has no `systemctl`):**
- `Type=oneshot` service stays "activating" until the process exits and a nonzero exit is a failure; a timer firing while it is still active does not start another copy ("it is not restarted, but simply left running"). [CITED: freedesktop.org systemd.service(5), systemd.timer(5)]
- **`RuntimeMaxSec=` does not apply to oneshot services, and oneshot has no start timeout by default** [CITED: systemd.service(5)]. A hung Guard call would therefore hang forever and, because the unit stays active, silently stop all future runs. Set `TimeoutStartSec=15min` (and per-call httpx2 timeouts). This is the main unattended-run trap.
- Timer: `OnCalendar=` (for example `*-*-* 00/3:00:00` for every 3 hours, `ASSUMPTION:` syntax per systemd.time(7), not verified on this host), `Persistent=true` (missed runs fire on activation; only for `OnCalendar=` timers), `RandomizedDelaySec=` for spread, `AccuracySec` default 1 min. [CITED: systemd.timer(5)]
- Secrets: `EnvironmentFile=` takes `VAR=VALUE` lines, `#`/`;` comments, no shell expansion and no documented `export` prefix. [CITED: systemd.exec(5)] The app's own `.env` reader accepts `export` and quotes; keep the file plain `KEY=VALUE` so both parsers agree, mode 0600, not in git. The Guard credential file is **not** an env var (it is rewritten on rotation).
- `StateDirectory=` creates `/var/lib/<name>` and exempts it from `ProtectSystem=strict`; pass `--state-dir "$STATE_DIRECTORY"`. [CITED: systemd.exec(5)] Hardening candidates: `ProtectSystem=strict`, `PrivateTmp=true`, `NoNewPrivileges=true`, `UMask=0077`.
- Logs: stderr goes to the journal; filter with `journalctl -u enji-guard-radar`. `OnFailure=` is optional; the app already sends ops messages. Exit codes (pipeline.py:84-90): COMPLETED 0, ATTENTION 1, GUARD_UNAVAILABLE 1, LOCKED 0, HALTED 2. With `SuccessExitStatus=` left default, ATTENTION shows as a failed unit in `systemctl --failed`, which is the desired visibility.
- `ExecStart` should call the installed entry point (`uv sync --frozen --no-dev` at deploy, then `.venv/bin/enji-guard-radar run ...`), not `uv run` at timer time (no network or resolver at 03:00).

**cron fallback:** works, but has no catch-up, no failure hook and a minimal `PATH`. **Do not wrap the command in `flock -n state/radar.lock`:** the app opens the same path and takes its own `flock`; flock locks belong to the open file description, so the child is blocked by the wrapper (verified: the child printed "BLOCKED by wrapper flock on same path") and every run would exit 0 as "LOCKED" without doing anything. Rely on the in-app lock, or use a different lock path for any wrapper.

**`doctor`:** each check prints `PASS`/`FAIL`/`SKIP` with a short reason and never a secret; exit 0 only if no FAIL.
1. settings: token present, chat ids well-formed (`settings.py:11-13`).
2. state dir writable; `state.json` loads (not corrupt); credential file present, mode 0600, schema-valid.
3. Telegram `getMe` (no parameters, returns a `User` with `id`, `is_bot`, `username`) → token valid. [CITED: core.telegram.org/bots/api#getme]
4. For the group chat and the ops chat: `getChatMember(chat_id, user_id=<bot id from getMe>)` returns a `ChatMember` whose `status` is one of `creator`, `administrator`, `member`, `restricted`, `left`, `kicked`. PASS for `creator`, `administrator`, `member`; for `restricted` PASS only if `is_member` and `can_send_messages` are true; FAIL for `left`, `kicked`. The docs warn the method "is only guaranteed to work for other users if the bot is an administrator", which does not affect asking about the bot itself. Optional WARN via `getChat` → `ChatFullInfo.permissions.can_send_messages` false (default member permissions deny posting). A 400 with `parameters.migrate_to_chat_id` is the existing `CHAT_MIGRATED` class (`telegram.py` `classify_result`). `can_post_messages` is channels-only and must not be required for groups. [CITED: core.telegram.org/bots/api, fields read this session]
5. Guard: token usable (refresh if needed, under the credential lock), then MCP connect, `list_tools` has the four read tools, and one `list_projects` page. Failures map to the Pattern 4 kinds. This is the only check that needs the live login.
6. A dead timer is not detectable from inside the process (MVP known gap, Phase 3 digest closes it); `doctor` prints the `systemctl list-timers` hint as SKIP.
`TelegramNotifier` hardcodes `sendMessage` (`SEND_MESSAGE_METHOD`, `telegram.py:22`); add a small generic `call_method(method, payload) -> TelegramResult` that reuses `parse_telegram_response`, the token redaction and the existing fake transport (`tests/telegram_fakes.py:FakeTransport.post_json`).

**Poll cost (success criterion 4):** a counting decorator around `GuardTools` (injected monotonic clock so it can stay in core) logs one line per run: calls per tool, report pages, projects, repos, duration. Persisting `last_success_at` in `PollHealth` is already planned. Minimum calls per poll are `1 + P + R` list calls plus pagination plus reports only for new `task_id`s (steady state), versus `1 + P + R*K` reports per poll today. [CITED: docs/guard-data-notes.md "Cost of a poll"]

### Anti-Patterns to Avoid
- **Using `OAuthClientProvider` for unattended refresh.** Wrong refresh URL in a fresh process; lossy failure classes (Pattern 2).
- **Treating `.env` or `EnvironmentFile` as the refresh-token store.** It cannot be rewritten atomically on rotation.
- **Stubbing the credential store in `--dry-run`.** A real-Guard dry-run rotates the token; losing it kills the login.
- **Retrying with the `.bak` refresh token.** Can revoke the grant on reuse detection.
- **Omitting an open series' alert when its report is unknown.** The ledger resolves it, then re-alerts it.
- **Counting runs by poll.** Always by `task_id` (RULE-08 and D-01).
- **Passing the redirect URL, code or token through argv, env, logs or chat.**
- **A `flock` wrapper on `radar.lock` in cron.**
- **Running `probe` output through `git add` automatically.**

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| MCP streamable-HTTP transport (SSE, session id, protocol negotiation 2026-07-28 vs legacy) | A custom JSON-RPC/SSE client | `mcp` SDK `Client` + `streamable_http_client` | Version negotiation and SSE are exactly what drifts; no live server to test a hand-rolled one |
| PKCE challenge | Custom hashing code | `secrets.choice` verifier + `hashlib.sha256` + `base64.urlsafe_b64encode(...).rstrip("=")` (stdlib, 5 lines) | Mirrors the SDK's own `PKCEParameters.generate` [VERIFIED: mcp/client/auth/oauth2.py]; do not invent variants |
| Atomic file write | New write helper | The existing `_atomic_write` recipe (tmp, fsync, `os.replace`, dir fsync) extracted from `state_store.py` | Already tested for crash safety |
| Secret redaction and send gate | New regexes | `secret_guard.redact`, `find_secret`, `SecretGatedNotifier`, `configure_logging` | Already covers bearer tokens and the Telegram token shape |
| Telegram error classes | New parser | `parse_telegram_response`, `classify_result`, `TelegramResult` | `getMe`/`getChatMember` return the same envelope |
| HTTP 401 detection in the MCP path | Parsing exception messages | `httpx2` response event hook on the `AsyncClient` | The SDK drops the status (verified) |
| YAML config validation | Ad-hoc dict checks | Existing `RadarConfig` pydantic models (`extra="forbid"`) | RULE-10; add `high`, `ops` sections |
| Report parsing | Anything new | `guard_report.parse_report_markdown` | `probe` and the round-trip invariant reuse it |

**Key insight:** the dangerous parts here are not algorithmic; they are lifecycle (rotation races, dry-run persistence, ack-gated marking, "unknown must not resolve"). Reuse the Phase 1 patterns and extend them, do not parallel them.

## Runtime State Inventory

Not a rename or migration phase. One schema migration exists: **State 1 → 2** (add `histories`, `health`, and `ConditionRecord.alerted_count`). Existing `state/state.json` from Phase 1 runs (only on fixtures so far) must load through a pure `migrate_1_to_2`; the deserializer today accepts exactly `SCHEMA_VERSION` (`ledger.py:218-223`). Nothing else stores an old name: no external service config, OS registration, secrets or build artifacts depend on a renamed string. None — verified by reading the whole `src/` tree this session.

## Common Pitfalls

### Pitfall 1: SDK refresh goes to the wrong URL
**What goes wrong:** every unattended run silently falls into the interactive branch. **Why:** the provider only learns `token_endpoint` after a 401 discovery; before that it posts to `<resource origin>/token`. **Avoid:** own token layer with the stored `token_endpoint`. **Warning sign:** `Token refresh failed: 404` in logs. [VERIFIED: spike with mcp 2.3.0 mock transport: recorded `POST https://guard.example.test/token` then the discovery GETs]

### Pitfall 2: Refresh-token rotation lost to a crash or a race
**What goes wrong:** new refresh token issued, process dies before the write, next run gets `invalid_grant`. A concurrent `doctor` and `run` both refresh and the second burns the first's token. **Avoid:** persist before use, credential lock with re-read, one refresh per lock hold, never use `.bak`. The unavoidable crash window is detected next run and reported by ops (auth class).

### Pitfall 3: Dry-run that does not persist rotated tokens, or that stubs the lock
**Avoid:** credential store and lock are real in dry-run; only `state.json` and sends are suppressed. Test: dry-run with an expired token writes the credential file and not `state.json`.

### Pitfall 4: Silent stall of the timer
**What goes wrong:** a hung call keeps a `Type=oneshot` unit active forever (no start timeout, `RuntimeMaxSec` ignored), the timer never fires again, and there is no digest heartbeat in Phase 2. **Avoid:** `TimeoutStartSec`, per-call timeouts, and a total-run wall-clock budget in the adapter.

### Pitfall 5: `flock` wrapper makes every run a no-op
See Pattern 8. Verified.

### Pitfall 6: Failed poll not persisted, so K never advances
The current `ListingUnknown` branch returns before saving (`pipeline.py:162-164`). Health must be saved on failure paths too, including `GUARD_UNAVAILABLE`.

### Pitfall 7: Resolve-then-re-alert double flood
Open series with unknown report must still emit its alert from history (Pattern 5).

### Pitfall 8: Zero-finding reports may be "unknown" forever
`parse_report_markdown` returns `ReportUnknown("no findings heading")` when neither `## Findings` nor `## Security findings` exists (`guard_report.py:105-115`). If Guard omits the heading for a clean audit, a series never closes. `ASSUMPTION:` unknown; `probe` must look for it (an audit with score 100 or zero findings). Real data decides whether the parser needs a "no heading but score present means zero findings" path; do not guess.

### Pitfall 9: Severity words above High
`Severity.UNKNOWN` findings are not counted by `high_count` (`guard_report.py` `AuditReport.high_count`). If Guard has "Critical", the rule is blind to it. `probe` lists severity words seen; Open Question 7.

### Pitfall 10: SDK logs session ids at INFO; dependency tree is large
Quiet the `mcp` logger; one dependency checkpoint.

### Pitfall 11: First poll cost and rate limits
First poll reads one report per `(repo, audit type)`. Measure in dry-run before enabling the timer; an optional `high.audit_types` allowlist can narrow it.

### Pitfall 12: Auth/probe leak paths
Tokens or URLs in `argparse` errors, tracebacks (`logger.exception`), or `repr` of models. Use `SecretStr` for token fields, `repr=False`, redact in `RedactingFormatter` (already global), and never include the pasted URL in any exception.

## Code Examples

Values below are quoted from files read this session unless tagged.

### Existing seam values (verbatim)
```python
# src/enji_guard_radar/ports.py:13-20
class GuardCallError(Exception):
    """Message is sanitized text written by the adapter: never a URL, header or credential."""

class GuardTools(Protocol):
    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        """Raise GuardCallError on any failure."""
# src/enji_guard_radar/guard.py:26-28
GUARD_READ_TOOLS = frozenset(
    {TOOL_LIST_PROJECTS, TOOL_LIST_REPOSITORIES, TOOL_LIST_AUDIT_RUNS, TOOL_GET_AUDIT_REPORT}
)
# src/enji_guard_radar/guard.py:31-33 (argument names are still an ASSUMPTION per line 29-30)
ARG_PROJECT_ID = "project_id"
ARG_REPOSITORY_ID = "repository_id"
ARG_CURSOR = "cursor"
# src/enji_guard_radar/guard_report.py:22
ARG_TASK_ID = "task_id"
# src/enji_guard_radar/state_store.py:25-29
STATE_FILE = "state.json"
BACKUP_FILE = "state.json.bak"
LOCK_FILE = "radar.lock"
FILE_MODE = 0o600
DIR_MODE = 0o700
# src/enji_guard_radar/ledger.py:19
SCHEMA_VERSION = 1
# src/enji_guard_radar/settings.py:11-13
ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_OPS_CHAT_ID = "TELEGRAM_OPS_CHAT_ID"
# src/enji_guard_radar/secret_guard.py:32
QUIET_LOGGERS = ("urllib3", "httpx", "httpcore", "httpx2", "httpcore2")
# src/enji_guard_radar/pipeline.py:84-90
EXIT_CODES = {
    RunOutcome.COMPLETED: 0,
    RunOutcome.ATTENTION: 1,
    RunOutcome.GUARD_UNAVAILABLE: 1,
    RunOutcome.LOCKED: 0,
    RunOutcome.HALTED: 2,
}
# src/enji_guard_radar/model.py:19-21
class AlertKind(StrEnum):
    STALE = "stale"
    NEVER_RAN = "never_ran"
# tests/test_architecture.py:15,18
SHELL_MODULES = {"cli", "env_file", "fixture_source", "telegram", "state_store", "settings"}
FORBIDDEN_CORE_IMPORTS = {"socket", "urllib", "http", "fcntl", "subprocess", "ssl", "time"}
```
Also: `cli.py:132` is `run.add_argument("--fixtures", type=Path, required=True, metavar="DIR")`; Phase 2 makes `--fixtures` optional (absent means live Guard).

### Sync bridge over the async Client (spike, runs clean)
```python
# Verified against mcp 2.3.0 on Python 3.12.14; in-process MCPServer, no sockets.
from anyio.from_thread import start_blocking_portal

class SyncGuard:
    def __init__(self, cm):                       # cm: AbstractAsyncContextManager[Client]
        self._cm = cm
    def __enter__(self):
        self._portal_cm = start_blocking_portal()
        self._portal = self._portal_cm.__enter__()
        self._wrapped = self._portal.wrap_async_context_manager(self._cm)
        self._client = self._wrapped.__enter__()
        return self
    def __exit__(self, *exc):
        self._wrapped.__exit__(*exc)              # exit in reverse order or the process hangs
        self._portal_cm.__exit__(*exc)
    def call(self, tool, args):
        return self._portal.call(self._client.call_tool, tool, dict(args))
```

### HTTP status capture for classification (spike)
```python
seen: list[int] = []
async def hook(response: httpx2.Response) -> None:
    seen.append(response.status_code)

async with httpx2.AsyncClient(
    auth=RefreshingBearer(token_source),                     # our httpx2.Auth subclass
    event_hooks={"response": [hook]},
    timeout=httpx2.Timeout(30.0, read=60.0),
) as http:
    async with Client(streamable_http_client(GUARD_MCP_URL, http_client=http)) as client:
        ...
# A 401 raises ExceptionGroup(... MCPError("Server returned an error response")); map via `seen`, not the text.
```

### Pure token-response classification (shape)
```python
def classify_token_response(status: int, body: bytes) -> TokenOutcome:
    # 200 + JSON object with str access_token -> Refreshed(tokens)
    # 400/401 + JSON {"error": "invalid_grant"} -> AuthFailure("auth_expired")
    # 400/401 + {"error": "invalid_client"}      -> AuthFailure("auth_client_gone")
    # 400 + any other RFC 6749 §5.2 error code   -> AuthFailure("auth_rejected", code)
    # 429, 5xx, non-JSON, anything else          -> Transient(status)
    ...
```

### Pure health step (shape)
```python
def advance_health(health: PollHealth, poll: PollResult, now: datetime, k: int) -> HealthStep:
    # success: recovered if outage_reported_at is set, else reset silently
    # failure: failures += 1; outage message only when failures >= k and outage_reported_at is None
    ...
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `mcp` 1.x `ClientSession` + `streamablehttp_client` on `httpx` | `mcp` 2.x `Client` + `streamable_http_client` on `httpx2` | 2.0.0 July 2026 | Headers, auth, timeouts now live on your `httpx2.AsyncClient`; `streamablehttp_client` and `get_session_id` are gone [CITED: py.sdk.modelcontextprotocol.io/whats-new/] |
| Handshake + `Mcp-Session-Id` | 2026-07-28 stateless requests with `server/discover`, falling back to `initialize` | 2026-07-28 protocol | The SDK negotiates; do not hand-roll |
| Static tokens | OAuth 2.1 style public client, PKCE S256, RFC 9207 `iss`, refresh rotation for public clients | RFC 9700 (2025) | Validate `iss`, expect rotation |

**Deprecated/outdated:** `McpError` (now `MCPError`); `streamablehttp_client`. `[CITED: py.sdk.modelcontextprotocol.io/whats-new/]`

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Fleet issues a refresh token to this DCR public client without an `offline_access` scope | Pattern 3 | MVP acceptance 2 cannot be met; `auth` fails loudly at step 7 |
| A2 | Guard rotates refresh tokens (RFC 9700 requires rotation or sender-constraint for public clients) | Pattern 2 | If not rotated, persist-before-use is merely harmless; if rotated with reuse detection, races are fatal (design already handles) |
| A3 | Access token lifetime and refresh-token lifetime are unknown | Pattern 2/6 | Poll cadence and margin may need tuning; recorded by `auth` output |
| A4 | Fleet DCR accepts an `http://127.0.0.1:<port>/callback` (or `localhost`) redirect URI | Pattern 3 | `auth` registration fails; fallback is a different redirect or CIMD |
| A5 | Fleet accepts the RFC 8707 `resource` parameter on authorize/token/refresh | Pattern 3 | Drop the parameter if the AS rejects it |
| A6 | Guard tool results arrive as `structured_content` or one JSON `TextContent`, with the documented top-level keys (`projects`, `repositories`, `runs`, report fields) | Pattern 1 | Adapter extraction needs another shape; `probe` finds out |
| A7 | Guard tool argument names are `project_id`, `repository_id`, `task_id`, `cursor` (existing code `ASSUMPTION` comments) | Pattern 7 | Calls fail with `is_error`; `tools/list` in `probe` resolves |
| A8 | The 401 probe `server/discover` then `initialize` fallback works against Guard unchanged | Pattern 1 | Pin `mcp>=1.28,<2` fallback path |
| A9 | Peak-based re-arm is the intended reading of "a rise re-arms" | Pattern 5 | Per-run rise would alert on oscillation; wording decision needed |
| A10 | Defaults K=3, M=2, poll every 3 hours | Pattern 6 | Tune from real data (D-discretion) |
| A11 | `OnCalendar=*-*-* 00/3:00:00` is valid systemd calendar syntax | Pattern 8 | Timer fails to load; `systemd-analyze calendar` on the host checks it |
| A12 | A clean audit may omit the findings heading (report parsed as unknown forever) | Pitfall 8 | Series never close; parser extension needed |
| A13 | No severity above High exists | Pitfall 9 | Critical findings invisible to the High rule |
| A14 | `member` in a group can post unless default permissions forbid it | Pattern 8 | `doctor` passes while the bot cannot post; optional `getChat` WARN mitigates |
| A15 | Production runs Python 3.12 (dev `.venv` is 3.14) | Standard Stack | A 3.14-only change could slip in; pin `.python-version` |

## Open Questions

1. **Re-arm semantics: peak vs per-run rise**
   - Known: STATE-07 "a rise ... re-arms", RULE-03 "a rise ... between two completed runs".
   - Unclear: whether 3→2→3 should alert again.
   - Recommendation: peak-based (`alerted_count` high-water, reset when the episode closes); confirm with the operator, wording in the alert says "rose from X to Y".
2. **Does an aged baselined High alert immediately?**
   - Known: ALERT-01 limits immediate alerts to new high-risk or newly stale; RULE-02 wants a flag with age; success criterion 2 shows "a High-count alert with its age".
   - Recommendation: no immediate alert for age alone; age appears in open/rise alerts and the dry-run report; Phase 3 digest/reminders carry the aged list.
3. **Which audit types raise immediate High alerts?** RULE-03 names two; ALERT-01 says "new high-risk finding". Recommendation: every audit type with scored findings opens/re-arms (config `high.rise_audit_types` for the two security types controls wording only); operator may narrow with `high.audit_types`.
4. **Age origin.** Recommendation: `completed_at` of the first observed High run, labelled "seen by radar since"; a refinement of D-02 that is never later than the first poll.
5. **K, M and cadence.** Defaults above; finalise after the first real poll and cost measurement.
6. **Refresh-token behaviour (lifetime, rotation, reuse detection).** Only the first real `auth` and a 7-day soak answer it; plan the verification plan around that.
7. **Severity words and zero-finding layout.** `probe` summary must list severity words and any report without a findings heading before the High rule is trusted.
8. **Dependency weight.** `mcp` 2.x brings 28 packages (cryptography, starlette, uvicorn, opentelemetry-api, ...) into a one-shot client. Acceptable? If vetoed, the fallback is a hand-rolled client with its test risk (Alternatives).
9. **Per-project persistent "unknown".** One project permanently unknown is not a failed poll and stays invisible. Add a log line and a `doctor` line in Phase 2; ops-chat alerting for it is not in the requirements.
10. **Where does the radar run, and where does `state/guard-auth.json` live?** Host unknown; this container has neither `systemctl` nor `crontab`. Credentials must live on the production host's persistent disk (not a code-factory worktree).

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| uv | install, test | ✓ | 0.12.21 | — |
| Python 3.12 | production target | ✓ via uv (spikes ran 3.12.14); system `python3` is 3.14.7 | 3.12.14 / 3.14.7 | pin `.python-version` |
| PyPI access | `uv add mcp` | ✓ (resolved 28 packages) | — | — |
| `flock` (util-linux) | optional cron wrapper | ✓ `/usr/bin/flock` | — | not needed (in-app lock) |
| `systemctl` | systemd timer | ✗ on this host | — | cron; unit files are tested statically only |
| `crontab` | cron fallback | ✗ on this host | — | systemd on the production host |
| Guard login (`auth` done) | live verification | ✗ not created | — | everything else built on fakes |
| Telegram test chat and ops chat id | doctor, ops message live check | operator input | — | fake transport for tests |

**Missing dependencies with no fallback:** the radar's own Guard login blocks only the gated live plan. The deployment host decision blocks enabling the timer, not building the unit files.
**Missing dependencies with fallback:** systemd/cron are unavailable here; unit files and the cron example are validated by static tests and by `systemd-analyze verify` on the host during the live plan.

## Validation Architecture

> `workflow.nyquist_validation` is `false` in `.planning/config.json`; this section is included because the orchestrator explicitly asked for it.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.x (`pytest>=8.3`), `--strict-markers --strict-config`, `filterwarnings = ["error"]` [VERIFIED: pyproject.toml] |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]`; `tests/conftest.py` blocks sockets for every test |
| Quick run command | `uv run pytest -q tests/<file>.py -x` |
| Full suite command | `make verify` (baseline: 420 passed, 8.5 s) [VERIFIED: `uv run --offline pytest -q`] |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SRC-01 | Adapter returns port-shaped mappings from an in-process MCPServer; rejects `run_audit` before any I/O; maps hook 401/403/429/5xx to auth/unavailable classes; closes cleanly | unit + in-process | `uv run pytest -q tests/test_mcp_guard.py -x` | ❌ Wave 0 |
| SRC-01 | Token layer: refresh success persists rotated token before use; `invalid_grant`, `invalid_client`, 5xx, non-JSON classified; absent `refresh_token` keeps old; expiry margin; lock re-read; crash after refresh before write | table + fake HTTP | `uv run pytest -q tests/test_guard_auth.py tests/test_guard_credentials.py -x` | ❌ Wave 0 |
| SRC-01 | `auth` flow with fake HTTP and prompt: state/iss mismatch rejected, scope with `audits:run` refused, missing refresh token fails, file mode 0600, nothing secret printed or logged | unit | `uv run pytest -q tests/test_auth_command.py -x` | ❌ Wave 0 |
| SRC-02 | Sanitizer allowlist on synthetic "dirty" payloads (injected names, emails, secrets, paths, slugs); round-trip parse invariant; taint-set check; written fixtures replay through `FixtureGuardTools` | unit | `uv run pytest -q tests/test_probe_sanitize.py -x` | ❌ Wave 0 |
| SRC-02 | Recorded real fixtures parse and contain no leftovers | contract | `uv run pytest -q tests/test_recorded_fixtures.py -x` | ❌ Wave 0 (skip until recorded; hard assert at the gate) |
| RULE-02 | Age, aged flag at boundary (threshold-1 s, equal, +1 s), labelled "seen by radar since" | table | `uv run pytest -q tests/test_high_history.py -x` | ❌ Wave 0 |
| RULE-03 | Rise detection; counts-only text (no title/path/id); security and public-api types | table + golden text | `uv run pytest -q tests/test_high_rules.py tests/test_messages.py -x` | ❌ Wave 0 |
| RULE-09 | M clean runs close; M-1 does not; flapping 0,H,0,H keeps one episode; counted by `task_id` | table + property (idempotence, order) | `uv run pytest -q tests/test_high_history.py -x` | ❌ Wave 0 |
| STATE-07 | Re-arm only above the peak; crash before ack retries; reset on close; v1→v2 migration | table | `uv run pytest -q tests/test_ledger_rearm.py tests/test_state_v2.py -x` | ❌ Wave 0 |
| ALERT-01 | Only open/rise/stale immediate; aged alone silent; first run baselines highs; unknown report keeps alert, no resolve | pipeline e2e on fakes | `uv run pytest -q tests/test_high_pipeline.py -x` | ❌ Wave 0 |
| OPS-02 | Health sequences: <K silent, =K one message, repeated failures no more, recovery exactly once, ack-gated marking, no ops chat, save on failed poll | table + property + pipeline | `uv run pytest -q tests/test_health.py tests/test_pipeline_ops.py -x` | ❌ Wave 0 |
| OPS-03 | `doctor` verdicts for each `getChatMember` status, `getMe` 401, migrated chat, Guard auth kinds, credential file mode | unit with `FakeTransport` | `uv run pytest -q tests/test_doctor.py -x` | ❌ Wave 0 |
| OPS-01 | Unit/cron files: `TimeoutStartSec` set, `Type=oneshot`, `Persistent=true`, `EnvironmentFile` outside the repo, no `flock` on `radar.lock`; `run` live wiring with fakes; poll-cost log line | static + e2e | `uv run pytest -q tests/test_deploy_files.py tests/test_live_wiring.py -x` | ❌ Wave 0 |
| (cross) | New shell modules registered; `urllib.parse` allowance only; no clock/file I/O in core | architecture | `uv run pytest -q tests/test_architecture.py` | ✅ edit |
| (cross) | `mcp` logger quiet; no token in any log line during refresh failure paths | log capture | `uv run pytest -q tests/test_secret_logging.py -x` | ❌ Wave 0 |

Live-only proofs (human, gated plan): real `auth`; first real `probe`; dry-run on real data reviewed by the operator; 7-day soak across an access-token expiry; failure drill (exactly one ops message, exactly one recovery message); `doctor` on the production host.

### Sampling Rate
- **Per task commit:** the quick command for the touched test file(s), then `make verify` before handing back the PR.
- **Per wave merge:** `make verify` (the factory gate).
- **Phase gate:** full suite green before `/gsd-verify-work`; the live checklist is a separate human-verified list.

### Wave 0 Gaps
- [ ] `tests/guard_fakes.py` additions: scripted multi-poll Guard (a sequence of response sets keyed by poll index) for High-history e2e; run-factory helper.
- [ ] Fixture scenario for High series (`fixtures/guard-high/` or in-test builders): open, rise, debounce, unknown report, late run.
- [ ] `tests/fake_oauth.py`: fake token endpoint and AS metadata (the public AS JSON above is safe to commit as a fixture), fake prompt, fake clock.
- [ ] `tests/test_architecture.py`: register new shell modules, allow `urllib.parse`.
- [ ] Dependency add (`mcp`, `httpx2`, pydantic floor) with the human-verify checkpoint; `.python-version` 3.12.

## Security Domain

> `security_enforcement` is enabled (ASVS level 1, block on high) [VERIFIED: .planning/config.json].

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes | OAuth 2.1 public client with PKCE S256, `state`, RFC 9207 `iss` check, read scopes only; no client secret exists (`token_endpoint_auth_methods_supported: ["none"]`) |
| V3 Session Management | yes | Access token short-lived, per-request bearer from `TokenSource`; refresh rotation persisted atomically; revoke endpoint exists for manual revocation |
| V4 Access Control | yes | Request `projects:read audits:read` only; refuse to store a grant containing `audits:run`; adapter tool allowlist (`GUARD_READ_TOOLS`) |
| V5 Input Validation | yes | pydantic strict models for credentials, state, config; Guard JSON through existing strict parsers; redirect URL parsed with explicit checks; AS metadata validated (https, issuer equality) |
| V6 Cryptography | yes | No hand-rolled crypto: `secrets`, `hashlib`, `compare_digest`; TLS default verification (urllib, httpx2 truststore); no JWT parsing |
| V7 Error Handling and Logging | yes | Fixed sanitized error texts, `RedactingFormatter`, `mcp` logger quieted, no `str(httpx2 error)` (can contain URLs), pasted URL never in exceptions |
| V8 Data Protection | yes | Credential file 0600 in a 0700 dir under git-ignored `state/`; group messages counts only (D-05); no raw evidence to Telegram or fixtures |
| V12/V13 Files and API | partly | Atomic writes, symlink-safe open for the credential path (`O_NOFOLLOW` is a hardening candidate), no path from Guard data used in file names |
| V14 Configuration | yes | Secrets from env/`.env` only; unit file `EnvironmentFile` 0600; dependency pinned by lockfile with human verification |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Authorization code interception or injection | Spoofing | PKCE S256, `state` compare, redirect URI exact, code used once and discarded |
| Mix-up attack (code from another AS) | Spoofing | Require `iss` equal to the pinned issuer (AS advertises RFC 9207 support) |
| Refresh token theft from disk | Information disclosure | 0600 file, 0700 dir, git-ignored, never in env or logs; revoke via the revocation endpoint |
| Refresh-token reuse after rotation (race) | Tampering/DoS | Credential lock, re-read under lock, persist before use, no `.bak` fallback |
| Token or URL in logs, tracebacks, argv, shell history | Information disclosure | `getpass` prompt, redaction formatter, sanitized `GuardCallError` texts, quiet `mcp`/`httpx2` loggers |
| Over-broad scope (`audits:run`) | Elevation of privilege | Constant read-scope string, post-grant scope check, tool allowlist, no `run_audit` code path |
| SSRF/open redirect via discovered endpoints | Tampering | `https` only, issuer equality, endpoints stored once at `auth`, SDK redirects limited to same origin |
| Prompt/markup injection from Guard names | Tampering | Existing HTML escaping and secret gate; counts-only security text |
| Raw finding data committed via `probe` | Information disclosure | Allowlist sanitizer, taint-set check, CI leak scan, human diff review |
| Supply chain (new dependency tree) | Tampering | Lockfile, one human-verify checkpoint, official docs as the name source |
| Hung call stalls scheduler | Denial of service | Per-call timeouts, run budget, `TimeoutStartSec` |

## Sources

### Primary (HIGH confidence)
- mcp 2.3.0 wheel source, read locally (`client/auth/oauth2.py`, `client/streamable_http.py`, `client/client.py`, `client/session.py`, `shared/_httpx_utils.py`) and httpx2 `_auth.py`; PyPI JSON for `mcp` and `httpx2` (`requires_dist`, release dates)
- Local spikes (scratchpad, mcp 2.3.0, Python 3.12.14): 401 surfacing and hook capture, refresh URL in a fresh process, sync bridge, MockTransport 401, network-block compatibility, `flock` wrapper behaviour
- Repo files read this session: `src/enji_guard_radar/*.py` (ports, guard, guard_report, ledger, state_store, pipeline, cli, settings, telegram, secret_guard, stale, config, model, fixture_source), `tests/test_architecture.py`, `tests/conftest.py`, `tests/guard_fakes.py`, `tests/telegram_fakes.py`, `pyproject.toml`, `Makefile`, `.planning/*`, `docs/guard-data-notes.md`, `.planning/research/{STACK,PITFALLS}.md`
- https://core.telegram.org/bots/api (getMe, getChatMember, getChat, ChatMember* types and fields, read locally from a downloaded copy)
- https://py.sdk.modelcontextprotocol.io/client/oauth-clients/, /client/transports/, /whats-new/

### Secondary (MEDIUM confidence)
- https://fleet.enji.ai/.well-known/oauth-authorization-server (raw JSON, public, unauthenticated GET via the fetch tool)
- https://guard.enji.ai/.well-known/oauth-protected-resource/mcp (summarized by the fetch tool; scopes also listed in HANDOVER.md)
- RFC 6749 §5.2, §6 and RFC 9700 §2.2.2 (fetched; section text partly summarized by the fetch tool)
- freedesktop.org systemd.service(5), systemd.timer(5), systemd.exec(5), systemd.unit(5) (fetched, summarized)

### Tertiary (LOW confidence)
- None used as authority. Nothing was learned from Guard beyond the two public discovery documents; no authenticated call, no Guard tool call, no token was touched.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH for versions and the SDK/token-layer decision (verified in source and spikes); MEDIUM for fit with the real Guard (A6, A8).
- Architecture: HIGH for pure cores (state v2, history, health) and the adapter boundary; MEDIUM for Guard-specific behaviour.
- Pitfalls: HIGH where marked verified (refresh URL, flock wrapper, oneshot timeout, 401 surfacing); MEDIUM for real-data risks (A12, A13).

## Recommended plan and wave split

Principle: build and verify everything that needs no live Guard now, keep new modules in separate files, and make each new CLI command its own module with a small registration in `cli.py` to avoid merge conflicts between parallel code-factory tasks.

| Plan | Wave | Content | Reqs | Needs live Guard |
|------|------|---------|------|------------------|
| 02-01 | 1 | State schema 2 (histories, health, `alerted_count`, 1→2 migration), config sections (`high`, `ops`, K, M), `GuardAuthError` and `cause` plumbing in `ports.py`/`guard.py`, `AccessTokenSource` protocol, dependency add behind the human-verify checkpoint, `.python-version`, architecture-test updates | foundation for all | no |
| 02-02 | 2 | High history fold and rules, `reconcile` re-arm and `apply_delivery(alerted_count)`, High alerts and counts-only wording, report-skip for known `task_id`, pipeline wiring, dry-run lines, scripted multi-poll fakes and fixtures | RULE-02, RULE-03, RULE-09, STATE-07, ALERT-01 | no |
| 02-03 | 2 | `guard_auth` core + credential store + refresh/classify + `auth` command (fake HTTP, fake prompt) | SRC-01 (token side), D-03/D-04 | no |
| 02-04 | 2 | `mcp_guard` adapter: sync bridge, per-request bearer, hook classification, allowlist, result extraction, `mcp` logger quiet; tests with in-process `MCPServer` and `MockTransport` | SRC-01 (transport) | no |
| 02-05 | 3 | `advance_health` + ops messages + pipeline save-on-failure + `doctor` (Telegram generic method, membership verdicts, credential and Guard checks through ports) | OPS-02, OPS-03 | no (Guard check on fakes) |
| 02-06 | 3 | `probe`, recording decorator, allowlist sanitizer, taint check, leak scanner, recorded-fixture contract tests | SRC-02 (tooling) | no (needs sample dirty payloads, synthetic) |
| 02-07 | 4 | Live `run` wiring (no `--fixtures`), counting wrapper and poll-cost log, deploy files (systemd, cron example), runbook in `docs/`, static deploy tests | OPS-01 | no |
| 02-08 | 5 (gated, human) | Operator creates the login with `auth`; `tools/list` and first `probe`, sanitize review and commit of `fixtures/guard-real/`, parser fixes from mismatches (Pitfalls 8, 9), poll-cost measurement, dry-run on real data reviewed by the operator, timer enabled, 7-day soak, failure drill, `doctor` on the production host, update `docs/guard-data-notes.md` Auth section | SRC-01, SRC-02, OPS-01..03 acceptance | **yes** |

Dependencies: 02-02 and 02-05 both touch `pipeline.py` and `ledger.py`; they are sequenced (02-02 wave 2, 02-05 wave 3), and 02-01 lands the shared schema first. 02-03 and 02-04 touch disjoint new files; 02-04 depends only on the `AccessTokenSource` protocol from 02-01. 02-06 needs the recording decorator over the adapter from 02-04. Plans 02-01 to 02-07 are fully verifiable by `make verify` with no live Guard and can be queued now; 02-08 cannot.

**Research date:** 2026-11-17
**Valid until:** 2026-12-01 for SDK and dependency versions (fast-moving 2.x line, httpx2 releasing weekly); 30 days for RFC, Telegram and systemd facts.
