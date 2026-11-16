# enji-guard-radar — AGENTS.md

Standing brief for every AI agent working in this repo. Read it first.
`CLAUDE.md` is a symlink to this file. Keep it short; detail lives in `docs/`.

## What we're building

A radar over Enji Guard. On a schedule it pulls project data from Guard,
applies deterministic rules to spot problems, asks Claude to triage and explain
what it found, and alerts a Telegram group through a bot when something is wrong.

Design: `docs/design/v0.md`.

## Stack

Python + uv. No database; state (last seen findings, dedup keys) lives in files
under `state/` (git-ignored). Developed through code-factory: tasks are queued
with `code-factory add`, implemented in worktrees, verified, and handed back as
draft PRs for human review.

## How to verify (the gate)

```
make verify
```

= ruff lint + format check + mypy strict + fake-done marker scan + pytest.
Nothing is "done" until it passes; state the command and the result line.

## Rules

- Simplest thing that works. No speculative features. Surgical edits.
- Deterministic core: rules, dedup and message formatting are pure functions with
  table tests. I/O (Guard MCP, Claude, Telegram, files) sits behind small
  intent-named boundaries and is faked in tests.
- Claude triage never decides *whether* to alert on its own: rules decide, Claude
  explains and prioritises. Its output is parsed into a typed model; invalid
  output falls back to the rule-only message.
- Secrets (`TELEGRAM_BOT_TOKEN`, Guard credentials) come from env / `.env`, never
  from git, logs or alert text.
- Unverified claim about Guard MCP tools, Telegram API or Claude CLI flags:
  check for real or flag it as `ASSUMPTION:` and ask.
- New behavior ships with tests and a log line on the important branches.
- Tech debt / ideas: one file per item in `docs/tech_debt/active/` or
  `docs/ideas/`, named `YYYY-MM-DD-slug.md`.
- Commits: Conventional Commits, one concern each.
