# API Coverage - Phase 3 (Remaining rules and the weekly digest: Guard run and report reads for scores, tooling and recurring; Telegram sends to the group for the digest and reminders)

> Full coverage by default. Opt-outs are explicit, reasoned decisions.
> Phase 3 adds no new Guard tool and no new Telegram method: it reads more from the four Guard read tools already used (plans 03-06 and 03-09) and sends two new kinds of message to the group chat through the existing `sendMessage` path (plans 03-10 and 03-11). The login stays read-only.

| capability | decision | reason |
|---|---|---|
| guard.list_projects | INTEGRATE | |
| guard.list_repositories | INTEGRATE | |
| guard.list_audit_runs | INTEGRATE | |
| guard.get_audit_report | INTEGRATE | |
| guard.list_audit_runs audit-type filter argument | OPT-OUT | not needed yet: its name and effect are not verified (ASSUMPTION in docs/guard-data-notes.md); tooling gaps (RULE-07) use the run history the radar has seen, and docs/ideas/2026-11-18-tooling-gap-history-lookup.md records the faster lookup to verify with `probe` first |
| guard.list_audits | OPT-OUT | not needed: the configured `tooling_gaps.audit_types` list names the audit types and the catalog adds no run evidence |
| guard.get_audit | OPT-OUT | not needed: Fleet execution status is not used by any Phase 3 rule |
| guard.run_audit | OPT-OUT | explicitly out of scope by design: write tool, needs audits:run, which the radar never requests; the adapter allowlist rejects it before any I/O |
| guard.any other write or run tool listed by tools/list | OPT-OUT | explicitly out of scope by design: the radar's login is read-only and only the four read tools pass the allowlist |
| telegram.sendMessage to the group: weekly digest | INTEGRATE | |
| telegram.sendMessage to the group: reminders | INTEGRATE | |
| telegram.sendMessage to the ops chat: outage and recovery | INTEGRATE | |
| telegram.sendMessage to the ops chat: a skipped digest week | OPT-OUT | not decided: 03-CONTEXT.md lists it under Deferred Ideas; a WARNING log line and `doctor`'s last-digest field are the minimum |
| telegram.sendMessage disable_notification (silent digest) | OPT-OUT | not needed: not requested; the digest notifies like any other message |
| telegram.pinChatMessage | OPT-OUT | not needed: not requested, and it needs an admin right the bot may not have |
| telegram.editMessageText and deleteMessage | OPT-OUT | not needed: each digest and reminder is a new message; nothing is edited or retracted |
| telegram.sendMessage.message_thread_id (forum topics) | OPT-OUT | not needed yet: no topic-enabled group is configured; revisit if the executive group enables topics |
| telegram.getUpdates and setWebhook | OPT-OUT | explicitly out of scope: the radar only sends; interactive acknowledgement is DLV-01, deferred to v2 |
| claude CLI or T3 (triage of digest items) | OPT-OUT | not needed in this phase: Claude triage is Phase 4 (TRI-01 to TRI-05) |

# Requirement, decision and success-criterion coverage - Phase 3

## Plan table

| Plan | Wave | Depends on | Files (source) | Requirements |
|---|---|---|---|---|
| 03-01 State schema 3 | 1 | none | ledger.py | RULE-05, RULE-06, RULE-07, STATE-02, DIG-04 |
| 03-02 Config: digest, reminders, scores, recurring, tooling | 1 | none | config.py, radar.example.yaml | RULE-05, RULE-06, RULE-07, RULE-08, STATE-02, DIG-01 |
| 03-03 Digest schedule decision | 1 | none | digest_schedule.py | DIG-02, DIG-03 |
| 03-04 Reminders (pure) | 1 | none | reminders.py, messages.py | STATE-02 |
| 03-05 Score and recurring rules | 2 | 03-01, 03-02 | score_rules.py, recurring_rules.py | RULE-05, RULE-06, RULE-08 |
| 03-06 Tooling-gap rule and run-list completeness | 2 | 03-01 | tooling_rules.py, guard.py | RULE-07 |
| 03-07 doctor: digest and score-rule lines | 2 | 03-01, 03-02 | doctor.py, cli_doctor.py | DIG-02, RULE-05, RULE-06 |
| 03-08 Digest builder and text | 3 | 03-01, 03-04, 03-05, 03-06 | digest.py | DIG-01, DIG-02, DIG-03, DIG-04 |
| 03-09 Collect scores, tooling and the resolved log in the run | 3 | 03-01, 03-02, 03-05, 03-06 | pipeline.py, high_rules.py | RULE-05, RULE-06, RULE-07, DIG-04 |
| 03-10 Reminders in the run, runbook section | 4 | 03-01, 03-02, 03-04, 03-09 | pipeline.py, reminders.py | STATE-02 |
| 03-11 Weekly digest in the run, runbook and notes | 5 | 03-01, 03-02, 03-03, 03-08, 03-09, 03-10 | pipeline.py, cli.py | DIG-01, DIG-02, DIG-03, DIG-04, RULE-08 |

