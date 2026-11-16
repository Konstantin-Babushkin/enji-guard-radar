# Feature Landscape

**Domain:** Engineering/tech-quality health monitor that alerts non-technical executives via Telegram (a "radar" over Enji Guard)
**Researched:** 2026-11-16
**Overall confidence:** MEDIUM. Alerting mechanics (dedup, repeat, resolved, silence, heartbeat) are well documented and HIGH. The executive-digest and exec-audience findings are synthesis from vendor material and practitioner posts (MEDIUM). Nothing here is validated against real Guard data, which is unavailable (see Gaps).

## Framing

The closest analogues are not one product but four families. Each contributes a different lesson.

| Family | Examples | What to borrow | What not to borrow |
|--------|----------|----------------|--------------------|
| Infra alert routers | Prometheus Alertmanager, Datadog, Nagios, Opsgenie | Dedup key, grouping, repeat interval, resolved notices, silences with expiry, flap damping | On-call paging, escalation chains to people, routing trees |
| Code/security scanners | SonarQube, Snyk, Dependabot | New-vs-existing split, time-boxed ignore (snooze), SLA aging and overdue escalation | Per-PR and per-developer notifications, fix PRs, dashboards |
| Engineering-intelligence | LinearB, Swarmia, Jellyfish | Executive-level framing, working-agreement style thresholds | Developer-level productivity metrics, dashboards, surveys |
| Heartbeat/cron monitors | Healthchecks.io, Cronitor | Absence detection ("no signal in N days"), grace periods | Ping URLs and per-job setup |

The radar is a small alert router with a scanner-style aging model and a heartbeat-style staleness rule, wrapped in a digest for executives. It is not a dashboard.

Source notes: Alertmanager behaviour is from the official config docs (HIGH). Snyk ignore semantics are from Snyk docs (HIGH). The Dependabot SLA escalation and weekly digest patterns come from a Kestra blueprint and a Claude agent template (LOW to MEDIUM, community material). Heartbeat behaviour comes from vendor blogs (MEDIUM). Product claims for LinearB, Swarmia and Jellyfish come from comparison blogs and are LOW. I found nothing authoritative on reopened-finding logic.

## Table Stakes

Features whose absence makes an exec group mute the bot within weeks. Every row maps to a stated Active requirement or is a direct prerequisite.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Stable dedup key (project + rule + subject) with persisted "last alerted" state | Alertmanager and every scanner collapse repeats by identity. A re-sent open finding is the fastest route to a muted bot | Low | Key design is the real work: the subject must be stable across Guard runs (finding id vs fingerprint). Depends on Guard data shape. Pure function plus file state |
| New-vs-open distinction in every alert | Scanners (Sonar "new code", Dependabot) separate "just appeared" from "still there". Execs need to know which it is | Low | Falls out of dedup. State per finding: `first_seen`, `last_seen`, `last_alerted`, `status` |
| Immediate alert only for new high-risk or newly stale | Matches the Active requirement. Infra tools do the same with severity-based routing (page vs ticket) | Low | Everything else goes to the weekly digest only. Make this a single explicit routing function so it is table-testable |
| Repeat/reminder on a fixed interval for unaddressed items | Alertmanager `repeat_interval` (default 4h in its docs). Without it a dedup system goes permanently silent on a real problem | Low | Choose days, not hours, for this audience. Reminder = same finding, flagged as reminder, with age. Depends on dedup state |
| Age on every open item ("open 23 days") | The core complaint is items sitting with no one chasing. Age is the actionable number. SLA-aging bots sort by days overdue | Low | Computed from `first_seen`, not from Guard's own timestamp unless Guard exposes a reliable one. Needs a decision on which clock wins |
| Weekly digest, worst first, with age | Active requirement. Dependabot and SLA digests group by severity and sort by overdue | Med | See Digest Design below. Must be one message, capped in length |
| Staleness rule: no Guard run in N days, per-project threshold with a default and override | Heartbeat monitors exist for exactly this. Alerts on absence, since there is no failed run to alert on | Low | Fixed N (decided). Clock source is the last completed run timestamp from Guard. Add an explicit "never run" case. Distinguish "Guard unreachable" from "project stale" (see Pitfalls note below) |
| Per-project threshold overrides in a config file | Required by the stale rule and expected for the rest (SLA-per-severity is standard in scanners) | Low | One checked-in config file: global defaults plus a per-project table. Validate at load with a typed model, fail loudly on typos |
| Resolved notice | Alertmanager `send_resolved`. Closes the loop for execs: "that thing is fixed" | Low | Design v0 already says "single resolved note". Fold into the next digest by default and not as an immediate message, to keep the channel quiet (see Differentiators) |
| Silence = healthy (no message when nothing is wrong) | Stated core value. Heartbeat tools and Alertmanager only speak on state change | Low | No "all clear" posts per run. The weekly digest is the only scheduled message and should still send when empty (see Digest Design) |
| Safe-failure when triage output is invalid | Active requirement. Rule text must stand alone | Low | Rule-only message template is the baseline. Claude only adds a short explanation, never gates sending |
| Bot self-health ("radar itself is broken") | Heartbeat lesson: a monitor that dies silently looks identical to a healthy system, and "silence means healthy" makes this worse | Med | Needs its own failure path: e.g. Guard poll failed N runs in a row produces one message to the group or an operator channel. Distinct from the project stale rule. Easy to forget and fatal if missed |
| Secrets kept out of logs and alert text | Constraint | Low | Redaction at the Telegram and log boundaries. Test with a fake token in a fixture |
| One message per run, concise, with a hard length cap | Telegram text limit is 4096 characters per message (ASSUMPTION: verify against the Bot API docs before coding). The audience mutes long messages first | Low | Truncate with "+N more" and point to the digest. Formatting is a pure function with table tests |

