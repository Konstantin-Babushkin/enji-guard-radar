# Project Research Summary

**Project:** enji-guard-radar
**Domain:** Scheduled project-health monitor and alerter (poll Enji Guard MCP, apply pure rules, Claude triage, Telegram alerts for executives, file state, no database)
**Researched:** 2026-11-16
**Confidence:** MEDIUM (stack, Telegram and Claude mechanics are HIGH. Everything that depends on Guard's real data shape is unverified.)

## Executive Summary

The radar is a small one-shot batch pipeline run by an external timer. Experts build this kind of system as a functional core with I/O at the edges. The shell holds Guard, Claude, Telegram, state and a clock behind small `Protocol` ports. The core holds pure functions: detect, reconcile (ledger diff), plan, render, apply_delivery. The nearest analogues are alert routers such as Alertmanager, with scanner-style aging (Snyk, Dependabot) and heartbeat-style staleness (Healthchecks, Cronitor). The difference is that the audience is an executive Telegram group. That audience tolerates almost no noise, so one bad week of false or repeated alerts can end the project.

Recommended approach:
- **Pipeline:** a synchronous Python 3.12 CLI. The official `mcp` 2.x client (async, wrapped in one `asyncio.run`) polls Guard deterministically, with no LLM in the fetch path. Frozen Pydantic models cover snapshot, finding and state.
- **Decisions:** rules and the planner decide what is sent. The dedup ledger doubles as the outbox, so `alerted` is written only after Telegram acknowledges (at-least-once delivery).
- **Claude:** it only explains. It runs through the `anthropic` SDK (`messages.parse` with a Pydantic schema) behind a `Triager` port, and only for messages that will actually be sent. Any failure falls back to the rule-only text.
- **Telegram:** `httpx2` calls `sendMessage` directly in HTML parse mode.
- **Scheduling:** a systemd oneshot timer, with `flock` inside the program.

The main risks, in order:
1. **Guard's data shape is unknown.** Tool names, auth, stable finding ids, timestamps and metric ranges have never been seen. Rules written against guessed data will not fit, so real Guard access is a hard gate before any Guard-dependent rule.
2. **False alarms from missing data, first-run floods and unstable dedup keys.** The mitigations are three-valued snapshots (Ok, Failed, Unknown), baseline mode, and keys built only from stable identity.
3. **The radar dying silently**, which is the weakness of "silence means healthy". The weekly digest must always send (a one-line all-clear), and operational failures go to an ops channel, not the executive group.
4. **Secret leakage.** The Telegram token sits in the request URL, and raw security-finding evidence must never reach Claude, Telegram or state.

## Key Findings

### Recommended Stack

See `.planning/research/STACK.md`. Pin Python 3.12 so dev and prod match. The runtime has 6 direct dependencies. Use one HTTP stack, `httpx2`, because `mcp` 2.x and `anthropic` 2.x both depend on it. Do not add plain `httpx`, `pytest-httpx` or `respx`.

**Core technologies:**
- `mcp` `>=2.3,<3`: official client for Guard tool calls. Check `is_error`, unwrap `ExceptionGroup`/`MCPError` into one intent-named failure. Fall back to `mcp` 1.x only if the connection spike shows 2.x cannot talk to Guard.
- `anthropic` `>=2.8,<3`: `messages.parse(output_format=Model)` for schema-validated triage (default model `claude-sonnet-5-5`, config-overridable). No tools, no subprocess, easy to fake.
- `httpx2` `>=2.16`: Telegram `sendMessage` with `parse_mode="HTML"` and `html.escape` on every dynamic string. Use `httpx2.MockTransport` in tests. Never call `raise_for_status()`, because the exception text contains the token URL.
- `pydantic` `>=2.12` and `pydantic-settings` `>=2.16`: one model system for snapshot, state, Claude schema and config. Secrets are `SecretStr` from env only.
- `pyyaml` (`safe_load`): human-edited config with per-project thresholds. JSON for machine-written state.
- systemd timer with `Type=oneshot` and `Persistent=true` (cron plus `flock` as fallback). `fcntl.flock` single-instance lock inside the program. Stdlib atomic write (tmp, fsync, `os.replace`, fsync dir) with a `.bak`.

**Rejected:**
- `python-telegram-bot`
- MarkdownV2
- `claude-agent-sdk`
- APScheduler or a sleep loop
- `tenacity`, `structlog`, `filelock`, `freezegun`, `pytest-asyncio`

**Conflict between files to resolve (decided here):**
- ARCHITECTURE.md and PITFALLS.md assume Triager is `claude -p --bare`. STACK.md recommends the Anthropic API SDK.
- **Go with the SDK**: it removes the CLI traps in Pitfall 12 (ambient config, OAuth under cron, envelope parsing, version drift) and needs only `ANTHROPIC_API_KEY`.
- Keep the `Triager` port so a `claude -p` adapter stays a one-file swap if the operator wants subscription billing. That is an operator question.

### Expected Features

See `.planning/research/FEATURES.md`.

**Must have (table stakes):**
- Stable dedup key (project, rule, subject) with persisted state, and a new-vs-open distinction in every alert.
- Immediate alerts only for new high-risk findings and newly stale projects. Everything else is digest-only, decided by one table-testable routing function.
- Age on every item, using Guard's own first-detected date when available.
- Reminder at a fixed interval measured in days, with a single "overdue" tier.
- Weekly digest: always sent, worst first, capped with "+N more", under 4096 characters. It doubles as the heartbeat.
- Stale rule: fixed per-project N with a default and overrides. It distinguishes "never ran", "Guard unreachable" and "stale".
- Per-project config with a strict model (`extra="forbid"`).
- Rule-only message template as the baseline. Claude never gates sending.
- Radar self-health: K consecutive poll failures produce one ops message, and a recovery note when it clears.
- Secret redaction at the log and Telegram boundaries.
- Message format: 3 to 4 lines, plain language, severity as a word, evidence line included.

**Should have (differentiators):**
- Recurring-finding detection. Define it as re-opened after being fixed (`episodes >= N`), counted per Guard run id and not per poll. This is a stated requirement, so keep it in v1.
- Debounced resolution (hysteresis, N clean runs). Build it together with recurring detection and resolved notes.
- Worsening detection: severity is on the record and not in the key, so an increase re-arms the alert.
- Time-boxed silence file with a mandatory expiry and reason.
- Digest trend delta ("3 new, 2 resolved").
- Claude one-line "why it matters" and priority. Priority only orders items.
- Inhibition: a stale project suppresses its other rule alerts.
- Dry-run mode (should be built early, not late).
- Quiet hours.

**Defer (v2+):**
- Interactive ack through Telegram, which needs a long-running process (log in `docs/ideas/`).
- More than two escalation tiers.
- Composite health scores, dashboards, per-developer metrics, auto-remediation.
- Anomaly-based staleness.
- SQLite.

### Architecture Approach

See `.planning/research/ARCHITECTURE.md`. The run is `lock, load state, fetch, detect, reconcile, save (idempotent), plan, triage (optional), render, send each message, apply_delivery, save`.

Two changes to `docs/design/v0.md`:
- **Dedup before triage.** Claude is called only for messages that will actually be sent.
- **The ledger is the outbox.** `alerted` is written only after an acknowledged send, with one commit unit per outbound message and disjoint key sets per chunk.

**Major components:**
1. `ports.py` and `adapters/` (`GuardMcpSource`, `FixtureGuardSource`, `Triager`, `TelegramNotifier`, `FileStateStore`, `PrintNotifier`) — all I/O, with an import-direction test so the core never imports adapters.
2. `model.py` — frozen snapshot, finding and state models. Per-project fetch is `Ok | Failed`, metrics are `None` when unknown, all datetimes are tz-aware UTC, and `now` is injected.
3. `rules/` — one file per rule plus a registry. Signature is `(ProjectSnapshot, HistoryView, Thresholds, now) -> list[Finding]`.
4. `ledger.reconcile` — classifies NEW, PERSISTING, ESCALATED, REOPENED and RESOLVED. It freezes projects whose fetch failed. `runs_seen` stores Guard run ids.
5. `planner` and `render` — dedup, reminders, digest due, chunking at item boundaries, escaping, redaction, worst-first order.
6. `triage` — validates against the request (keys subset, no duplicates, no URLs or mentions, digits must come from evidence). Fallback is per item.
7. `pipeline.run_once` and `cli` (`run`, `--dry-run`, `--fixtures`, `probe`, `doctor`, `digest --force`). Dry-run works by swapping adapters, not by an `if`.

### Critical Pitfalls

Full list in `.planning/research/PITFALLS.md` (34 entries).

1. **Rules built on guessed Guard data (P1).** Make real Guard discovery a hard gate. Ship `probe` to record sanitized real payloads as fixtures. Every fake is a mutated real fixture, and a contract test parses every fixture.
2. **Absence of data treated as absence of the problem (P2).** A failed or empty fetch must never resolve findings or fire stale alerts. Freeze failed projects, treat "zero projects" as an error, and keep resolution on positive evidence only.
3. **First-run flood and "state reset equals first run" (P3).** Use baseline mode per rule and per project. Never auto-reinitialise corrupt state: try `.bak`, then halt and notify the operator. Add a flood cap (collapse to one summary above about 5 immediates) and default to dry-run.
4. **Silent dead radar (P5).** Always send the weekly digest, with last successful poll time. Send one ops alert after K failed polls to a separate `TELEGRAM_OPS_CHAT_ID`. Exit non-zero on failure, and add a `doctor` preflight (`getMe`, `getChat`, `getChatMember`, Guard reachable).
5. **Unstable dedup identity (P4).** Keys contain identity only: no severity, counts, line numbers, titles or ages. Use Guard's stable id, or a versioned fingerprint. Age prefers Guard's own date, and our own first-seen must be labelled.
6. **Secret leakage (P15).**
   - The Telegram token is in the URL, so set the `httpx2` and `httpcore2` loggers to WARNING and add a redaction filter.
   - Never forward raw security-finding evidence to Claude, Telegram or state.
   - Run a final secret-pattern gate before every send.
7. **Alert loss and duplication around the send (P20).** Send first, then persist only the keys in an acknowledged message. Classify Telegram errors in one table (parse 400 falls back to plain text, `migrate_to_chat_id`, 429 with bounded `retry_after`, 401/403 go to ops).
8. **Hallucinated facts and prompt injection.** Facts such as counts, ages and names come from code. Claude supplies only `summary`, `probable_cause` and `priority`, and cannot add or drop alerts.

## Implications for Roadmap

Ordering rule: Guard access does not exist yet, so build everything that does not need it first against fakes. Isolate the real adapter as one gated phase. Do durable state and Telegram before the Guard adapter, because they are the highest-risk design and do not need Guard.

### Phase 1: Walking Skeleton (dry-run)
**Rationale:** it fixes the seams every later PR must respect. It needs no Guard or Telegram access.
**Delivers:** `model.py`, `ports.py`, fakes, `FixtureGuardSource`, the stale-runs rule only (needs just a last-run timestamp), `render` with the rule-only template, `PrintNotifier`, CLI `run --dry-run --fixtures`, config validation and the import-direction test.
**Addresses:** stale rule, per-project overrides, rule-only message, dry-run.
**Avoids:** hidden clock (inject `now`, enable ruff `DTZ`), hard-coded thresholds, unvalidated config, naive datetimes.
**Parallel, non-code:** the operator supplies Guard MCP access (URL or command, auth), the Telegram token, a private test chat and the ops chat.

### Phase 2: Durable State, Dedup, Real Telegram
**Rationale:** crash safety and no double or lost alerts are the highest-risk design. Build the ledger now, because retrofitting `first_seen` and `episodes` after alerts are live loses history.
**Delivers:**
- `FileStateStore` (atomic write, `.bak`, `flock`, `schema_version`, `Missing | Corrupt | Loaded`).
- `ledger.reconcile`, `planner`, `apply_delivery`, baseline mode, flood cap.
- `TelegramNotifier` (HTML, chunking, error-class table, 429 and migrate handling, preflight) and the redaction and logging setup.
- Crash-injection tests and a validating fake Telegram.
**Uses:** `httpx2`, `pydantic-settings`, stdlib.
**Avoids:** pitfalls 3, 15, 16, 17, 18, 19, 20, 21 (lock) and 22.
**Research flag:** standard patterns. One manual test with hostile strings in a private chat is enough.

### Phase 3: Guard Discovery and Real Adapter (gated)
**Rationale:** the highest uncertainty, and it blocks every other rule. It cannot start without operator-provided Guard access.
**Delivers:** a connection spike and `probe` that record `protocol_version`, `list_tools()` and sample payloads as sanitized fixtures. `GuardMcpSource` plus a normalizer (strict parse, tz-aware UTC, unknown enums map to the most severe class and are logged). `isError` and pagination completeness checks, per-project failure isolation and timeouts. The radar-health message to the ops channel, `.env` loading, systemd units, exit codes and the first real scheduled dry-run.
**Avoids:** pitfalls 1, 2, 9, 23 and 31.
**Research flag:** NEEDS DEEPER RESEARCH. Resolve these here:
- Auth and non-interactive credentials.
- Stable finding id.
- Run statuses and what counts as a run.
- Timestamp format.
- Metric names and ranges.
- Pagination.
- How `GUARD.md` is checked.

Fallback to `mcp` 1.x if 2.x fails to negotiate.

### Phase 4: Remaining Rules (highest value first)
**Rationale:** each rule is an independent slice and can be a parallel task. Definitions and thresholds must come from the real data distribution, not from guesses.
**Delivers (in order):**
1. Unaddressed high-risk with age (immediate on new).
2. Security, new and worsening, with minimal disclosure and grouped advisories.
3. Harness and `GUARD.md`.
4. Analysis tooling (dead-code, duplicate, security and dependency scanners present), digest-only.
5. AI readiness.
6. Tests.
7. Recurring findings, using debounced resolution and `episodes`.

Hysteresis and cooldown are applied to metric rules.
**Avoids:** pitfalls 4, 7, 8, 25 and 26.
**Research flag:** NEEDS PER-RULE RESEARCH against real data (threshold calibration, the meaning of "recurring", and operator confirmation of the run definition).

### Phase 5: Digest, Reminders, Resolved Notes, Quiet Hours
**Rationale:** it needs the ledger (Phase 2) and content worth summarizing (Phase 4).
**Delivers:** the weekly digest, always sent (one-line all-clear), worst first, age, "+N more", under 4096 characters, with the last successful poll time, the stale and silenced sections and the trend delta. Reminder interval in days, resolved notes folded into the digest, the silence file with expiry, `last_digest_week` marker, and quiet hours and digest timezone via `zoneinfo`.
**Avoids:** pitfalls 5 (heartbeat), 10, 17, 21 (digest marker) and 29.
**Research flag:** standard patterns, no research.

### Phase 6: Claude Triage
**Rationale:** a pure addition. The rule-only message is already the shipped product, so triage can never regress decisions, only text.
**Delivers:** the `Triager` port and the `anthropic` `messages.parse` adapter (no tools, timeout, `max_tokens` headroom). Output validation covers key subset, no duplicates, no URLs or mentions, and digits drawn from evidence. Fallback per item. Input is scrubbed of secrets and untrusted text is delimited. Cost is logged with a daily cap, and results are cached by key plus evidence hash.
**Avoids:** pitfalls 11, 12, 13 and 14.
**Research flag:** LIGHT research. The same call can be tested with a short spike (`refusal` and `max_tokens` handling, schema compile latency). Confirm API-key versus subscription billing with the operator.
**Note:** it can run in parallel with Phases 4 and 5 once Phase 2 exists.

### Phase Ordering Rationale
- Phase 1 comes first because it fixes the architecture's seams. Phase 2 comes before Phase 3 because state and delivery are the riskiest design and do not need Guard.
- Phase 3 is the only phase blocked on the operator. Start the non-code Guard access request on day one so it does not gate the schedule.
- Claude triage is last: it is optional for value, and rules decide, so it cannot change alert decisions.
- Cross-cutting items go into the earliest phase that touches them, not at the end. These are baseline mode, redaction, the flood cap, dry-run, idempotence tests ("second identical run sends nothing") and the replay-simulation test.
- The first live send happens only after dry-run output has been reviewed on real data, with `RADAR_ENV=prod` required for the executive chat.

### Research Flags

Needs deeper research during planning:
- **Phase 3:** Guard MCP discovery (the biggest unknown).
- **Phase 4:** per-rule definitions and thresholds from real data.
- **Phase 6:** light spike (structured-output edge cases, billing choice).

Standard patterns (skip research-phase):
- **Phase 1:** ports, models and fakes.
- **Phase 2:** atomic state, outbox and Telegram sender (read the Telegram docs only).
- **Phase 5:** digest and scheduling.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH (MEDIUM for the MCP client fit) | Verified against official docs and local probes (PyPI metadata, mcp 2.3.0, anthropic 2.8.0). Guard's transport, auth and protocol version are unknown, and the fake-401 and in-process probes do not prove compatibility with the real server. |
| Features | MEDIUM | Alert mechanics (dedup, repeat, resolved, silence) are well documented. Executive-digest conventions and the recurrence definition are synthesis, and there is no reference for reopened-finding logic. Nothing is validated against real Guard data. |
| Architecture | MEDIUM | Pipeline, outbox and crash-matrix patterns are established but not tied to a single source. Telegram facts are HIGH. Every Guard-facing field is an `ASSUMPTION`. |
| Pitfalls | MEDIUM-HIGH | Telegram and Claude behavior was checked against official docs. Monitoring and dedup pitfalls are established practice applied to this design. |

**Overall confidence:** MEDIUM. The engineering approach is solid. The product-specific rules are not yet grounded in data.

### Gaps to Address

- **Guard shape and access:** unknown tool names, auth, stable ids, timestamps, run statuses, metric names and scales. Handle it in Phase 3 with the `probe` and fixtures. Rules for a requirement wait until its input has been seen in real data.
- **Claude billing and mechanism:** the research files disagree (API SDK versus `claude -p`). This summary picks the API SDK. Confirm with the operator. `--max-turns` and the error envelope for `claude -p` are unverified and matter only if the CLI path is chosen.
- **At-least-once versus at-most-once:** recommended default is at-least-once, with one narrow duplicate window. Confirm in requirements.
- **Recurring-finding definition:** the recommendation is re-opened after a fix, counted per Guard run. The operator must confirm it and set N and the window.
- **Operator questions:**
  - ops or degraded-radar channel (`TELEGRAM_OPS_CHAT_ID`)
  - digest day and timezone, and quiet hours
  - which run statuses count as a run
  - dormant or archived projects (exclusion and a `paused` flag)
  - whether the whole group may see security finding summaries
  - whether silences are checked into git (recommended) or kept local
- **Deployment host:** systemd is assumed. The dev box has none, so the unit files are untested. If the host has no systemd, use cron plus `flock` and add the heartbeat and failure notification in code.
- **Threshold values and cadences:** reminder interval, overdue tier and flood cap are starting guesses. Expose them as config and tune them in dry-run on real data.
- **Claude Code CLI path (if chosen):** subscription use for unattended jobs may conflict with Anthropic policy for third-party use. Flag as `ASSUMPTION:` and ask.

## Sources

### Primary (HIGH confidence)
- py.sdk.modelcontextprotocol.io — MCP Python SDK v2 client API, transports, OAuth, error semantics.
- platform.claude.com docs (structured outputs, models) — `messages.parse`, `output_config.format`, schema limits, refusal and `max_tokens` caveats, model IDs.
- code.claude.com docs (headless, authentication, CLI reference) — `claude -p` flags, `--bare`, `structured_output`, auth precedence.
- core.telegram.org/bots/api and /bots/faq — `sendMessage` limits, HTML versus MarkdownV2 rules, `retry_after`, `migrate_to_chat_id`, rate limits.
- systemd.timer(5) — `OnCalendar`, `Persistent`, overlap behavior.
- PyPI JSON API and local probes (2026-11-16) — dependency versions, 401 handling, the token in the `httpx2` INFO log.
- Prometheus Alertmanager, Snyk, SonarQube, Nagios and Datadog docs — dedup, repeat, silence and flap patterns.

### Secondary (MEDIUM confidence)
- pydantic.dev httpx2 migration docs.
- Alertmanager governance write-ups; heartbeat monitoring vendor sources (Cronitor, Healthchecks).
- UK Ministry of Justice GitHub alert SLA standard (day counts not verified).
- Functional-core, outbox and atomic-write patterns (established practice, not a single source).

### Tertiary (LOW confidence)
- Kestra Dependabot SLA blueprint and the Dependabot digest agent template — digest conventions.
- LinearB, Swarmia and Jellyfish comparison blogs — framing only, no requirements derived.

Detail files: `.planning/research/STACK.md`, `FEATURES.md`, `ARCHITECTURE.md`, `PITFALLS.md`.

---
*Research completed: 2026-11-16*
*Ready for roadmap: yes*
