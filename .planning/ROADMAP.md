# Roadmap: Enji Guard Radar

## Overview

A scheduled, one-shot radar that reads Enji Guard, applies deterministic rules, and alerts a Telegram group of executives only when something is really wrong. Silence means healthy. The journey: first prove the whole pipeline on synthetic fixtures with no network access (Phase 1), then connect it to real Guard on the radar's own read-only login and switch on the core alerts and the timer (Phase 2), then widen the rule set and add the always-sent weekly digest (Phase 3), and finally let Claude explain what the rules have already decided (Phase 4). Rules decide; Claude never adds, drops or suppresses an alert.

Scope: 41 v1 requirements in 4 phases (granularity: coarse). RULE-04 was dropped on 2026-11-16 and is not mapped.

## MVP

**MVP = Phases 1 and 2 (27 of 41 requirements).** Defined 2026-11-16. Phase 1 alone is not an MVP: it runs only on synthetic fixtures and tells nobody anything real. The MVP is done at the end of Phase 2.

**What the group gets:** on a timer, with no operator action, the Telegram group receives a short alert when (a) the number of High findings rises in a repo's audit, or (b) a project has had no completed Guard run for N days. The operator gets a message in a separate ops chat when the radar itself breaks and when it recovers.

**Acceptance test (all must hold, on real Guard data):**
1. A real run against the radar's own read-only Guard login sends one real alert to a test chat, and an identical second run sends nothing.
2. The timer runs unattended for 7 days across at least one Guard access-token expiry without the operator logging in again. If the token cannot be refreshed unattended, the MVP is not met, and the ops chat must report it.
3. Breaking Guard access on purpose produces exactly one ops message, and fixing it produces exactly one recovery message.
4. `doctor` passes on the production host.

**In the MVP:** Guard source and parser, stale rule, High-count rules (rise and recurring clear), state and dedup, Telegram send with error classes and dry-run, `probe`, timer, `doctor`, ops chat, secret handling.

**Not in the MVP (Phases 3 and 4):** weekly digest and its always-sent all-clear, AI-readiness, tests and tooling rules, recurring-N flag, reminders, Claude triage, and all v2 items.

**Known gap of the MVP:** without the digest there is no heartbeat, so silence is ambiguous for up to a week between a healthy radar and a dead one that failed to notice. The ops chat (OPS-02) and `doctor` cover most of this but not a dead timer. Phase 3 closes it. Accepted for the MVP, because it is the cheapest way to get real alerts to the group.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Working Pipeline on Fixtures** - Models, parser, stale rule, durable state and dedup, Telegram send and dry-run, all runnable on `fixtures/guard/`
- [ ] **Phase 2: Guard Connection and Core Alerts** - Real read-only Guard adapter, `probe`, High-count rules, timer, `doctor` and ops-chat alert (gated on Guard access)
- [ ] **Phase 3: Remaining Rules and the Digest** - AI readiness, tests, tooling and recurring rules, reminders and the always-sent weekly digest
- [ ] **Phase 4: Claude Triage** - Summary, probable cause and priority from Claude through a swappable CLI/T3 boundary, with rule-only fallback

## Phase Details

