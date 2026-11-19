# API Coverage - Phase 4 (Short templates and Claude triage: the `claude -p` CLI as the only new external call; Telegram and Guard calls unchanged)

> Full coverage by default. Opt-outs are explicit, reasoned decisions.
> Phase 4 adds no Guard tool and no Telegram method. It adds one external call: the Claude Code CLI in print mode (`claude -p`), at most once per run, behind the `Triager` port (plans 04-01, 04-13). No API key and no new package.

| capability | decision | reason |
|---|---|---|
| guard.get_audit_report (finding titles, in memory) | INTEGRATE | |
| telegram.sendMessage to the group: short alerts, reminders, digest | INTEGRATE | |
| claude CLI `-p` with `--output-format json` and `--json-schema` | INTEGRATE | |
| claude CLI `--max-budget-usd` | INTEGRATE | only if plan 04-01 observes it is honoured (D-13) |
| claude CLI with tools, sessions, project settings, MCP | OPT-OUT | explicitly out of scope by design: disabled per call, empty temp cwd (D-15) |
| Anthropic API SDK, Claude Agent SDK, API key | OPT-OUT | explicitly out of scope by design: operator chose CLI or T3 with no API key (REQUIREMENTS Out of Scope) |
| T3 headless route | OPT-OUT | not verified: built only if plan 04-01 finds a documented non-interactive entry point (D-02) |
| Claude on digests, reminders, re-armed and retried alerts | OPT-OUT | not decided: needs persisted titles; operator declined (04-CONTEXT Deferred) |
| ops-chat notice for repeated triage failure, daily budget cap | OPT-OUT | not decided: Deferred in 04-CONTEXT; log line and doctor only |

# Requirement, decision and success-criterion coverage - Phase 4

## Plan table

| Plan | Wave | Depends on | Files (source) | Requirements |
|---|---|---|---|---|
| 04-01 Claude CLI route check (operator) | 1 | none | docs/triage-notes.md | TRI-05 |
| 04-02 Short alert templates | 1 | none | texts.py, messages.py | ALERT-07 |
| 04-03 Parser keeps titles in memory | 1 | none | guard_report.py | TRI-04 |
| 04-04 sanitize_title and pick_titles | 1 | none | titles.py | TRI-04 |
| 04-05 Triage config and token setting | 1 | none | config.py, radar.example.yaml, settings.py | TRI-03, TRI-05 |
| 04-06 Short reminder template | 2 | 04-02 | texts.py, reminders.py | ALERT-07 |
| 04-07 Triage port, models, request and prompt | 2 | 04-04 | triage.py, ports.py | TRI-01, TRI-04, TRI-05 |
| 04-08 Reply validation | 3 | 04-07 | triage.py | TRI-01, TRI-02, TRI-03 |
| 04-09 Short digest template, grouped tooling gaps | 3 | 04-02, 04-06 | texts.py, digest.py | ALERT-07 |
| 04-10 Render seam for the Claude line | 3 | 04-02, 04-07 | messages.py | TRI-01, TRI-02, TRI-03 |
| 04-11 Cost record and cost file | 3 | 04-07 | triage_cost.py, triage_cost_store.py | TRI-05 |
| 04-12 Triage in the run (fake triager) | 4 | 04-03, 04-04, 04-05, 04-07, 04-08, 04-10, 04-11 | pipeline.py | TRI-01..TRI-05 |
| 04-13 `claude -p` adapter and CLI wiring | 5 | 04-01, 04-12 | claude_cli_triager.py, cli.py | TRI-03, TRI-05 |
| 04-14 On/off matrix and hostile-text tests | 5 | 04-12 | tests only | TRI-02, TRI-03, TRI-04 |
| 04-15 doctor line and runbook | 6 | 04-05, 04-11, 04-12, 04-14 (after 04-13 when it runs) | doctor.py, cli_doctor.py, docs/deploy.md | TRI-05, ALERT-07 |

Same-wave plans touch disjoint files. `texts.py` is edited by 04-02, 04-06, 04-09 (waves 1, 2, 3); `messages.py` by 04-02 and 04-10 (waves 1, 3); `triage.py` by 04-07 and 04-08 (waves 2, 3); `tests/golden/dry_run_default.txt` by 04-02 and 04-09 (waves 1, 3); `tests/test_architecture.py` only by 04-11 (it lists both new shell modules). Every plan ends with `make verify` green, so the tree is green after each plan.

Only 04-01 needs the operator. The pure parts and the pipeline integration (04-02..04-12, 04-14, 04-15) do not wait for it; 04-13 does. If 04-01 ends with `Route verified: no`, 04-13 is not executed, triage stays off, and ROADMAP Gates records "Phase 4 SC5: no API-key route verified" (D-02 gate).

No research file: discovery level 0 (patterns follow Phase 2 and 3 code; the one external unknown, the CLI, is settled by plan 04-01 on the real host, not by desk research).

## Requirements

