# Enji Guard Radar

## What This Is

A radar over Enji Guard for the executives and stakeholders who own tech quality. On a schedule it pulls project data from Guard through the Guard MCP, applies deterministic rules to spot problems, has Claude triage and explain what it found, and posts concise alerts to a Telegram group through a bot. Silence means healthy.

## Core Value

Stakeholders learn about a real problem in Telegram, in a few lines and without false alarms, instead of finding out weeks later or never.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Poll Enji Guard (via its MCP) for every in-scope project's state: scan runs, findings, risk, readiness signals
- [ ] Rule: **stale runs.** Alert when a project has had no Guard run for N days. N is a fixed per-project threshold with a default and per-project override
- [ ] Rule: **recurring findings.** Alert when the same finding keeps coming back across runs
- [ ] Rule: **unaddressed high risk.** Alert when a high-risk finding stays open past a threshold, with its age
- [ ] Rule: **security.** Alert on new or worsening security findings
- [ ] Rule: **harness.** Alert when Guard's harness metric is weak, or when the repo has no `GUARD.md`
- [ ] Rule: **analysis tooling.** Guard finds issues more easily when the scanned project ships dead-code finders, duplicate detectors, security scanners and dependency scanners (инструменты поиска мертвого кода, дубликатов, сканеров безопасности и зависимостей). Flag projects missing any of these, as a digest item rather than an immediate alert. How presence is detected (Guard data vs repo files) depends on Guard MCP data
- [ ] Rule: **AI readiness.** Alert on a weak Guard AI-readiness signal
- [ ] Rule: **tests.** Alert on a weak Guard test signal
- [ ] Claude triages findings (short per-project summary, probable cause, priority) and returns typed, schema-validated output. If the output is invalid, the alert falls back to the rule-only text
- [ ] Dedup: an alerted, still-open finding is not re-sent. Reminders for unaddressed items go out only on a set interval
- [ ] Immediate Telegram alert only for a new high-risk finding or one that has gone stale
- [ ] One concise weekly digest message to the group: everything open and how long it has been open, worst first
- [ ] Runs unattended on a schedule. State (last-seen findings, dedup keys) lives in files, not a database
- [ ] Secrets (Telegram bot token, Guard credentials) come from env/`.env`, never from git, logs or alert text

### Out of Scope

- Fixing the problems it finds — it observes and alerts only
- A dashboard or web UI — Telegram is the interface
- Claude deciding whether to alert — the rules decide, Claude only explains and prioritises
- Alerting individual developers or per-PR noise — the audience is executives on tech quality
- A database — file state is enough at this scale

## Context

- Audience: a Telegram group of executives and stakeholders who care about tech quality. They tolerate very little noise; an alert that proves false costs trust.
- Problems today: Guard scans go long stretches without running, the same problems recur, and high-risk findings sit unaddressed with no one chasing them.
- The Guard MCP is connected to Claude on the operator's side but is **not configured in this environment**. Its tool names, auth and data shape are unknown. The rules and the snapshot model must be drafted against real Guard data, not guessed.
- Guard works better on projects that already run dead-code, duplicate, security and dependency scanning tools, so the radar nudges teams to add them.
- "Harness" means Guard's own harness metric plus the presence of a `GUARD.md` in the project. The exact metric name and shape come from Guard data.
- Poller is deterministic code. Claude is used only for triage and explanation (decision made with the operator).
- Built through code-factory: tasks queued with `code-factory add`, implemented in worktrees, verified with `make verify`, handed back as draft PRs. The repo is scaffolded (Python + uv, verify gate, `AGENTS.md`, `docs/design/v0.md`) and registered as a target at `/factory/tasks/enji-guard-radar`.

## Constraints

- **Alert quality**: No false alarms; very concise messages — the audience mutes noisy bots
- **Architecture**: Rules, dedup and message formatting are pure functions with table tests; I/O sits behind small boundaries and is faked in tests
- **Dependencies**: Guard MCP access (URL/command and auth) is required before the real poller can be built and rules validated. Telegram bot token and group chat id are needed to send
- **Tech stack**: Python + uv, no database, file state under `state/`
- **Security**: Secrets never reach git, logs or alert text
- **Verification**: `make verify` (ruff, mypy strict, marker scan, pytest) must pass before anything is called done

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Deterministic poller + Claude triage | Predictable, cheap, testable; Claude explains but does not decide | — Pending |
| Alert cadence: immediate event alert plus weekly digest | Keeps channel quiet; urgent items are not delayed | — Pending |
| Stale-run rule uses a fixed per-project threshold | Simple and explainable; no false alarms from a "usual cadence" heuristic | — Pending |
| Harness = Guard metric + `GUARD.md` presence | Operator's definition | — Pending |
| Developed through code-factory | Task conveyor with worktrees, cross-model verification and human PR review | — Pending |
| Guard MCP details deferred (configure later) | Not available in this environment yet | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-11-16 after initialization*
