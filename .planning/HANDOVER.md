# Handover — enji-guard-radar (2026-11-16)

Read this first in a new session. Then read `.planning/PROJECT.md`, `.planning/REQUIREMENTS.md` and `.planning/research/SUMMARY.md`.

## Where we are

`/gsd-new-project` is **mid-flow**. Done: questioning, `PROJECT.md`, `config.json`, research (4 researchers + `SUMMARY.md`), `REQUIREMENTS.md` drafted. **Not done:** requirements approval, roadmap (`ROADMAP.md`, `STATE.md`), and the project guide. The workflow resumes at "Step 7 approval" then "Step 7.5 / Step 8 roadmap".

Because `.planning/` exists but `ROADMAP.md` and `STATE.md` are missing, GSD treats init as incomplete, so re-running `/gsd-new-project` resumes from the first missing artifact. Keep the existing `PROJECT.md`, `REQUIREMENTS.md` and `config.json`.

## What the project is

A radar over Enji Guard for a Telegram group of executives (tech quality). On a timer it polls Guard (deterministic MCP client calls), applies pure rules, has Claude triage and explain, and alerts Telegram. Silence means healthy. Details: `PROJECT.md`.

## Decisions made with the operator

- Deterministic poller + Claude triage. Rules decide, Claude only explains and prioritises and can never add or drop an alert.
- Alert cadence: immediate alert only for a new high-risk finding or a newly stale project, plus one weekly digest that is **always sent** (a one-line all-clear when nothing is open).
- Stale runs: fixed per-project threshold N, with a default and overrides.
- "Harness" = Guard's own harness metric plus presence of `GUARD.md`.
- Guard works better when a project has dead-code, duplicate, security and dependency scanners, so a digest-only "analysis tooling" rule flags projects missing them.
- Recurring finding = **open across N consecutive Guard runs** (counted by Guard run id, not radar polls). Resolution is debounced (M clean runs).
- Claude triage: **CLI or T3, no API key** (operator choice). This overrides the research recommendation of the Anthropic API SDK. Keep a swappable `Triager` boundary. Mechanism is still to be verified (see open items).
- v1 scope: recurring + debounce are in. Silence file, quiet hours, digest trend delta and inhibition are v2.
- GSD settings: yolo, coarse, parallel, commit docs, adaptive models, research / plan check / verifier on.
- Roadmap shape agreed in principle: **4 phases**, not the researchers' 6:
  1. Working pipeline on fixtures (models, stale rule, state and dedup, Telegram send, dry-run)
  2. Guard connection and core alerts (gated on Guard access: `probe`, real adapter, high-risk, security and harness rules, timer, `doctor`, ops-chat alert)
  3. Remaining rules and the digest (AI readiness, tests, tooling, recurring, reminders)
  4. Claude triage

## Requirements status

`REQUIREMENTS.md` has 42 v1 requirements in 7 groups (SRC, RULE, STATE, ALERT, DIG, TRI, OPS). **The operator has not yet said "approved".** They asked for a pitch (given, in Russian) and for fewer phases. Ask for approval, then commit, then run the roadmapper with the 4-phase structure. Traceability table is empty until the roadmapper fills it.

## Guard MCP

- Server: `enji-guard`, HTTP, `https://guard.enji.ai/mcp`, user-scope config, now **authenticated in this container** (`claude mcp list` shows Connected).
- The tools were **not loaded** in the previous session (started before auth). In a new session they should appear. If they don't, try `/mcp` reconnect.
- Auth is OAuth (PKCE, authorization server `https://fleet.enji.ai`, grants: authorization_code and refresh_token, no client-credentials or static tokens). Scopes advertised: `projects:read`, `audits:read`, `audits:run`. The radar must request **read scopes only**, never `audits:run`.
- This login belongs to Claude Code. The radar needs its **own** one-time login (a small `auth` command that stores and refreshes a read-only token, in `.env`/`state`, never in git). Refresh-token lifetime is the main risk to "unattended" and is unknown.
- Do **not** run any scan or write tool. Discovery is read-only.
- Never ask the operator to paste OAuth codes, tokens or callback URLs into chat.