| ID | Covered by | Where it is true |
|---|---|---|
| ALERT-07 | 04-02, 04-06, 04-09, 04-15 | one-line alerts and summaries, short reminders, short digest with grouped tooling gaps, all in en and ru; runbook shapes |
| TRI-01 | 04-07, 04-08, 04-10, 04-12 | typed ItemTriage (priority, cause <= 60 chars, no summary), `P1 · <cause>` line, fresh High alerts only, cause in messages.language |
| TRI-02 | 04-08, 04-10, 04-12, 04-14 | ids cross-checked; render seam cannot change order, keys or collapse; on/off matrix |
| TRI-03 | 04-05, 04-08, 04-10, 04-12, 04-13, 04-14 | every failure reason and invalid reply gives the rule-only text; ships off |
| TRI-04 | 04-03, 04-04, 04-07, 04-12, 04-14 | titles in memory only, sanitised; fixed field list; nonce-delimited JSON data block; hostile-text suite |
| TRI-05 | 04-01, 04-05, 04-07, 04-11, 04-12, 04-13, 04-15 | Triager port, CLI adapter with no API key, per-call and daily cost lines, cost file, doctor line |

## Decisions (04-CONTEXT.md)

| Decision | Covered by | Notes |
|---|---|---|
| D-01 one Triager port, adapters behind it, no SDK, stdlib subprocess | 04-07, 04-13 | backend Literal["claude_cli"] in 04-05 |
| D-02 verify the route first, operator-gated, gate on failure | 04-01, 04-13 precondition | |
| D-03 ships off | 04-05, 04-12 | no adapter import with triage off (04-13 row) |
| D-04 short templates in the catalog, en and ru | 04-02, 04-06, 04-09, 04-15 | repo shown only when ambiguous (flagged below) |
| D-05 fresh High alerts only, fixed request fields | 04-07, 04-12, 04-14 | |
| D-06 titles in memory only, sanitize_title, state schema 3 | 04-03, 04-04, 04-12 | hex-run rule needs a digit (flagged below) |
| D-07 fixed system prompt, nonce data block, controls in order, hostile test | 04-07, 04-08, 04-14 | |
| D-08 strict flat models, no summary, generated schema | 04-07 | |
| D-09 two-level validation, 60 characters, no digits, language | 04-08 | |
| D-10 pure render, own line, 3-space indent, escaped | 04-02 (catalog entry), 04-10, 04-15 (indent check) | |
| D-11 typed failure reasons, one broad except at the boundary, no retry, no exit-code change | 04-07, 04-12, 04-13 | |
| D-12 triage after reconcile and before delivery; on/off matrix | 04-12, 04-14 | |
| D-13 one call per run, item cap, timeout, budget flag, model | 04-05, 04-12, 04-13 | |
| D-14 call line, daily line, separate cost file, dry-run writes none | 04-11, 04-12, 04-13 | |
| D-15 token from env, redacted, allowlisted env, temp cwd, stdin | 04-05, 04-13 | |
| D-16 doctor line, runbook, example config | 04-05, 04-15 | |

## Success criteria (ROADMAP.md Phase 4)

| SC | Covered by |
|---|---|
| SC1 short one-line templates in both languages; escaping, splitting, secret gate unchanged | 04-02, 04-06, 04-09 |
| SC2 `P1 · <cause>` on fresh High alerts only, typed and validated, none on digests, reminders, re-armed, retried | 04-07, 04-08, 04-10, 04-12 |
| SC3 same alerts on or off; every failure rule-only | 04-12, 04-14 |
| SC4 only sanitised in-memory titles reach Claude; hostile text cannot steer | 04-03, 04-04, 04-07, 04-14 |
| SC5 swappable boundary with no API key; daily cost logged | 04-01, 04-07, 04-11, 04-13, 04-15 |

## Source audit

SOURCE | ID | Feature/Requirement | Plan | Status | Notes
--- | --- | --- | --- | --- | ---
GOAL | - | One short line per item in en and ru; a fresh High alert can carry `P1 · <cause>`; same alerts as rules alone | 04-01 to 04-15 | COVERED | |
REQ | ALERT-07 | short templates | 04-02, 04-06, 04-09 | COVERED | |
REQ | TRI-01 | priority and cause, validated | 04-07, 04-08, 04-10, 04-12 | COVERED | |
REQ | TRI-02 | cannot add, drop or suppress | 04-08, 04-10, 04-14 | COVERED | |
REQ | TRI-03 | fallback | 04-08, 04-12, 04-14 | COVERED | |
REQ | TRI-04 | no evidence, delimited | 04-04, 04-07, 04-14 | COVERED | |
REQ | TRI-05 | swappable, no API key, daily cost | 04-01, 04-11, 04-13 | COVERED | live route is operator-gated (04-01) |
CONTEXT | D-01 to D-16 | see the Decisions table | see above | COVERED | |
CONTEXT (Deferred) | - | cause on digests and re-armed alerts, ops notice, budget cap, failover, cache, priority ordering, DIG-05 | none | EXCLUDED | not built |

No row is MISSING.

## Interpretations the operator should confirm (none blocks execution)

1. Repo name in a High line is shown only when two items of one rendered set share project and audit type (04-02, 04-06, 04-09); across two separate runs the same project-and-audit pair in two repos would not be disambiguated unless both are in one set.
2. sanitize_title drops a hex run of 7+ characters only if it contains a digit, so words like "defaced" survive (04-04); tokens with a file-extension tail are dropped, which also drops "e.g.".
3. Low-score digest line `🟡 <b>Project</b> · ai-readiness 40/100 · 8d`, age from the run to the poll; resolved lines `<b>Project</b> · security closed` with no marker (04-09).
4. Default `triage.model: haiku` until 04-01 picks one (04-05).
5. A corrupt `state/triage-cost.json` is logged and overwritten, never halting the run (04-11).
6. The Russian wording of every new catalog entry is the executor's; the operator reviews it in the golden and language tests.