Same-wave plans touch disjoint files; `pipeline.py` is touched only by 03-09, 03-10 and 03-11, one per wave (3, 4, 5). No plan contains a checkpoint; operator steps (thresholds, preview, test-chat check) are text in `docs/deploy.md` (plans 03-10 and 03-11).

## Requirements

| ID | Covered by | Where it is true |
|---|---|---|
| RULE-05 | 03-01, 03-02, 03-05, 03-07, 03-09, 03-11 | ScoreRecord and the off-by-default threshold keys; `evaluate_scores`; collected every poll; a digest line (via 03-08) |
| RULE-06 | 03-01, 03-02, 03-05, 03-07, 03-09, 03-11 | same as RULE-05 for `audit.tests` |
| RULE-07 | 03-01, 03-02, 03-06, 03-09, 03-11 | ToolingRecord, window and audit-type keys, `evaluate_tooling` and `runs_complete`, collection, digest tier |
| RULE-08 | 03-02, 03-05, 03-11 | `recurring.after_runs`, `is_recurring` and `trailing_high_runs`, the marker in the digest |
| STATE-02 | 03-01, 03-02, 03-04, 03-10 | ReminderClock, `reminders.every_days`, `reminders_due` and `render_reminders`, the wired reminder step |
| DIG-01 | 03-02, 03-08, 03-11 | `digest.max_items`, ordering, cap and "+N more" in the text, sent once a week |
| DIG-02 | 03-03, 03-07, 03-08, 03-11 | schedule decision, doctor line, one-line all-clear, sent every week |
| DIG-03 | 03-03, 03-08, 03-11 | deferral while the poll failed; the poll time in every message |
| DIG-04 | 03-01, 03-08, 03-09, 03-11 | resolved log, resolved block in the text, tooling-gap tier |

## Decisions (03-CONTEXT.md)