## Differentiators

Not expected by a user, but they are what makes this bot survivable in an executive group. Ordered roughly by value for this audience.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Recurring-finding detection (same finding returns across runs) | Names a failure mode generic scanners hide: "fixed" items that reappear. Directly named in the requirements | Med | Needs history, not a snapshot: per finding key keep a `reopened_count` and `last_resolved_at`. A finding that disappears and returns within a window is "recurring". Debounce: require absence for 1+ full runs before counting as resolved, otherwise a flaky scan creates false "recurrence". Reopen logic has no standard reference, so treat the exact definition as a design decision. Depends on stable dedup key and resolved tracking |
| Debounced resolution (hysteresis) | Prevents a finding that vanishes from one bad scan from producing "resolved" then "new" then "resolved" noise. Nagios flap detection and Datadog min-threshold exist for the same reason | Med | Require N consecutive clean runs before marking resolved. Also suppress the "new high-risk" alert if the same key was open within the last window. Shares state with recurring detection, so build together |
| Escalation by age (wording and cadence, not people) | Scanners escalate overdue items by days past SLA. Here: reminder at 7d, "chase" tone at 14d, etc. Makes unaddressed high risk visible without per-person paging | Med | Escalation only changes message prominence and reminder interval. There is no one to route to, since the audience is one group. Keep thresholds in config. Age tiers in the digest ("over 30 days") are cheaper than distinct escalation messages |
| Time-boxed silence/ack with a mandatory expiry | Snyk ignores are snooze-style with an expiry and a reason. Alertmanager silences are time-bound. Execs sometimes know about an issue and accept it for now | Med | See Silence/Ack Design. File-based entry, not interactive. The expiry is the point: acknowledged items return in the digest as "acknowledged until X" and re-alert on expiry. Silences never suppress the digest listing, only immediate alerts and reminders |
| Digest with trend delta vs last week ("3 new, 2 resolved, 1 worsened") | Execs want direction, not inventory. Cheap once state has history | Low-Med | One header line. Derived from the same state as dedup. No charts |
| Claude "why it matters" in one line plus a priority | Turns scanner jargon into an exec sentence. Triage is already in scope | Med | Output strictly schema-validated. Claude may reorder or prioritise within the rule-selected set, never add or drop items. Fixed max length per item |
| Worsening detection for security findings (new or severity raised) | Active requirement. Distinguishes "still open" from "got worse", which deserves an immediate alert even though the key is already deduped | Med | Dedup key stays the same, but the alert fingerprint includes severity. A severity change re-arms the alert. Table-test the re-arm cases |
| Grouped project line (one project, many findings, one line each) | Alertmanager grouping reduces notification count. Execs think in projects, not findings | Low | Format only. Group by project, then rule. Keep a "worst first" order |
| Quiet hours / delivery window for non-urgent messages | Design v0 flags it as an open question. Reminders and digests at 09:00 local, not 03:00 | Low | Immediate alerts may bypass it. Config only |
| "Evidence" line that makes alerts verifiable | Trust is the product. Each alert carries what was seen and when (last run date, count) so a false alarm is detectable on sight | Low | Rule output already carries evidence. Just render it |
| Dry-run / preview mode printing the message instead of sending | Lets the operator tune thresholds on real Guard data without spamming the group | Low | Likely free, since the Telegram boundary is faked in tests. High practical value during rollout |