### Phase 1: Working Pipeline on Fixtures
**Goal**: The operator can run the whole pipeline end to end on the committed synthetic fixtures, with no network access to Guard, and see a stale project turn into a concise, deduplicated Telegram alert (or a dry-run print), with state that survives crashes and restarts.
**Depends on**: Nothing (first phase). Dry-run and fake-Telegram tests need nothing from the operator. The live-send check needs a Telegram bot token and a private test chat from the operator (`.env`, never in git).
**Requirements**: SRC-03, SRC-04, SRC-05, SRC-06, RULE-01, RULE-10, STATE-01, STATE-03, STATE-04, STATE-05, STATE-06, ALERT-02, ALERT-03, ALERT-04, ALERT-05, ALERT-06, OPS-04
**Success Criteria** (what must be TRUE):
  1. The pipeline reads everything from `fixtures/guard/` with no network: JSON lists and Markdown reports parse into typed snapshots with timezone-aware UTC times (3, 4 and 6 digit fractions) and an injected clock; only runs with `status == completed` and `report_status == ready` count; an unparseable report, or a failed, empty or partial fetch, becomes "unknown" and never creates or resolves a finding.
  2. A dry-run with a fixed clock prints one concise alert (project, what is wrong, how long, severity) for each stale project, tells "never ran" and "Guard unreachable" apart from "stale", skips paused or excluded projects, writes no state and sends nothing. N has a default and per-project overrides in validated config, and an unknown config key is rejected with a clear error.
  3. A live run against a private test chat sends the alert; an identical second run sends nothing; a send that fails is retried on the next run because an alert is recorded only after Telegram acknowledges it; the first run for a project records a baseline instead of flooding; many alerts collapse into one summary; messages are escaped and split at item boundaries under the Telegram length limit; rate limit, migrated chat id and bad token are each handled as their own error class.
  4. State is saved atomically with a backup; a corrupt state file halts the run with a non-zero exit and an operator-visible error and nothing is reset; a second overlapping run exits cleanly without sending.
  5. The bot token comes only from env or `.env` and never appears in logs, errors or alert text, and a message containing a secret-looking string is blocked before it is sent.
**Plans**: 7 plans

Plans:
**Wave 1**
- [x] 01-01-PLAN.md — Wave 1: synthetic six-project fixture scenario and call manifest, with a standard-library consistency test

**Wave 2** *(blocked on Wave 1 completion)*
- [x] 01-02-PLAN.md — Wave 2: tracer skeleton, dry-run on fixtures end to end (models, ports, Guard reader with run-paging bound, fixture source, stale rule, render, pipeline, CLI, architecture guard)

**Wave 3** *(blocked on Wave 2 completion)*
- [x] 01-03-PLAN.md — Wave 3: strict Guard parsing, Markdown reports page by page, unknown on failed/empty/partial fetch (zero items stays a real answer), read-only fixture source
- [x] 01-04-PLAN.md — Wave 3: Telegram sender with error classes, secret gate, token redaction, secrets from env/.env
- [x] 01-05-PLAN.md — Wave 3: strict config with pause/exclude and per-project N, stale boundaries, concise escaped alerts with collapse and split
- [x] 01-06-PLAN.md — Wave 3: ledger (dedup, baseline, retry, freeze, optional count in schema 1) and atomic file state with backup, corrupt halt and lock

**Wave 4** *(blocked on Wave 3 completion)*
- [x] 01-07-PLAN.md — Wave 4: live run wiring, failure paths, dry-run with state, and the human live check against a private test chat

### Phase 2: Guard Connection and Core Alerts
**Goal**: The radar reads real Guard data on its own read-only login and, on a timer, alerts the group about new or worsening High findings and newly stale projects, and the operator hears about it when the radar itself stops working.
**Depends on**: Phase 1. **Gated on Guard access:** the radar needs its own read-only Guard login with `projects:read` and `audits:read` scopes, never `audits:run`. This login is separate from the one Claude Code uses and is created once by the operator (codes and tokens never pasted into chat). Until it exists, only the parts that need no live Guard can be built. Token lifetime and refresh behaviour are unknown (`docs/guard-data-notes.md`, Auth) and are the main risk to "unattended".
**Requirements**: SRC-01, SRC-02, RULE-02, RULE-03, RULE-09, STATE-07, ALERT-01, OPS-01, OPS-02, OPS-03
**Success Criteria** (what must be TRUE):
  1. With the radar's own login, the radar connects to Guard without any interactive prompt, lists the projects in scope using only the read tools (never `run_audit`), and `probe` records sanitized real responses as fixtures (no real names, secrets or finding evidence committed). The recorded fixtures parse through the Phase 1 parser, and any mismatch with the synthetic fixtures is fixed there.
  2. A dry-run on real Guard data prints what would be sent: a High-count alert for a `(repo, audit type)` with its age, and a rise of High findings in `audit.security` or `audit.public-api-access-safety` with no raw evidence in the text. Only a new high-risk finding or a newly stale project is immediate. The operator reviews this output before the executive group is enabled.
  3. An alerted, still-true High condition is not re-sent; a rise in the High count re-arms its alert; a flapping result does not flip alerts on and off, because a `(repo, audit type)` is clear only after M consecutive completed runs with zero High findings.
  4. After the poll cost is measured and logged, a timer runs the one-shot command unattended. After K consecutive failed polls one message goes to a separate ops chat, and one more when the radar recovers.
  5. `doctor` reports pass or fail for Guard reachability, the Telegram token, and the bot's membership in the group chat.