| Decision | Covered by | Notes |
|---|---|---|
| D-01 weekday and hour in UTC, default Wednesday 05:00, first run at or after | 03-02, 03-03, 03-07, 03-11 | `digest.weekday`, `digest.hour_utc`; doctor shows it |
| D-02 grace window, retry inside it, skip after, ack-before-mark | 03-02, 03-03, 03-07, 03-11 | `digest.grace_hours` 1..168 (range is Claude's Discretion); doctor WARNs below the 3-hour timer |
| D-03 defer while Guard is unavailable, poll time in the text | 03-03, 03-08, 03-11 | a partial poll still sends and says how many projects were unread (flagged below) |
| D-04 everything open, including already-alerted items, one-line all-clear | 03-08, 03-11 | |
| D-05 order by type then age, tiers worst first | 03-08 | recurring tier realised as a marker (flagged below) |
| D-06 one message, `digest.max_items` 3..50 default 10, "+N more", escaping, gate, splitting | 03-02, 03-08, 03-11 | |
| D-07 resolved since the previous acknowledged digest, each in exactly one digest, counts only | 03-01, 03-08, 03-09, 03-11 | pending log bounded at 200, cleared on acknowledgement |
| D-08 score rules off until a threshold is set, per-project override, digest only, unknown never flags | 03-01, 03-02, 03-05, 03-07, 03-09, 03-11 | thresholds are an operator step in the runbook (plan 03-11) |
| D-09 recurring: N consecutive completed runs by task_id, default 3 (2..20), digest only, once per series | 03-02, 03-05, 03-08, 03-11 | |
| D-10 tooling gaps: four audit types, 60 days default, per-project override, digest only, paused and excluded skipped | 03-01, 03-02, 03-06, 03-09, 03-08 | evidence rule flagged below; the two ASSUMPTIONs carried into docs |
| D-11 reminders: open High series only, 7 days (1..90), clock from last alert or reminder, re-arm restarts it, one message per run, no cap | 03-01, 03-02, 03-04, 03-10 | baselined, never-alerted series are not reminded (flagged below) |

## Success criteria (ROADMAP.md Phase 3)

| SC | Covered by |
|---|---|
| SC1 low ai-readiness or tests score in the digest with the score, never an immediate alert | 03-05, 03-09, 03-08, 03-11 |
| SC2 recurring across N completed runs by task_id, N configurable | 03-02, 03-05, 03-08, 03-11 |
| SC3 tooling gap in the digest only | 03-06, 03-09, 03-08, 03-11 |
| SC4 weekly digest always sent, worst first, ages, "+N more", last poll time, resolved items and tooling gaps | 03-03, 03-08, 03-11 |
| SC5 reminder at the configured interval and not before | 03-04, 03-10 |

## Source audit

SOURCE | ID | Feature/Requirement | Plan | Status | Notes
--- | --- | --- | --- | --- | ---
GOAL | - | Group hears about weak AI readiness, weak tests, missing tooling and recurring High findings in one always-sent weekly digest, worst first, plus reminders | 03-01 to 03-11 | COVERED | |
REQ | RULE-05 | ai-readiness score below threshold | 03-05, 03-09 | COVERED | threshold off by default (D-08) |
REQ | RULE-06 | tests score below threshold | 03-05, 03-09 | COVERED | |
REQ | RULE-07 | tooling gap, digest only | 03-06, 03-09 | COVERED | |
REQ | RULE-08 | recurring across N runs by task_id | 03-05, 03-11 | COVERED | |
REQ | STATE-02 | reminders at an interval in days | 03-04, 03-10 | COVERED | |
REQ | DIG-01 | weekly digest worst first, ages, "+N more" | 03-08, 03-11 | COVERED | |
REQ | DIG-02 | always sent, one-line all-clear | 03-03, 03-08, 03-11 | COVERED | |
REQ | DIG-03 | time of the last successful poll | 03-08, 03-11 | COVERED | |
REQ | DIG-04 | resolved items and tooling gaps | 03-08, 03-09, 03-11 | COVERED | |
RESEARCH | - | No RESEARCH.md for Phase 3 (discovery level 0: patterns follow Phase 2 code) | - | COVERED | docs/guard-data-notes.md read; its open ASSUMPTIONs are carried, not settled |
CONTEXT | D-01 to D-11 | see the Decisions table | see above | COVERED | |
CONTEXT (Deferred) | - | Ops-chat notice for a skipped digest week | none | EXCLUDED | not decided; log line only |
CONTEXT (Deferred) | - | Score trend (DIG-05), reminders for stale projects, reminder caps, High history backfill | none | EXCLUDED | not built |
CONTEXT (Deferred) | - | Real thresholds and a check of the 60-day window against real cadences | none | EXCLUDED | operator steps and a later gap-closure step (runbook, plan 03-11) |

No row is MISSING.

## Interpretations the operator should confirm (none blocks execution)

1. D-05 and D-09 (recurring tier): a recurring series is by construction an open High series, so showing it once in its worst tier leaves a separate "recurring" tier empty. The digest keeps four tiers (High, stale, low score, tooling gap) and puts a "Recurring: High findings in each of the last N runs" marker on the High line (plan 03-08). Ranking recurring series below fresh High series would be a one-function change.
2. D-10 evidence rule: `list_audit_runs` returns only the newest runs, so a repository is flagged only when the radar read its whole run list or has watched it for `tooling_gaps.window_days`; otherwise the pair is undecided. For repositories with long histories the first tooling gaps appear up to 60 days after the first poll (plans 03-06, 03-11). ASSUMPTION, plus the existing one that `audit.implementation-unification` is the duplicate detector. A faster filtered lookup is parked in docs/ideas until the tool schema is checked.
3. D-03 partial poll: a poll where some projects could not be read still sends the digest and says how many were unread; only a failed poll (listing unknown, or every active project unknown) defers it.
4. D-11 baseline: a High series that was only baselined (never sent as an alert) is not reminded; it is in every weekly digest.
5. D-07 resolved wording: resolved items show the project name and kind (High findings closed in an audit type, no longer stale, now has runs); no finding titles or repository ids.
6. Config key names and ranges beyond the decided ones are Claude's Discretion: `digest.hour_utc`, `digest.grace_hours` 1..168, `scores.ai_readiness_below`, `scores.tests_below`, `tooling_gaps.window_days` 7..365, per-project `tooling_window_days`.