## Anti-Features

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| Per-run "all green" heartbeat messages | Directly violates "silence means healthy" and trains people to ignore the bot | Weekly digest only. Optionally a short "no open items" line inside it |
| Interactive ack/silence through Telegram buttons or commands | Requires a long-running bot process (webhook or `getUpdates` loop), which conflicts with a scheduled unattended run and no database. Adds an auth model for who may ack | File-based silence entries edited by the operator. Revisit only if execs ask for it (log in `docs/ideas/`) |
| Dashboard, web UI, charts, images | Out of scope. Telegram is the interface | Text digest. Link to Guard if a URL exists |
| Per-developer, per-PR or per-author alerts and metrics | Out of scope, and the engineering-intelligence tools get blamed for exactly this | Project-level only. Never name individuals |
| Claude deciding whether to alert, or inventing findings | Violates the core design and the no-false-alarm constraint | Rules decide, Claude explains, schema-validated, fallback to rule text |
| "Usual cadence" or anomaly-based staleness | Rejected in Key Decisions. Unexplainable alerts cost trust | Fixed per-project N with an override |
| Alert on every severity and every rule immediately | Alert fatigue. The audience mutes the bot | Immediate only for new high-risk and newly stale. Everything else in the digest |
| Rule-by-rule user preferences, per-person subscriptions, routing trees | One group is the whole audience. Alertmanager-style routing is complexity with no consumer | One config file, one chat |
| Auto-remediation, fix PRs, ticket creation | Out of scope: observe and alert only | Report only |
| Long Claude-written narratives | Unverifiable, long, and a false-claim risk | One line per item, bounded length, with evidence from rules |
| Open-ended "scores" or composite health index | Executives will ask what it means and nobody can explain it, and Guard's own metrics already exist | Report Guard's own signals as they are (harness, AI readiness, tests) with the threshold that was crossed |
| Permanent (no-expiry) silences | Snyk shows "ignore forever" becomes forgotten risk. Alertmanager docs also flag silences expiring unnoticed | Require an expiry and a reason. Surface active silences in the digest |

## Specific Design Questions

### Alert dedup and suppression

Recommended model: one record per finding key with `first_seen`, `last_seen`, `last_alerted_at`, `last_alerted_severity`, `status` (open, resolved), `reopen_count`.

- Immediate alert fires only on: key not seen before AND high-risk; severity raised on an existing key; project crossed stale threshold for the first time.
- Everything else is state change recorded silently and shown in the digest.
- Suppression layers, cheapest first: dedup (same key) then silence (explicit, expiring) then quiet hours (delivery window). Alertmanager's inhibition (suppress B while A fires) is worth one narrow use: if a project is stale, suppress its other rule alerts, since the data is old. Complexity Low, and it removes a whole class of confusing double alerts.

### Severity and age escalation

Escalation changes tone and cadence, not recipients. Suggested ladder (thresholds in config, values to be tuned on real data):

| Age open | Behaviour |
|----------|-----------|
| New | Immediate alert if high risk, otherwise digest only |
| Past reminder interval | One reminder, labelled with age |
| Past a second threshold | Reminder labelled as overdue, interval unchanged or shorter |
| Silenced | No reminders, shown in digest as acknowledged until a date |

Resist more than two tiers. More tiers means more config and more explaining.

### Reminder cadence

Fixed interval per severity, in days, global default with per-project override. Alertmanager's lesson is that the repeat interval is a floor, not a clock: repeats can only fire when a run happens. State the effective cadence as "at most once per interval, evaluated per run". Daily runs with a 7-day high-risk reminder is a sensible starting point, flagged as an assumption until real data exists.

