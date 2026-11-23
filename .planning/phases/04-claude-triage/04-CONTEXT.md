# Phase 4: Claude Triage and Short Templates - Context

**Gathered:** 2026-11-19 (revised the same day with the operator's answers, three passes)
**Status:** Ready for planning once T-0033 (`messages.language` and the `texts.py` catalog) is merged. The mechanism that reaches Claude is still unverified (D-02): verifying it is the first piece of Phase 4 work and needs the operator on the radar host. Triage ships off (D-03).

<domain>
## Phase Boundary

Two changes to what the group reads, in this order:

1. **Short templates (ALERT-07).** Alerts, reminders and the weekly digest shrink to one line per item with a colour marker, the project, what is wrong and the age, in both message languages. This lands first, before any Claude line exists.
2. **Triage (TRI-01..TRI-05).** A **fresh High alert** (the first alert of a new High series, whose report was read in this run) can carry one extra line from Claude: a priority and one short cause, `P1 · <cause>`, written in the configured message language. The set of alerts is exactly what the rules alone would have sent. Claude never adds, drops, reorders or suppresses anything, and every count, age and name comes from code.

In scope: ALERT-07 (new), TRI-01 (amended), TRI-02, TRI-03, TRI-04, TRI-05; the parser keeping finding titles in memory (D-06); the new templates written into the T-0033 catalog in English and Russian.
Out of scope: Claude deciding whether to alert; the Anthropic API SDK and the Agent SDK (operator chose CLI or T3, no API key); evidence, paths, report bodies and links going to Claude or the group; any persisted finding title (state stays **schema 3**); a Claude line on re-armed (rise) alerts, retried alerts, reminders, digest items, stale, never-ran, low-score, tooling-gap, resolved or ops lines; changing rules, thresholds, tiers or the digest schedule.

**Depends on T-0033** (adds `messages.language`, `en|ru`, and a `texts.py` message catalog). It is not in the repo yet: Phase 4 plans start after it is merged and read its real key and catalog shape; anything here that names them is an **ASSUMPTION** until then. Phase 4 also depends on Phase 2 (alerts) and Phase 3 (digest, reminders); both are merged. Everything can be built against fakes; the live mechanism check (D-02) needs network and the operator's Claude token.
</domain>

<decisions>
## Implementation Decisions

### Short templates (ALERT-07)
- **D-04:** Alerts, reminders and the digest use one line per item. Target shapes (the operator's, 2026-11-19):

  ```
  🔴 Project A · security High 0→1 · 8d
     P1 · SQL injection in order search
  🟡 Project B · no scan 28d
  ```

  ```
  Radar weekly · 5 open
  Last poll: 2026-11-19 05:00 UTC
  🔴 Project A · security High 2 · 8d · recurring 3 runs
  🟡 Project B · no scan 28d
  🟡 Project C · repo-x · no runs 60d: dead-code, security
  ```

  ```
  Radar reminder · 2 open
  🔴 Project A · security High 3 · 15d
  🔴 Project D · tests High 1 · 9d
  ```

  Rules, all code-owned:
  - **Texts live in the T-0033 catalog (`texts.py`) in both languages (en and ru), selected by `messages.language`.** Every fixed word and template of the shapes above (title, "no scan", "no runs", "recurring", the age unit, the footer, the `P1 · ` form, resolved and note lines) is a catalog entry with an English and a Russian text; the Russian wording is Claude's Discretion. Every new text test runs for both languages. Dynamic text still passes through `messages.escape`.
  - Marker: 🔴 for a High-findings item, 🟡 for every other kind; one marker per line, never chosen by Claude. The "Severity: ..." line and the "Seen by the radar since ..." sentence are dropped from the text.
  - High line: project name (bold, cleaned, escaped), audit type without the `audit.` prefix, `High <previous>→<count>` for a rise or a new series (a new series starts from 0), `High <count>` otherwise, then the age as `Nd`. Age is still counted from the first High run the radar saw (Phase 2 D-02); because the line no longer says so, the digest carries one footer line "Ages count from the radar's first sight." whenever it lists a High item, and the runbook notes it. The "Long-standing:" prefix is dropped. A project with two repos that both alert on the same audit type must stay distinguishable (repo name added when needed; exact form is Claude's Discretion).
  - Stale: `no scan Nd`; never ran: `no scan yet`. A collapsed summary (more than `alerts.collapse_above` alerts) uses a short header (`Radar · N need attention`) and the same one-line items; the collapse decision is unchanged.
  - Digest: title `Radar weekly · N open` where N is the number of open item lines; the last successful poll time stays (DIG-03); a digest with nothing open is one line with "nothing open" and the poll time (DIG-02); `+N more` after `digest.max_items` lines stays (DIG-01); resolved items and tooling gaps stay (DIG-04) in compact form. **Tooling gaps are grouped per repo into one line** (`no runs 60d: dead-code, security, ...`, the number being `tooling_gaps.window_days`, names without the `audit.` prefix); one repo is one item for N and for the cap, so the cap counts lines, not (repo, audit type) pairs. Tier order, order inside a tier, the recurring flag (on the High line) and the "projects could not be read" note keep their meaning.
  - **Reminders (Phase 3 D-11) are shortened the same way** (operator, 2026-11-19): a title `Radar reminder · N open` and one line per series with the same High line shape. Interval, clock, one message per run, ack-before-mark and the separate-message rule are unchanged. A reminder carries no Claude line.
  - Unchanged: escaping through `messages.escape` (one point), splitting at item boundaries under the Telegram limit, the secret gate, ack-before-mark, counts only in rule text (no titles, ids, paths or evidence). Emoji count as 2 UTF-16 units in `telegram_length`.
  - The existing text tests of Phases 1..3 pin the old wording and are rewritten against the catalog; the templates ship in their own plans before triage touches any text.
  — **Reversibility:** reversible — catalog entries and pure render functions.

### How Claude is reached (TRI-05)
- **D-01:** One port, `Triager`, in `ports.py`: `triage(request: TriageRequest) -> TriageOutcome`. It never raises: every failure comes back as a typed "no triage" outcome with a reason (D-11). Adapters are shell modules behind it: `claude -p` (`claude_cli_triager`), a T3 route only if D-02 shows a documented non-interactive entry point, and a fake used by all tests. The adapter is picked by `triage.backend` in config; swapping it is a one-file change plus one config value. Pure code (request building, sanitising, schema, validation, rendering) never imports an adapter (the architecture test applies). **No API key and no new dependency:** no `anthropic` SDK, no `claude-agent-sdk`; the adapter uses the standard library `subprocess`. — **Reversibility:** reversible.
- **D-02:** The mechanism is **unverified** and verifying it is the **first piece of Phase 4 work** (plan 04-01, operator-gated like 02-11; nothing here assumes it works). Decided with the operator on 2026-11-19: unattended use of a Claude subscription login by the scheduled job is allowed; the login is the operator's own account; its token comes from `claude setup-token` and lives in `.env` (mode 0600) on the radar host. 04-01 still has to show, on that host, and record in `docs/triage-notes.md` (OBSERVED / ASSUMPTION tags, like `docs/guard-data-notes.md`):
  - **`claude -p`:** OBSERVED only that `claude --help` (v2.1.322 in this container) lists `--print`, `--output-format json`, `--json-schema`, `--tools`, `--max-budget-usd`, `--model`, `--no-session-persistence`, `--permission-prompts`, `--disable-slash-commands`, `--setting-sources`, `--strict-mcp-config`, `--bare`. NOT verified: that a call succeeds with the `setup-token` token and no API key (`claude -p` was "Not logged in" in this container); **ASSUMPTION:** the variable the CLI reads is `CLAUDE_CODE_OAUTH_TOKEN` and `--bare` must not be used because it ignores OAuth (STACK.md, unverified); the token's lifetime and what happens at expiry (**ASSUMPTION:** about one year, per STACK.md); the JSON envelope (where the structured result sits, an error flag, cost and token fields, exit codes); whether `--json-schema` is enforced or only advised; whether the call reads the working directory, user settings, hooks or MCP config.
  - **T3:** OBSERVED only that a `t3` binary exists and its top-level help describes running T3 Code (a server and UI); no headless prompt command was seen. ASSUMPTION: a T3 route needs a documented, stable, non-interactive interface; without one the T3 adapter is not built and that is recorded.
  - **Gate:** if no route is verified, Phase 4 still ships the templates (D-04), the port, the fake, sanitising, validation, rendering and fallback with triage off, and the roadmap records "SC4: no API-key route verified" as a blocker. It never falls back to an API key or an SDK silently.
- **D-03:** Triage ships **off**: `triage.enabled` defaults to `false`. With it off the send path is the same as without Phase 4 triage code (no adapter import, no subprocess, no cost file); only the new templates (D-04) apply. The operator turns it on after D-02 and after reading one dry-run with triage (D-14). — **Reversibility:** reversible — config only.

### What Claude sees (TRI-04)
- **D-05:** Triage covers **fresh High alerts only** (operator, 2026-11-19: "Claude line on High items only", and "cause only on fresh High alerts read in this run"). Fresh means: the first alert of a new High series (`High 0→N`) whose Guard report was read in this run, with at least one usable title (D-06). **No Claude line** on: a rise that re-arms an existing series, an alert retried after a failed send (its report is not re-read), a reminder, any digest item, and every non-High kind. An item without a usable title is not sent to Claude and stays rule-only.
  The request is a JSON list: opaque `item_id` (`i1`, `i2`, ... per call), `audit_type`, `high_count`, `age_days`, and `titles`: at most 3 **sanitised** High finding titles. **Never sent:** project and repo names, finding ids, file paths, links, code spans, evidence, report bodies, scores. `audit_type` is Guard text, sent only if it matches `^audit\.[a-z0-9-]+$`, else `other`. The system prompt names the output language (the value of `messages.language`).
  **Operator decision 2026-11-19, amending Phase 2 D-05:** the group may read a short Claude cause derived from sanitised finding titles (the target example is "SQL injection in order search"). It is still never a verbatim report body, evidence, path, link or secret, and the rule text stays counts only. Residual risk accepted: an unfixed vulnerability's nature is visible to every group member.
- **D-06:** **The parser keeps titles, in memory only** (Phase 4 work; today `guard_report.py` keeps only ids, severities and the score). `ReportFinding` gains `title`, parsed from the heading `` ### `<ID>` — <title> ``; an unreadable title makes that finding's title empty, never the report unknown. Guard writes a report in English or in Russian per run (OBSERVED 2026-11-19, `main` commit 9253f73 reads both and recognises a finding by its shape, a `###` heading followed by a severity line), so titles can be Russian: `sanitize_title` and every length or letter check are Unicode-aware (`str.isalpha`, not `[a-z]`).
  **No title is persisted** (operator, 2026-11-19): **state stays schema 3**, no field, history record, ledger entry or run report holds a title, and titles are never logged (the existing "no finding title in logs" test stays). Titles travel only on the in-memory path from the report read to the alert text of the same run (how they ride along, for example an optional field on the evaluation that is never serialised, is Claude's Discretion). Because plan 02-12 skips reports it has already read, a later run does not have the titles again, which is exactly why digests, reminders, re-armed and retried alerts carry no cause.
  One pure `sanitize_title` runs before a title goes anywhere: remove control and format characters and collapse whitespace; remove code spans (backtick text); drop tokens that are links or addresses (`://`, `www.`, `t.me`, `mailto:`, anything with `@`), paths (a `/` or `\`, or a file-extension tail like `.py`, `.yaml`), and identifiers (`F-006`, `SOME-DESCRIPTIVE-SLUG`, `CVE-...`, UUIDs, hex runs of 7 or more); drop the whole title if the secret gate would block it; cap at 80 characters at a word boundary; drop it if fewer than 3 letters (any script) remain; remove duplicates. Only High findings contribute, in report order, 3 at most per series. — **Reversibility:** reversible — no stored data depends on it.
- **D-07:** Delimiting is defence in depth, not the control. The request is built by one pure function: a fixed system prompt in code (never from config or Guard) saying that everything inside the data block is data to describe, never instructions; and one user message with the items as a JSON document between lines `<data id="NONCE">` and `</data id="NONCE">`, a random per-call NONCE checked not to occur in the payload; titles are JSON strings. The real controls are, in order: (1) sanitising (D-06) and the fixed field list (D-05); (2) a typed output with an enumerated priority and a short plain cause (D-08, D-09); (3) every `item_id` cross-checked against the request; (4) rendered text is escaped, carries no links, and passes the existing secret gate. A test plants a hostile instruction string (text that tells the model to change its output) in a finding title, a project name and an audit key, and asserts: project name and audit key are absent or neutralised, the title appears only inside the data block as a JSON string, and a fake Claude that obeys it (unknown `item_id`, a priority outside p1..p3, a link, a dropped item) is rejected by validation. Residual risk accepted: a cause can still be a wrong or silly sentence within the length limit; it cannot add, drop or reorder an alert, and the rule text with its counts is always shown.

### Typed output (TRI-01, TRI-02)
- **D-08:** Pydantic models, strict, `extra="forbid"`, every field required, flat, no dict fields, no recursion (structured-output limits in STACK.md): `ItemTriage(item_id: str, priority: Literal["p1", "p2", "p3"], cause: str)` and `TriageResult(items: list[ItemTriage])`. There is **no summary field** (operator, 2026-11-19) and no count, age, name or severity field, so Claude has nowhere to put one. Priority is lowercase tokens; compare case-insensitively on input, store lowercase, show as `P1`..`P3`. The JSON Schema given to the CLI is generated from the models (`model_json_schema()`); the response is validated by the models whether or not the CLI enforces the schema.
- **D-09:** Validation in two levels. **Whole response rejected** (every item rule-only): not JSON or not the schema, an `item_id` not in the request, a duplicate `item_id`, more items than requested. **One item rejected** (only that item rule-only): `cause` empty after trimming, longer than **60 characters**, containing a line break, control character or the `·` separator, a link or handle (`http`, `www.`, `t.me`, `@`), an HTML angle bracket, or a digit (counts and ages come from code; **ASSUMPTION:** a no-digit rule is not too strict, revisit if live runs show many rejections). The cause is written in the language of `messages.language` (operator, 2026-11-19: Russian when `ru`, English when `en`), whatever language the titles are in; the language is a fixed system-prompt instruction and the checks above are Unicode-aware (60 is a character count, not bytes). A request item missing from the response simply stays rule-only. Rejection counts are logged per reason.
- **D-10:** Rendering is a pure function over (rule text, optional `ItemTriage`). The Claude line goes on its own line under the alert item (single alert or an item of a collapsed summary), `P1 · <cause>`, indented by 3 spaces (**ASSUMPTION:** Telegram keeps leading spaces; if the live check shows it does not, drop the indent). The `P1 · ` form is a catalog entry like every other fixed text. Escaped with `messages.escape`. Rule text, order, counts, ages, names and the collapse decision are computed before triage and untouched. Claude never changes the order of items (TRI-02). Splitting at item boundaries still applies; items are packed with their Claude line, so chunk borders may move, the set of items does not.
  **Amendment 2026-11-23 (T-0051, operator decision: alerts are per project):** the group message has one line per project (`High <total> in <N> audits · <oldest age>`, no repo or audit names; stale and never-ran stay their own lines), so "the alert item" above is the project line. The Claude line goes under it, at most one per project: the entry with the most urgent priority (p1 before p2 before p3), on a tie the alert that sorts first (the oldest). The request to Claude stays per item (D-05..D-09 unchanged); dedup, baseline, re-arm and delivery marks stay per condition.

### Fallback and failure (TRI-03)
- **D-11:** Any failure yields the rule-only message, which is the D-04 text without the Claude line. Failure reasons, one enum, each logged once per call by name and never with prompt, title or response text: `disabled`, `not_installed`, `not_logged_in`, `timeout`, `exit_nonzero`, `refused_or_error`, `not_json`, `schema_invalid`, `unknown_or_duplicate_ids`, `item_rejected` (per item), `over_item_cap`, `no_titles`. The adapter catches everything including unexpected exceptions at the one boundary and returns the outcome (an explained broad `except` there and nowhere else). No retry inside a run: the next run retries delivery anyway. A triage failure never changes the exit code, never makes the run "attention", never sends an ops message, never blocks a send beyond the timeout (D-13). Repeated failure is visible in the log and `doctor` only (an ops notice is Deferred).

### Same alerts with triage on or off (TRI-02)
- **D-12:** Triage runs **after** rules, dedup and reconcile have produced the messages and **before** delivery. It only adds a line to an item's text; it cannot touch `keys`, `audience`, the ledger, the digest decision or any state. If no fresh High alert is to be sent, Claude is not called. Contract, enforced by one test matrix over the fixture scenarios (triage off; on with a good fake; on with each failure reason; on with a hostile fake): the same multiset of alert keys is sent, the state file after the run is identical (the cost file aside), the run report counts and exit code are identical, and with the Claude lines removed the text equals the off-run text (compared with chunking made irrelevant, plus a separate splitting test). A dry-run with triage prints the same set as one without.

### Calls, cost and where it is logged (TRI-05)
- **D-13:** At most **one Claude call per run**, batching all fresh High alerts of that run (the digest and reminders never call Claude), so cost is bounded by run count. `triage.max_items_per_call` (default 30, 1..100): extra items stay rule-only (`over_item_cap`). `triage.timeout_seconds` (default 60, 5..300): on timeout the process group is killed and the run goes on. `triage.max_cost_usd_per_call` (default 0.50, 0.01..5) is passed as `--max-budget-usd` only if D-02 confirms the flag is honoured. `triage.model`: an alias or full model name, validated as a short token, default chosen in 04-01.
- **D-14:** Cost is logged in two places. Every call writes one INFO line: `triage call: items N, outcome <ok|reason>, seconds S, cost_usd X|unknown, input_tokens A|unknown, output_tokens B|unknown`. After each call it is also added to a **separate** small file `state/triage-cost.json` (git-ignored with `state/`, atomic write, one record per UTC day: calls, failures, cost, tokens; the last 35 days kept) and the run logs `triage today (UTC): N calls, M failed, $X`. It is a separate file and state schema 3 is unchanged (operator accepted this, 2026-11-19), so dedup state is untouched and `--dry-run` keeps "writes no state": a dry-run with triage on still calls Claude (the operator needs to read the output before enabling it live), logs the call line and the daily line computed in memory, and writes no file. If the CLI reports no cost (subscription login), the line says `unknown` and counts calls and seconds; no number is invented. A daily budget cap is not built (Deferred).

### Secrets and subprocess hygiene
- **D-15:** The Claude token (the variable name is confirmed in 04-01; candidate `CLAUDE_CODE_OAUTH_TOKEN`) comes from env or `.env` only (AGENTS.md), `.env` mode 0600, is added to the known secrets for log redaction and the secret gate, and is passed to the subprocess by environment, never in argv, a log line or a message. The subprocess gets an allowlisted environment (`PATH`, the token, and what 04-01 shows the CLI needs to find its login), so `TELEGRAM_BOT_TOKEN` and Guard credentials never reach it. It runs in a fresh empty temporary directory (no project `CLAUDE.md`, no `.mcp.json`, no hooks), with all tools disabled and no session persistence (flags confirmed in 04-01). The prompt goes in by stdin, not argv. Stdout is parsed and discarded; it is never logged.

### Operations
- **D-16:** `doctor` gains a triage line: off, or on with the backend, whether the binary is found on `PATH` and whether the token variable is set. It makes **no Claude call**. `docs/deploy.md` gets a Triage section (token from `claude setup-token`, `.env` mode, enable steps, reading the daily line, turning it off), the age wording note from D-04 and the reminder shape. `config/radar.example.yaml` gets a `triage:` block with every key, range and default, validated strictly like the rest (unknown or duplicate keys exit 2; `enabled: true` with an unknown `backend` exits 2).

### Claude's Discretion
- Exact system prompt wording, the generated JSON Schema text, and hints per kind given to Claude, provided D-05..D-09 hold.
- The token variable name and CLI flag set, as 04-01 verifies them; the shape of any `triage-probe`-style command used by 04-01 (it sends only a fixed synthetic item list, never real Guard data).
- Exact wording of the compact lines inside D-04's shapes (resolved lines, the "could not be read" note, the repo form for same-audit-type alerts, footer wording) and of the log lines.
- Module and file names, `TriageRequest` / `TriageOutcome` fields, and how the render seam is threaded through `render_messages` and `render_digest` (an item-level hook, not string post-processing of final text).
- How titles ride from the report read to the alert text inside one run (an optional in-memory field on the evaluation or alert that is never serialised, or a side map), and the Russian wording of every catalog entry.
- Plan sizes and waves. Plans start after T-0033 is merged. Suggested: Wave 1: 04-01 mechanism check (operator-gated) in parallel with the parser keeping titles in memory, `sanitize_title`, config, and the new templates (alerts, reminders, digest) in the catalog in both languages; Wave 2: request, schema, validation and triage render, then the adapter shaped by 04-01 and the pipeline integration with the cost file; Wave 3: on/off matrix, hostile-text test, `doctor`, runbook.

### Folded Todos
None.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project and phase scope
- `AGENTS.md` — standing brief: rules decide and Claude explains; typed output with rule-only fallback; secrets; unverified claims are `ASSUMPTION:`.
- `.planning/ROADMAP.md` — Phase 4 goal and success criteria SC1..SC4 (amended 2026-11-19), "Gates and Open Items".
- `.planning/REQUIREMENTS.md` — TRI-01 (amended), TRI-02..TRI-05, ALERT-07 (new, covers alerts, reminders and the digest, both languages); "Out of Scope": Anthropic API SDK for triage, Claude deciding whether to alert.
- `.planning/HANDOVER.md` — operator choice "CLI or T3, no API key" overrides the research recommendation; `claude -p` not logged in inside the container.
- `.planning/STATE.md` — Phase 4 blocker: route unverified.
- T-0033 (task, not yet merged) — `messages.language` (`en|ru`) and the `texts.py` catalog the templates and the Claude line text are written into. Read its merged code before planning.

### Research (read, but the SDK recommendation is overridden)
- `.planning/research/STACK.md` section "Claude headless with structured output" — the `claude -p` recipe, schema limits, case-insensitive enums, "cross-check every id", `setup-token`. All flags there are claims to re-check in 04-01.
- `.planning/research/FEATURES.md` — one line per item, bounded length, rule-only template as the baseline.
- `.planning/research/PITFALLS.md` — no raw evidence to Claude or Telegram.

### Earlier decisions that still hold or are amended
- `.planning/phases/02-guard-connection-and-core-alerts/02-CONTEXT.md` — D-02 age from the radar's first sight (kept, wording moves to a footer), D-05 counts only in the group (amended by D-05 here for the Claude cause), secrets and `state/` rules.
- `.planning/phases/03-remaining-rules-and-the-digest/03-CONTEXT.md` — D-04..D-07 digest content, tiers, cap and resolved (shapes change in D-04 here, meaning stays), D-11 reminders (interval, clock and delivery unchanged, shape shortened in D-04), ack-before-mark, dry-run writes nothing.
- `docs/guard-data-notes.md` — report heading layout `` ### `<ID>` — <title> ``; reports can hold vulnerability detail: never sent whole to Claude or Telegram, never logged.
- `docs/design/v0.md` — flow `rules -> triage -> dedup -> Telegram`; here triage sits after dedup and reconcile (D-12).
- `config/radar.example.yaml` — config shape and validation style.

### External
No external specs. Claude Code CLI behaviour is checked on the real CLI in 04-01, not from memory.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/enji_guard_radar/ports.py`: where `Triager` joins `GuardTools`, `Notifier` and `Clock`.
- `src/enji_guard_radar/reminders.py` (`render_reminders`, `_item`): the reminder text D-04 shortens.
- `src/enji_guard_radar/guard_report.py` (`ReportFinding`, `_finding`, `_finding_blocks`, `parse_report_markdown`, as on `main` after 9253f73: English and Russian layouts): finding headings are already split into id and severity; the title is the text after the id in the heading and is dropped today. Plans start from current `main`, not from the Phase 3 snapshot.
- `src/enji_guard_radar/messages.py` (`render_alert`, `render_summary_item`, `summary_header`, `render_messages`, `escape`, `clean_name`, `pack_items`, `telegram_length`) and `digest.py` (`_high_item`, `_stale_item`, `_gap_item`, `build_digest`, `_body`, `_header`, `render_digest`): the render functions D-04 rewrites; `Alert` and `DigestInput` hold counts, ages and kinds.
- `src/enji_guard_radar/high_rules.py` (`HighSeries`, previous count) and `ledger.py` (new vs rearmed vs retried alerts): tell a fresh series from a rise or a retry; `state_store.py` stays at schema 3 and is not touched by Phase 4.
- `src/enji_guard_radar/pipeline.py` (`_run_locked`, `_due_reminders`, `_deliver`, `_digest_step`): messages are built, then delivered with ack-before-mark; the triage map slots in between building the alert messages and delivery. `Deps` gains an optional `triager`.
- `src/enji_guard_radar/secret_guard.py` (`find_secret`, `redact`, `SecretGatedNotifier`, `configure_logging`): the final gate, reused by `sanitize_title`; the Claude token joins its known secrets.
- `src/enji_guard_radar/poll_cost.py`: pattern for a pure cost record plus a formatted log line.
- `src/enji_guard_radar/config.py`, `config/radar.example.yaml`, `settings.py`, `env_file.py`, `doctor.py`, `cli_doctor.py`: strict config, env loading, the doctor line.
- `tests/test_guard_report.py`: asserts a finding title never appears in logs; this stays true.

### Established Patterns
- Pure functions with table tests; I/O behind small intent-named ports, faked in tests.
- Ack-before-mark; `--now` clock injection; dry-run writes nothing and sends nothing.
- Every new knob is a validated config key with a documented range and default.
- ASSUMPTION markers in code and docs for anything not verified against the real thing.

### Integration Points
- `pipeline.py`: after `render_messages` (alerts only), before `_deliver`; `cli.py` builds the adapter only when `triage.enabled`, and the `--dry-run` path calls it too (D-14).
- `docs/deploy.md`: a Triage section; `.gitignore` already covers `state/`.
</code_context>

<specifics>
## Specific Ideas

- The group message reads as the Phase 3 facts in one line, plus at most one short Claude line on a fresh High alert; the first line alone is enough in a muted-notification preview.
- Cause wording stays plain and hedged by length alone (60 characters); no summary.
- A dry-run with triage on is the operator's review step before the live switch, like the Phase 2 dry-run review.
</specifics>

<deferred>
## Deferred Ideas

- A Claude cause on digests, reminders, re-armed (rise) and retried alerts: needs titles persisted (a state schema change); the operator declined that on 2026-11-19.
- Tell the ops chat when triage keeps failing (N failed calls in a row).
- A daily budget cap that turns triage off for the rest of the UTC day.
- Claude lines on stale, low-score and tooling-gap items, and on resolved items.
- A second adapter kept warm for failover, or a retry inside a run.
- A cache so a delivery retry does not pay for the same triage twice.
- Using Claude's priority to order items (TRI-02 forbids it for now).
- Digest line variants per audience, or a week-over-week trend (DIG-05, v2).

### Reviewed Todos (not folded)
None.

## Open questions

None. The first-pass questions were answered by the operator on 2026-11-19 and the follow-ups the same day (third pass); all are recorded in `04-DISCUSSION-LOG.md`: subscription use allowed and the token in `.env` (D-02, D-15), sanitised titles go to Claude (D-05, D-06), no summary and a 60-character cause (D-08, D-09), no persisted titles and a cause only on fresh High alerts (D-05, D-06), cause language follows `messages.language` (D-09), reminders shortened (D-04), plans start after T-0033 (domain). Remaining unverified points are marked `ASSUMPTION` above and are settled in 04-01, after T-0033 is merged, or at the first live check.
</deferred>

---

*Phase: 4-Claude Triage and Short Templates*
*Context gathered: 2026-11-19*