## Next steps, in order

1. Confirm the Guard tools are visible. List them, then call only read tools on 2 or 3 projects.
2. Save raw JSON outside git (scratchpad), then write sanitized fixtures to `fixtures/guard/` (placeholders for project names, emails, secrets, code snippets). Check them for leftovers.
3. Write `docs/guard-data-notes.md`: stable finding id, run identity and statuses, timestamp format, severity scale, pagination, harness / AI-readiness / test metrics (names and ranges), where `GUARD.md` and tooling presence show up, token and refresh behaviour.
4. Check `REQUIREMENTS.md` against that data. Fix any guessed requirement, then ask the operator to approve.
5. Commit requirements, then run the roadmapper (4 phases, vertical-slice mode, MVP not chosen yet, ask or default to standard). Generate `STATE.md`.
6. Create the project guide for GSD. The runtime would write `.claude/CLAUDE.md`, but the repo already has a root `CLAUDE.md` symlinked to `AGENTS.md`. Don't create a second guide. Put any GSD guidance into `AGENTS.md`.
7. Queue the first phase through code-factory (see below).

## Repo and code-factory state

- GitHub: `https://github.com/Konstantin-Babushkin/enji-guard-radar`. Only the first commit (`69d71ef` scaffold) is pushed to `origin/main`. **The `.planning/` commits (`34b89df` to `cb6bb0a`, plus the handover commit) are local only.** Push when the operator agrees.
- Scaffold: Python + uv, `make verify` passes (ruff, mypy strict, marker scan, pytest), `AGENTS.md` (`CLAUDE.md` symlink), `docs/design/v0.md`.
- Pin Python 3.12 and raise dependency floors in `pyproject.toml` (current ruff and mypy floors are stale per the stack research).
- code-factory target registered: task store `/factory/tasks/enji-guard-radar/factory.yaml`, checkout `/factory/repos/enji-guard-radar` (a clone of origin), T3 project added. Opus 5.5 implements, Sonnet 5.5 verifies, budget $15 per task and $100 per day. `alerts:` is empty, `debt_feeder` is off. **No tasks queued.** Run `code-factory --tasks-dir /factory/tasks/enji-guard-radar ...`. Do not use the default `CODE_FACTORY_TASKS_DIR` (`/factory/dogfood-store`), which belongs to code-factory itself.
- The code-factory checkout is a separate clone. It must pull from origin, so **push the planning commits before queuing a task**, or the agents won't see them.
- Unknown: whether the code-factory daemon is running (no `ps` here).

## Open items (need the operator)

- Approve the requirements (or adjust).
- Ops chat for "radar degraded" messages (`TELEGRAM_OPS_CHAT_ID`), Telegram bot token and group chat id (through the secret prompt, into `.env`).
- Digest day and timezone, quiet hours (v2), the reminder interval.
- Which Guard run statuses count as a "run". Whether dormant or archived projects are excluded.
- Whether every group member may see security-finding summaries.
- How Claude is reached without an API key: `claude -p` is **not logged in inside this container** ("Not logged in"), so verify the CLI or T3 route (for example how code-factory drives Claude through T3 / `t3` CLI) before the triage phase. Unverified: `claude -p` flags such as `--max-turns` and error envelopes.
- Deployment host for the timer (systemd or cron). Where the radar runs in production (this container or the host) decides where its own Guard login lives.
- Thresholds for harness, AI readiness, tests and recurring N and M. Set them from real Guard data, not guesses.

## Pitfalls to remember (full list in `research/PITFALLS.md`)

- A failed or empty Guard fetch must never create or resolve findings. Treat it as "unknown".
- First run records a baseline and does not alert on history. Never auto-reset corrupt state.
- Mark an alert as sent only after Telegram acknowledges it. A lock prevents overlapping runs.
- The Telegram token sits in the request URL: silence the HTTP client loggers and redact. Never forward raw security evidence to Claude or Telegram.
- Use HTML parse mode (not MarkdownV2). The limit is 4096 characters and about 20 messages per minute per group.