### Weekly digest design

- Always send, even when empty ("Nothing open"). A missing digest is indistinguishable from a dead bot. This also doubles as the bot-liveness signal for executives.
- Structure: headline counts and delta vs last week, then projects ordered by worst finding, then per item `severity, what, age`. Cap the items shown (the Kestra SLA bot caps at ten) and print "+N more".
- Include: newly resolved since last digest, active silences with expiry, projects that are stale.
- Exclude: anything already conveyed with no change and no age tier crossed is still listed (digest is the inventory), but collapse repeats by project.
- One message, under the Telegram length cap. If it cannot fit, trim lowest severity first.
- Unknown data is labelled unknown, not guessed (a guardrail seen in the Dependabot digest agent template).

### Resolved notices

Default: fold into the next digest as "resolved this week". Optional immediate resolved message only for items that were alerted immediately (high-risk or stale), so the group sees the loop close. Apply debounce (N clean runs) first. Do not send resolved notices for items that were never alerted.

### Per-project thresholds and overrides

One config file with `defaults` and `projects.<name>` overrides, validated by a typed model. Overridable: stale-days, reminder interval, high-risk age threshold, harness/AI-readiness/test minimums, `enabled` per rule, `muted` flag. Do not allow overriding the dedup key or message format. Reject unknown keys.

### Silence and ack

File-based entries: `project`, optional `rule` and `subject`, `reason`, `until` (required), `added_by`. Behaviour: suppress immediate alerts and reminders; keep listing in the digest with the expiry; re-alert at expiry if still open. Warn in the digest when a silence expires within the next week (Alertmanager guidance on expiry reminders). Where the file lives is open: `state/` is git-ignored, so decide whether silences are checked in (auditable) or local (simple). Recommend checked-in config so the reason and author are in git history.

### Concise executive-readable format

Pattern per alert, 3 to 4 lines:

```
HIGH  <project>: <plain-language problem>
Open 23 days (since 2026-10-24). <one-line why it matters>
Evidence: <metric/value, last scan date>
```

Rules: no jargon without a gloss, no rule ids in the headline, age and evidence always present, severity as a word and not just an emoji (accessibility and muted-notification previews), at most one emoji marker per line if any, no links unless Guard supplies a stable URL. The rule-only fallback uses the same template with the Claude line omitted.

### Staleness detection

Table-stakes pattern from heartbeat tools: alert on absence, with a grace period, using a fixed expected window. Here: `now - last_run_at > N days` per project. Edge cases to design for: project never scanned, last run failed vs succeeded (a failing scan is still "ran" unless Guard says otherwise, to be decided from real data), clock source and timezone, and Guard returning stale data itself. Also the meta-case: Guard unreachable must not look like every project going stale at once.

### Recurring-finding detection

The same finding returning across runs, not simply persisting. Needs history and a stable identity. Two readings exist and the requirement should pick one: (a) finding is resolved, then reappears (regression), or (b) finding appears in K of the last M runs while flipping. Recommend (a) with debounced resolution, plus a count shown in the message ("came back 3 times"). Treat as a differentiator because no mainstream scanner surfaces it prominently and there is no standard reference for it.

## Feature Dependencies

```
Guard snapshot model (needs real Guard data)
  -> stable finding identity / dedup key
       -> persisted finding state (first_seen, last_alerted, status)
            -> new-vs-open distinction
            -> age computation
                 -> reminder cadence
                      -> age-based escalation
                 -> weekly digest (worst first, with age)
                      -> digest delta vs last week
            -> debounced resolution
                 -> resolved notices
                 -> recurring-finding detection
            -> silence/ack (expiry) -> digest "acknowledged" section
  -> severity-aware re-arm -> worsening security alerts
  -> last_run_at -> stale rule -> (inhibits other rules for that project)

Per-project config file -> every threshold above
Rule-only message template -> Claude triage (fallback depends on it)
Triage schema validation -> Claude "why it matters" line
Poll-failure tracking -> bot self-health message
Telegram boundary fake -> dry-run/preview mode
```

## MVP Recommendation

Build in this order so each step is shippable and testable with fakes:

