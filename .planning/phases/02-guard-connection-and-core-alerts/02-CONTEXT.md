# Phase 2: Guard Connection and Core Alerts - Context

**Gathered:** 2026-11-17
**Status:** Ready for planning

<domain>
## Phase Boundary

The radar reads real Guard data on its own read-only login and, on a timer, alerts the group about new or worsening High findings and newly stale projects. The operator hears about it in a separate ops chat when the radar itself stops working or recovers. Requirements: SRC-01, SRC-02, RULE-02, RULE-03, RULE-09, STATE-07, ALERT-01, OPS-01, OPS-02, OPS-03.

**Gate:** the radar's own read-only Guard login (`projects:read`, `audits:read`, never `audits:run`) does not exist yet. Until it does, only parts that need no live Guard can be built and verified (fixture-based High-count rules, state, ops-chat logic, `doctor` Telegram checks). Real-data work (SRC-01, SRC-02 probe, poll cost, token refresh) waits on the login.

</domain>

<decisions>
## Implementation Decisions

### High-count history (RULE-02, RULE-09)
- **D-01:** The radar accumulates history in its own state. Each poll records the High count and the Guard `task_id` per `(repo, audit type)`. It does not read older Guard reports to reconstruct history. Age ("days since the first High run") and the M-clean-runs clear rule are computed from this accumulated history, counted by Guard `task_id`, not by radar polls. — **Reversibility:** costly — the state schema (schema 1 already has an optional `ConditionRecord.count`) and every rule that reads history depend on it; changing to backfill later needs a state migration.
- **D-02:** Consequence accepted: age starts at the first poll, so a long-standing High finding shows as young. Keep that visible in the alert wording or docs rather than hiding it. Backfill from older reports is a possible later improvement (see Deferred).

### Radar login failure
- **D-03:** If the radar's Guard token cannot be refreshed unattended (expired, revoked, dead refresh token), treat it like any failed Guard fetch: the poll is "unknown" and never creates or resolves findings. After K consecutive failed polls the ops chat gets one message saying the login needs re-auth (naming the `auth` command, never a token or code). One recovery message follows after re-auth (OPS-02).
- **D-04:** The radar requests read scopes only. It has its own login, separate from Claude Code's, created once by the operator. OAuth codes, tokens and callback URLs are never pasted into chat or logs.

### Group visibility of security findings
- **D-05:** Every member of the executive group may see security-finding summaries, counts only: repo, audit type, High count (e.g. "High rose from 1 to 3 in audit.security"), age. Group messages never carry finding titles, file paths, secrets or raw evidence (RULE-03). Existing secret gate and escaping from Phase 1 still apply.

### Claude's Discretion
- `probe` design and sanitization rules for real responses written to `fixtures/` (placeholders for names, emails, secrets, code snippets, finding evidence).
- Timer mechanism (systemd or cron), where it runs, the values of K (failures before ops message) and M (clean runs to clear), and how poll cost is measured and logged. These depend on the deployment host and real Guard data; defaults live in validated config, thresholds are finalised from real data.
- Exact `auth` command UX and where the refresh token is stored (`.env` or `state/`, never in git).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project scope and plan
- `.planning/ROADMAP.md` §Phase 2 and §Gates and Open Items — goal, success criteria, open questions
- `.planning/REQUIREMENTS.md` — SRC-01, SRC-02, RULE-02, RULE-03, RULE-09, STATE-07, ALERT-01, OPS-01..03
- `.planning/PROJECT.md` — core value, constraints
- `.planning/STATE.md` — carried-forward open points from Phase 1
- `.planning/HANDOVER.md` — Guard MCP and OAuth facts, pitfalls

### Guard data
- `docs/guard-data-notes.md` — what Guard really returns, pagination, poll cost, Auth (read before touching the parser or rules)
- `fixtures/guard/` — synthetic fixtures in the real shape
- `.planning/research/PITFALLS.md` — unknown-never-creates-findings, token in URL, no raw evidence

### Design and standing rules
- `docs/design/v0.md` — design
- `AGENTS.md` — standing rules (rules decide, secrets from env, intent-named I/O boundaries)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/enji_guard_radar/ports.py`, `guard.py`, `fixture_source.py`: Guard read boundary and fixture source; a real MCP-backed source slots in behind the same port.
- `guard_report.py`: Markdown report parser, "unknown" on failed, empty or partial fetch.
- `ledger.py`, `state_store.py`: dedup, baseline, retry, freeze; atomic state with backup, corrupt halt, lock; optional `count` in schema 1.
- `telegram.py`, `messages.py`, `secret_guard.py`, `settings.py`: sender with error classes, escaping/collapse/split, secret gate, env/.env loading (already has `TELEGRAM_OPS_CHAT_ID`).
- `cli.py`, `pipeline.py`: `run` with `--dry-run`, `--fixtures`, `--state-dir`, `--now`.

### Established Patterns
- Rules, dedup and message formatting are pure functions with table tests; I/O sits behind small intent-named boundaries and is faked in tests.
- Alert recorded as sent only after Telegram acknowledges; first run baselines.

### Integration Points
- New real Guard source behind the existing port; new `probe`, `auth`, `doctor` subcommands in `cli.py`; High-count rules feed `ledger.py`; ops-chat messages reuse the Telegram sender.

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches. The operator reviews real-data dry-run output before the executive group is enabled (success criterion 2).

</specifics>

<deferred>
## Deferred Ideas

- Backfill High-count history from older Guard reports on first sight of a `(repo, audit type)` for accurate age from day one (revisit after real data shows how often age matters).
- Phase 1 open points carried forward, not closed here: `>=` N days vs "older than" wording, zero-project listing treated as "Guard unavailable", never-ran immediate alert vs ALERT-01, STATE-01 count part.

</deferred>

---

*Phase: 2-Guard Connection and Core Alerts*
*Context gathered: 2026-11-17*