**Plans**: 11 plans

Plans:
**Wave 1**
- [ ] 02-01-PLAN.md — Wave 1: State schema 2 (High history by task_id, poll health, alerted_count, v1 migration), history fold with M-clean-run clear, peak re-arm
- [ ] 02-02-PLAN.md — Wave 1: High-count alert rule, counts-only alert text, strict `high` config and the age threshold with per-project override
- [ ] 02-03-PLAN.md — Wave 1: Guard token layer (auth error contracts, credential file, refresh with rotation persisted before use, failure classification, lock)

**Wave 2** *(blocked on Wave 1 completion)*
- [ ] 02-04-PLAN.md — Wave 2: High-count alerts in the run (fold per project, alerts from stored history, skip known reports, updated baselines)
- [ ] 02-05-PLAN.md — Wave 2: `auth` command (discovery, client registration, PKCE, hidden paste, read-only grant validation)
- [ ] 02-06-PLAN.md — Wave 2: MCP adapter over the official SDK, behind the operator's dependency decision and package check (not autonomous)

**Wave 3** *(blocked on Wave 2 completion)*
- [ ] 02-07-PLAN.md — Wave 3: Poll health and the ops chat (K failures, one outage, one recovery, save on failed polls)
- [ ] 02-08-PLAN.md — Wave 3: `doctor` (Telegram token, chat membership, Guard reachability, local setup)

**Wave 4** *(blocked on Wave 3 completion)*
- [ ] 02-09-PLAN.md — Wave 4: `probe` with allowlist sanitizer, round-trip and leak checks, recorded-fixture contract test

**Wave 5** *(blocked on Wave 4 completion)*
- [ ] 02-10-PLAN.md — Wave 5: Unattended live run (real Guard by default, lazy connect inside the lock, poll cost), systemd units, cron fallback, runbook

**Wave 6** *(gated on the operator and the production host)*
- [ ] 02-11-PLAN.md — Wave 6: Human live verification (auth, first probe, real dry-run review, test-chat check, 7-day soak, failure drill, doctor on the host)

### Phase 3: Remaining Rules and the Digest
**Goal**: The group also hears about weak AI readiness, weak tests, missing analysis tooling and High findings that keep coming back, in one weekly digest that is always sent, worst first, plus reminders for what stays unaddressed.
**Depends on**: Phase 2 (real data, High-count history and the alert lifecycle). Thresholds are set from the distribution seen in real Guard data, not guessed.
**Requirements**: RULE-05, RULE-06, RULE-07, RULE-08, STATE-02, DIG-01, DIG-02, DIG-03, DIG-04
**Success Criteria** (what must be TRUE):
  1. A repo whose `audit.ai-readiness` score or `audit.tests` score (0 to 100) is below its configured threshold appears in the digest with the score, and never as an immediate alert.
  2. A `(repo, audit type)` whose High count has not reached zero across N consecutive completed runs, counted by Guard `task_id` and not by radar polls, is flagged as recurring with N configurable.
  3. A repo with no recent completed run of `audit.dead-code`, `audit.implementation-unification`, `audit.security` or `audit.dependency-hygiene` shows up as a tooling gap in the digest only.
  4. One weekly digest reaches the group even when nothing is open (a one-line all-clear), lists open items worst first with each item's age and a "+N more" cap, shows the time of the last successful Guard poll, and carries resolved items and tooling gaps.
  5. An unaddressed high-risk finding is re-sent as a reminder at the configured interval in days, and not before.