1. Snapshot model and finding identity (blocked on Guard data), finding state file, dedup and new-vs-open.
2. Per-project config with defaults and overrides.
3. Stale rule and the immediate-alert routing for new high-risk and newly stale; rule-only message template with age and evidence.
4. Reminder cadence and the weekly digest (always sends, worst first, capped).
5. Bot self-health (poll-failure message) and dry-run mode. Do this before the first live send.
6. Claude triage with schema validation and fallback.
7. Remaining rules (security with worsening re-arm, harness, AI readiness, tests).
8. Debounced resolution, resolved notices and recurring-finding detection.
9. Time-boxed silence file.

Defer:
- Interactive ack through Telegram: needs a long-running process. Log in `docs/ideas/`.
- Quiet hours: cheap but depends on knowing the schedule, which is an open question.
- Multi-tier escalation beyond one reminder and one overdue tier: add only if real data shows items still ignored.
- Digest trend deltas: nice but only after the digest is trusted.

Note that recurring-finding detection is a stated requirement even though it is listed late here. It is late because it needs history and debounce state, not because it is optional. Roadmap should keep it in v1.

## Gaps and Assumptions

- ASSUMPTION: Guard exposes a stable finding identifier or fingerprint, a last-run timestamp, and a severity or risk field. If not, dedup keys must be derived and recurring detection becomes harder. Verify on first Guard connection.
- ASSUMPTION: Telegram message cap of 4096 characters and MarkdownV2 or HTML escaping rules. Verify against Bot API docs.
- No authoritative source found for reopened-finding or recurrence detection. The definition above is a design recommendation, not an industry standard.
- No evidence found on how real executive groups respond to specific cadences. Reminder and digest intervals are starting guesses and should be exposed as config, tuned in dry-run mode.
- Vendor claims about LinearB, Swarmia and Jellyfish alert features are from secondary sources. They informed framing only, not requirements.

## Sources

- Prometheus Alertmanager configuration (grouping, repeat_interval, send_resolved, inhibition): https://prometheus.io/docs/alerting/latest/configuration/ (HIGH)
- Alertmanager routing and silence hygiene write-ups: https://www.besthub.dev/articles/alertmanager-alert-governance-grouping-inhibition-silencing-and-tiered-notifications-8a4b4ab53912 , https://oneuptime.com/blog/post/2026-02-09-grafana-unified-alerting-silences/markdown (MEDIUM)
- Snyk ignore semantics (expiry, reasons, resurfaces when fix available): https://docs.snyk.io/manage-risk/prioritize-issues-for-fixing/ignore-issues (HIGH)
- SonarQube new-code concept: https://docs.sonarsource.com/sonarqube-server/user-guide/about-new-code (HIGH)
- Dependabot SLA escalation to Slack (Kestra blueprint): https://kestra.io/blueprints/github-dependabot-sla-escalation-sentinel (LOW to MEDIUM)
- Dependabot weekly digest agent template: https://21st.dev/community/agents/serafimcloud/dependabot-digest (LOW)
- UK Ministry of Justice GitHub alert SLA standard: https://developer-portal.service.justice.gov.uk/github/github-alert-management-sla-standard (MEDIUM, day counts not verified)
- Heartbeat / dead-man's-switch monitoring: https://cronitor.io/heartbeat-monitoring , https://crontap.com/blog/dead-man-switch-explained-for-developers (MEDIUM, vendor sources)
- Nagios flap detection: https://support.nagios.com/kb/article/nagios-core-detection-and-handling-of-state-flapping-378.html (HIGH for Nagios behaviour)
- Datadog flapping reduction: https://docs.datadoghq.com/monitors/faq/how-do-i-reduce-alert-flapping-noise (HIGH)
- Engineering-intelligence platform comparisons (LinearB, Swarmia, Jellyfish): https://tianpan.co/forum/t/jellyfish-vs-linearb-vs-dx-vs-swarmia-what-we-learned-evaluating-engineering-intelligence-platforms/312 , https://linearb.helpdocs.io/article/xvt2034qnh-engineering-health-dashboard (LOW)
- Project context: `/home/factory/.t3/projects/enji-guard-radar/.planning/PROJECT.md`, `/home/factory/.t3/projects/enji-guard-radar/docs/design/v0.md`
