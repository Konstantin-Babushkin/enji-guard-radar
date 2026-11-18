# Phase 3: Remaining Rules and the Digest - Context

**Gathered:** 2026-11-18
**Status:** Ready for planning (thresholds stay off until real data exists; see D-08)

<domain>
## Phase Boundary

The group also hears about weak AI readiness, weak tests, missing analysis tooling and High findings that keep coming back, in one weekly digest that is always sent, worst first, plus reminders for what stays unaddressed. Rules decide; nothing here is an immediate alert except the reminder for an open High series.

In scope: RULE-05, RULE-06, RULE-07, RULE-08, STATE-02, DIG-01, DIG-02, DIG-03, DIG-04.
Out of scope: Claude triage (Phase 4), week-over-week trend (DIG-05, v2), per-finding identity.

Phase 3 depends on Phase 2 being merged (done 2026-11-18) and on plan 02-11 (operator's live check and first `probe`) for real thresholds. Planning may start before 02-11; the score thresholds ship off (D-08), so no guessed number reaches the group.
</domain>

<decisions>
## Implementation Decisions

### Digest schedule and delivery
- **D-01:** The digest has a configured weekday and hour in UTC only (no timezone key). Default Wednesday 05:00 UTC (= 11:00 UTC+6). The systemd timer already runs every 3 hours (`OnCalendar=*-*-* 00/3:00:00`), so the digest goes out on the first run at or after that moment (06:00 UTC with the current timer). Message times are shown in UTC. — **Reversibility:** reversible — config keys and one pure "is it due" function.
- **D-02:** A digest that is not acknowledged by Telegram within a grace window after its planned moment is skipped for that week; the radar then waits for the next week. Default window 24 hours, configurable. Inside the window a failed send is retried on each run, and it is marked sent only after Telegram acknowledges (same ack-before-mark rule as alerts). A skipped week is not made up later.
- **D-03:** If Guard is unavailable when the digest is due (the last successful poll is stale or the current poll failed), the digest is deferred until a successful poll, still inside the D-02 window. After the window the week is skipped. The digest never goes out on data from a failed poll. DIG-03 (time of the last successful Guard poll) is always in the text.

### Digest content and format
- **D-04:** The digest lists everything currently open, including items the group already saw as immediate alerts (open High series, stale projects), plus below-threshold scores, recurring series, analysis-tooling gaps, and items resolved since the previous digest. With nothing open it is a one-line all-clear (DIG-02) that still carries the last successful poll time.
- **D-05:** Order is "by type, then by age". Tiers, worst first: open High series (higher High count first), recurring series, stale projects, low scores, tooling gaps. Inside a tier, older first. Age uses the D-02 "Seen by the radar since" definition from Phase 2; the wording must keep it clear that older history is not known.
- **D-06:** One message with at most `digest.max_items` open items (default 10, range 3..50, configurable), then a "+N more" line (DIG-01). Phase 1 escaping, the secret gate and item-boundary splitting still apply as a safety net.
- **D-07:** "Resolved" means closed since the previous digest was acknowledged (a High series closed by the M-clean-runs rule, or a project that is no longer stale). Each resolved item appears in exactly one digest. Counts only, no finding titles (Phase 2 D-05 still holds).

### Rules without real data
- **D-08:** The score rules (RULE-05 `audit.ai-readiness`, RULE-06 `audit.tests`) ship **off**: the threshold keys default to unset and the rule does nothing until the operator sets a threshold (0..100, strictly below flags), with a per-project override. Digest only, never an immediate alert (SC1). A digest line shows the current score and the run date, nothing else; score deltas stay out (Guard notes: deltas are hints, not triggers; trend is DIG-05, v2). The latest completed run per repo and audit type is the one compared. A score the parser could not read ("unknown") never flags and never clears. The operator picks real thresholds from the first real `probe` data after 02-11. — **Reversibility:** reversible — config only.
- **D-09:** Recurring (RULE-08): a `(repo, audit type)` whose High count has not reached zero across N consecutive completed runs, counted by Guard `task_id` from the Phase 2 history (not by poll). Default N = 3, range 2..20, configurable. Appears in the digest only, in its own tier, once per series (a series is shown once, in its worst tier).
- **D-10:** Tooling gaps (RULE-07): a repo with no completed run of `audit.dead-code`, `audit.implementation-unification`, `audit.security` or `audit.dependency-hygiene` in the last 60 days (default, configurable, per-project override) is a gap for that audit type, shown in the digest only. **ASSUMPTION:** 60 days fits cadences not yet observed (only security and recon cadences are known); revisit after real data. **ASSUMPTION:** `audit.implementation-unification` is the duplicate detector (REQUIREMENTS RULE-07, still unconfirmed). Paused and excluded projects are skipped, as in every other rule.

### Reminders (STATE-02)
- **D-11:** Reminders cover only open High series (STATE-02 wording). Default interval 7 days (`reminders.every_days`, range 1..90), counted from the last alert or reminder sent for that series; a per-run rise that re-arms the alert (Phase 2 D-06) restarts the clock. A reminder is never sent earlier than the interval (SC5). All series due in one run are one separate message, marked "reminder" with each series' age, sent through the same ack-before-mark path. No cap on the number of reminders while the series stays open; the interval is the only throttle. Stale projects are not reminded (they live in the digest).

### Claude's Discretion
- State schema bump (schema 3) for: last digest sent time and week key, last alert-or-reminder time per series, latest score per `(repo, audit type)`. Reading schemas 1 and 2 must keep working; follow how schema 2 was added.
- Whether `audit.ai-readiness`/`audit.tests` reports are read when `high.audit_types` filters them out, and how the "skip already-read report" rule from plan 02-12 handles a task_id counted for High but not yet for score. The score rules need the newest report of those two audit types even when it has no High findings.
- Exact digest wording and line layout, the "resolved" line cap, and whether an item that is both recurring and High is shown with a marker in one tier (D-09 says once).
- Config key names and validation messages, beyond the ranges named above.
- The digest in dry-run (ALERT-06): print it with the same function the live path uses, send nothing, write no state.
- A log line when a week's digest is skipped (D-02/D-03). Whether the ops chat should also hear about a skipped week was not decided (see Deferred).

### Folded Todos
None.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project and phase scope
- `AGENTS.md` — standing brief: deterministic core, ports and fakes, secrets rules, `make verify`.
- `.planning/ROADMAP.md` — Phase 3 goal, success criteria SC1..SC5, dependency on Phase 2 data.
- `.planning/REQUIREMENTS.md` — RULE-05..08, STATE-02, DIG-01..04, ALERT-01..06; DIG-05 is v2.
- `.planning/PROJECT.md` — digest and tooling-gap intent (weekly digest, worst first).
- `.planning/STATE.md` — project position and open assumptions.

### Earlier decisions that still hold
- `.planning/phases/02-guard-connection-and-core-alerts/02-CONTEXT.md` — D-01 history by task_id, D-02 "Seen by the radar since", D-05 counts only in the group, D-06 per-run rise re-arm, RULE-09 debounce.
- `.planning/phases/02-guard-connection-and-core-alerts/02-RESEARCH.md` — defaults and cost notes for the poll.
- `docs/guard-data-notes.md` — report layout, score line, cadence observations, why finding ids and score deltas are not trusted.
- `docs/design/v0.md` — original design.
- `docs/deploy.md` and `deploy/systemd/enji-guard-radar.timer` — the 3-hour timer the digest rides on.
- `config/radar.example.yaml` — config shape, validation style and ranges to follow.

### External
No external specs.
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/enji_guard_radar/high_history.py` and `ledger.py` (`SeriesHistory`, `RunPoint`, `State`, schema 2): High history per series by `task_id`; the base for recurring (RULE-08), resolved items and reminder clocks.
- `src/enji_guard_radar/high_rules.py` (`HighSeries`, `in_scope`, `skip_report_for`, `evaluate_high`): pure rule shape to copy; `skip_report_for` is the "already counted" skip that scores must not break.
- `src/enji_guard_radar/guard_report.py` (`AuditReport.score`, `layout`, `ReportUnknown`): the score is already parsed as `int | None`; unknown reports already do not create findings.
- `src/enji_guard_radar/stale.py`, `messages.py`, `telegram.py`, `secret_guard.py`: stale evaluation, escaping and splitting, send with error classes, secret gate. The digest and reminder text go through the same escape, split and gate.
- `src/enji_guard_radar/health.py` and ops-chat sending (plan 02-07): ack-before-mark handling for a second chat; the digest uses the group chat only.
- `src/enji_guard_radar/pipeline.py`: where alerts are reconciled, sent and marked; the digest and reminder steps plug in after alerts.
- `src/enji_guard_radar/config.py` and `config/radar.example.yaml`: strict validation (unknown or duplicate keys exit 2), per-project overrides.

### Established Patterns
- Pure functions with table tests for rules and text; I/O behind small ports and faked in tests.
- Ack-before-mark for everything sent; `--now` clock injection; dry-run writes nothing and sends nothing.
- Counts only in group messages; no finding titles, paths or evidence.
- Every new threshold is a validated config key with a documented range and default.
- ASSUMPTION markers in code and docs for anything not verified against real Guard data.

### Integration Points
- Poll loop in `pipeline.py` (rules → alerts) gains: score collection, tooling-gap evaluation, recurring evaluation, a "digest due" check, a reminder check.
- State gains schema 3; `state_store.py` migration must keep reading schemas 1 and 2.
- `deploy/` timer needs no change (D-01 rides the 3-hour tick); `docs/deploy.md` gets the digest and reminders sections.
- `doctor` (plan 02-08) can report the digest config (weekday, hour, window) and whether score thresholds are set.
</code_context>

<specifics>
## Specific Ideas

- Digest day and hour were given as "Wednesday 11:00 UTC+6", which the operator confirmed as a UTC config value of Wednesday 05:00. Message times are in UTC.
- A project already known as stale or with open High must still appear every week; the digest is the one place that shows all open items with age.
</specifics>

<deferred>
## Deferred Ideas

- Tell the ops chat when a weekly digest was skipped (window passed, Guard down or Telegram failing). Not decided; the group would otherwise see silence. A log line is the minimum (see Discretion).
- Score trend and week-over-week counts (DIG-05, v2).
- Reminders for stale projects, or a cap on the number of reminders per series.
- Backfill of High history from older Guard reports (carried over from Phase 2).
- Setting real score thresholds and checking the 60-day tooling window against real cadences: a gap-closure step after 02-11 data exists.

### Reviewed Todos (not folded)
None.
</deferred>

---

*Phase: 3-Remaining Rules and the Digest*
*Context gathered: 2026-11-18*
