# Requirements: Enji Guard Radar

**Defined:** 2026-11-16
**Core Value:** Stakeholders learn about a real problem in Telegram, in a few lines and without false alarms, instead of finding out weeks later or never.

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases.

### Guard Source

- [ ] **SRC-01**: The radar connects to the Enji Guard MCP non-interactively and lists the projects in scope
- [ ] **SRC-02**: A `probe` command records real Guard responses as sanitized fixtures, so rules and tests are built on real data
- [ ] **SRC-03**: Guard responses are parsed strictly into typed snapshots with timezone-aware UTC times and an injected clock
- [ ] **SRC-04**: A failed, empty or partial Guard fetch for a project is recorded as "unknown" and never creates or resolves findings for that project
- [ ] **SRC-05**: Each project can be paused or excluded in config, so dormant projects are never alerted on
- [ ] **SRC-06**: A fixture-backed source lets the whole pipeline run with no network access

### Rules

- [ ] **RULE-01**: A project with no Guard run for N days is flagged stale. N has a default and per-project overrides, and the rule tells "never ran" and "Guard unreachable" apart from "stale"
- [ ] **RULE-02**: A high-risk finding that stays open past a threshold is flagged, with its age
- [ ] **RULE-03**: A new or worsening security finding is flagged, and the alert never carries raw evidence such as secrets
- [ ] **RULE-04**: A weak Guard harness metric, or a project with no `GUARD.md`, is flagged
- [ ] **RULE-05**: A weak Guard AI-readiness signal is flagged
- [ ] **RULE-06**: A weak Guard tests signal is flagged
- [ ] **RULE-07**: A project missing a dead-code finder, duplicate detector, security scanner or dependency scanner is flagged, as a digest item only
- [ ] **RULE-08**: A finding that stays open across N consecutive Guard runs is flagged as recurring. N is configurable, and runs are counted by Guard run id, not by radar polls
- [ ] **RULE-09**: A finding is considered resolved only after M clean Guard runs, so a flapping metric does not flip alerts on and off
- [ ] **RULE-10**: Every threshold lives in validated config with defaults and per-project overrides, and unknown config keys are rejected

### State and Dedup

- [ ] **STATE-01**: A finding already alerted and still open is not re-sent. Its identity is stable and independent of severity, counts or titles
- [ ] **STATE-02**: Unaddressed high-risk findings are re-sent as a reminder at a configurable interval in days
- [ ] **STATE-03**: State is saved atomically with a backup, and a corrupt state file halts the run and notifies the operator instead of resetting
- [ ] **STATE-04**: An alert is recorded as sent only after Telegram acknowledges it, so a failed send retries on the next run
- [ ] **STATE-05**: The first run for a project records a baseline and does not flood the group with historical findings
- [ ] **STATE-06**: Two overlapping runs cannot both send. A second instance exits cleanly
- [ ] **STATE-07**: A worsening finding (higher severity) re-arms its alert

### Alerts

- [ ] **ALERT-01**: An immediate Telegram alert is sent only for a new high-risk finding or a newly stale project. Everything else waits for the digest
- [ ] **ALERT-02**: Each message is concise (a few plain-language lines): project, what is wrong, how long, severity
- [ ] **ALERT-03**: Dynamic text is escaped for Telegram, and a message never exceeds the Telegram length limit (long output is split at item boundaries)
- [ ] **ALERT-04**: When many alerts would fire at once, they collapse into one summary message instead of a flood
- [ ] **ALERT-05**: Telegram errors are handled by class: rate limit with retry-after, group migrated to a new chat id, and bad token or removed bot reported to the operator
- [ ] **ALERT-06**: A dry-run mode prints what would be sent and writes no state and sends nothing

### Digest

- [ ] **DIG-01**: One weekly digest goes to the group, worst first, with each item's age and a "+N more" cap
- [ ] **DIG-02**: The digest is always sent, with a one-line all-clear when nothing is open, so a dead radar is visible
- [ ] **DIG-03**: The digest shows the time of the last successful Guard poll
- [ ] **DIG-04**: The digest carries resolved items and the analysis-tooling gaps

### Triage

- [ ] **TRI-01**: Claude adds a short summary, probable cause and priority for items that will be sent, and returns schema-validated output
- [ ] **TRI-02**: Claude cannot add, drop or suppress an alert. Counts, ages and names always come from code, never from Claude
- [ ] **TRI-03**: Invalid, refused or failed Claude output falls back to the rule-only message
- [ ] **TRI-04**: Raw security evidence is never sent to Claude. Untrusted finding text is delimited so it cannot steer the prompt
- [ ] **TRI-05**: Claude is reached through a swappable boundary that uses the CLI or T3, with no API-key dependency. Daily cost is logged

### Operations

- [ ] **OPS-01**: The radar runs unattended on a schedule with a one-shot command and a timer
- [ ] **OPS-02**: After K consecutive failed polls, one message goes to a separate ops chat, and one more when the radar recovers
- [ ] **OPS-03**: A `doctor` command checks Guard reachability, the Telegram token and the bot's membership in the chat
- [ ] **OPS-04**: Secrets come from env or `.env` only. The bot token never appears in logs, errors or alert text, and a final check blocks secret-looking strings before every send

## v2 Requirements

Deferred to a future release. Tracked but not in the current roadmap.

### Noise Control

- **NOISE-01**: A time-boxed silence file mutes a known finding, with a mandatory expiry and reason
- **NOISE-02**: Quiet hours hold non-urgent sends outside a time window
- **NOISE-03**: A stale project suppresses its other alerts (inhibition)

### Digest

- **DIG-05**: The digest shows week-over-week trend ("3 new, 2 resolved")

### Delivery

- **DLV-01**: Interactive acknowledgement in Telegram (needs a long-running process)

## Out of Scope

| Feature | Reason |
|---------|--------|
| Fixing findings or opening PRs | The radar observes and alerts only |
| Dashboard or web UI | Telegram is the interface |
| Claude deciding whether to alert | Rules decide, Claude only explains and prioritises |
| "All green" chatter | Silence means healthy. The weekly digest is the only scheduled message |
| Composite health score | Hides the cause and invites arguments over the formula |
| Per-developer alerts or metrics | The audience is executives on tech quality |
| Anomaly-based staleness | Unexplainable, and risks false alarms |
| Database or SQLite | File state is enough at this scale |
| Anthropic API SDK for triage | Operator chose the CLI or T3, with no API key |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| (filled in by the roadmapper) | | Pending |

**Coverage:**
- v1 requirements: 42 total
- Mapped to phases: 0
- Unmapped: 42 ⚠️

---
*Requirements defined: 2026-11-16*
*Last updated: 2026-11-16 after initial definition*