**Plans**: TBD

### Phase 4: Claude Triage
**Goal**: Every message that will be sent carries a short Claude-written summary, probable cause and priority, and the set of alerts is exactly what the rules alone would have sent.
**Depends on**: Phase 2 (real alerts to enrich), plus Phase 3 for digest items. Claude is reached through the CLI or T3 with no API key; the mechanism is unverified (see Gates and Open Items), so verifying it is the first piece of work in this phase.
**Requirements**: TRI-01, TRI-02, TRI-03, TRI-04, TRI-05
**Success Criteria** (what must be TRUE):
  1. Each alert and digest item that will be sent shows a short summary, probable cause and priority, and the Claude output is validated against a typed schema before use.
  2. Turning triage on or off sends the same alerts. Counts, ages and names in every message come from code, and invalid, refused, timed-out or failed Claude output falls back to the rule-only message.
  3. Raw security evidence never reaches Claude, and hostile text planted in a finding fixture is delimited as data and cannot steer the output.
  4. The triage boundary is swappable and needs no API key, and the daily cost is logged.
**Plans**: TBD

## Gates and Open Items

**Phase 2 gate.** Needs the radar's own read-only Guard login (`projects:read`, `audits:read`; never `audits:run`). Not created yet.

**Flagged from `docs/guard-data-notes.md` (ASSUMPTION or open there, not settled here):**
- Run statuses other than `completed` and `report_status` other than `ready` were never observed. "Completed and ready" is a proposal, and which statuses count as a run is an open item (Phases 1 and 2).
- `audit.ai-readiness` standing in for the dropped harness metric needs operator confirmation (Phase 3, RULE-05).
- `audit.implementation-unification` as the "duplicate detector" is unconfirmed (Phase 3, RULE-07).
- Severity words above High or below Low were never observed. Unknown words must be parsed as "unknown", not crash and not dropped (Phases 1 and 2).
- Rate limits are undocumented and the poll cost is unmeasured. Measure before the timer is enabled (Phase 2).
- Whether the stale threshold is per repo or per project is open. RULE-01 as approved uses the project's newest completed run of any type.
- Radar login, token lifetime and refresh behaviour are unknown (Phase 2).

**Questions for the phase planners:**
- First-run baseline vs stale: a project already stale on the first run is not "newly" stale, so it would stay silent until the Phase 3 digest. Decide whether that is acceptable (Phase 1).
- Source of history for RULE-02 age and RULE-08/09 run counts: Guard run history is returned a few runs per call, so decide between reading older reports and accumulating history in state from the first poll (Phase 2).
- RULE-02 (age in days) and RULE-08 (run count) measure the same streak of runs with High findings. Both are kept as approved.
- STATE-03 "notifies the operator" before the Phase 2 ops chat exists: Phase 1 uses the ops chat id if configured, otherwise a non-zero exit and an error log.

**Operator inputs still needed:** Telegram bot token and a private test chat (Phase 1 live check), group chat id and ops chat id (Phase 2), deployment host for the timer, systemd or cron (Phase 2), digest day and timezone and the reminder interval (Phase 3), whether every group member may see security summaries (Phase 2), and how Claude is reached with no API key (Phase 4).

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 (Phase 4 may be planned in parallel with Phase 3 once Phase 2 is complete)

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Working Pipeline on Fixtures | 7/7 | Complete | 2026-11-17 |
| 2. Guard Connection and Core Alerts | 0/11 | Planned | - |
| 3. Remaining Rules and the Digest | 0/TBD | Not started | - |
| 4. Claude Triage | 0/TBD | Not started | - |
