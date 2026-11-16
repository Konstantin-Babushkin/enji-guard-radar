# Requirements: Enji Guard Radar

**Defined:** 2026-11-16
**Core Value:** Stakeholders learn about a real problem in Telegram, in a few lines and without false alarms, instead of finding out weeks later or never.

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases.

### Guard Source

- [ ] **SRC-01**: The radar connects to the Enji Guard MCP non-interactively and lists the projects in scope
- [ ] **SRC-02**: A `probe` command records real Guard responses as sanitized fixtures, so rules and tests are built on real data
- [ ] **SRC-03**: Guard responses (JSON lists and Markdown audit reports) are parsed strictly into typed snapshots with timezone-aware UTC times (fraction of 3, 4 or 6 digits) and an injected clock. Only runs with `status == completed` and `report_status == ready` count. Reports are read page by page until `has_more` is false. An unparseable report is "unknown", never "no findings"
- [ ] **SRC-04**: A failed, empty or partial Guard fetch for a project is recorded as "unknown" and never creates or resolves findings for that project. "Empty" means a blank, truncated or malformed response, or a page that was not fetched. A well-formed answer with zero items (for example a project with no repositories) is a real answer: it is reported as "never ran" (RULE-01), not "unknown". Clarified 2026-11-16
- [ ] **SRC-05**: Each project can be paused or excluded in config, so dormant projects are never alerted on
- [ ] **SRC-06**: A fixture-backed source lets the whole pipeline run with no network access

### Rules

- [ ] **RULE-01**: A project whose newest completed Guard run (any audit type, any of its repos) is older than N days is flagged stale. N has a default and per-project overrides, and the rule tells "never ran" and "Guard unreachable" apart from "stale"
- [ ] **RULE-02**: A `(repo, audit type)` that has had at least one High finding in every completed run for longer than a threshold is flagged, with its age (days since the first such run)
- [ ] **RULE-03**: A rise in the number of High findings in `audit.security` (or `audit.public-api-access-safety`) between two completed runs is flagged, and the alert never carries raw evidence such as secrets
- [ ] ~~**RULE-04**~~: Dropped 2026-11-16. Guard has no harness metric and no file-presence check (the guidance file is `ENJI_GUARD.md`); the AI-readiness score (RULE-05) is the signal
- [ ] **RULE-05**: The `audit.ai-readiness` score (0 to 100) below a threshold is flagged
- [ ] **RULE-06**: The `audit.tests` score (0 to 100) below a threshold is flagged
- [ ] **RULE-07**: A repo with no recent completed run of `audit.dead-code`, `audit.implementation-unification`, `audit.security` or `audit.dependency-hygiene` is flagged, as a digest item only (mapping of "duplicate detector" to `audit.implementation-unification` is unconfirmed)
- [ ] **RULE-08**: A `(repo, audit type)` whose High count has not reached zero across N consecutive completed runs is flagged as recurring. N is configurable, and runs are counted by Guard `task_id`, not by radar polls
- [ ] **RULE-09**: A `(repo, audit type)` is considered clear only after M consecutive completed runs with zero High findings, so a flapping result does not flip alerts on and off
- [ ] **RULE-10**: Every threshold lives in validated config with defaults and per-project overrides, and unknown config keys are rejected

### State and Dedup

- [ ] **STATE-01**: A condition already alerted and still true is not re-sent. Guard finding ids are not stable between runs (`F-006` in one run, a slug in another), so the radar tracks the number of High findings per `(repo, audit type)` and does not match individual findings. The dedup key is `(repo, audit type, level)`. Decided 2026-11-16; per-finding matching is a possible later addition (see `docs/guard-data-notes.md`)
- [ ] **STATE-02**: Unaddressed high-risk findings are re-sent as a reminder at a configurable interval in days
- [ ] **STATE-03**: State is saved atomically with a backup, and a corrupt state file halts the run and notifies the operator instead of resetting
- [ ] **STATE-04**: An alert is recorded as sent only after Telegram acknowledges it, so a failed send retries on the next run
- [ ] **STATE-05**: The first run for a project records a baseline and does not flood the group with historical findings
- [ ] **STATE-06**: Two overlapping runs cannot both send. A second instance exits cleanly
- [ ] **STATE-07**: A rise in the High count for a `(repo, audit type)` re-arms its alert

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
| SRC-01 | Phase 2 | Pending |
| SRC-02 | Phase 2 | Pending |
| SRC-03 | Phase 1 | Pending |
| SRC-04 | Phase 1 | Pending |
| SRC-05 | Phase 1 | Pending |
| SRC-06 | Phase 1 | Pending |
| RULE-01 | Phase 1 | Pending |
| RULE-02 | Phase 2 | Pending |
| RULE-03 | Phase 2 | Pending |
| RULE-05 | Phase 3 | Pending |
| RULE-06 | Phase 3 | Pending |
| RULE-07 | Phase 3 | Pending |
| RULE-08 | Phase 3 | Pending |
| RULE-09 | Phase 2 | Pending |
| RULE-10 | Phase 1 | Pending |
| STATE-01 | Phase 1 | Pending |
| STATE-02 | Phase 3 | Pending |
| STATE-03 | Phase 1 | Pending |
| STATE-04 | Phase 1 | Pending |
| STATE-05 | Phase 1 | Pending |
| STATE-06 | Phase 1 | Pending |
| STATE-07 | Phase 2 | Pending |
| ALERT-01 | Phase 2 | Pending |
| ALERT-02 | Phase 1 | Pending |
| ALERT-03 | Phase 1 | Pending |
| ALERT-04 | Phase 1 | Pending |
| ALERT-05 | Phase 1 | Pending |
| ALERT-06 | Phase 1 | Pending |
| DIG-01 | Phase 3 | Pending |
| DIG-02 | Phase 3 | Pending |
| DIG-03 | Phase 3 | Pending |
| DIG-04 | Phase 3 | Pending |
| TRI-01 | Phase 4 | Pending |
| TRI-02 | Phase 4 | Pending |
| TRI-03 | Phase 4 | Pending |
| TRI-04 | Phase 4 | Pending |
| TRI-05 | Phase 4 | Pending |
| OPS-01 | Phase 2 | Pending |
| OPS-02 | Phase 2 | Pending |
| OPS-03 | Phase 2 | Pending |
| OPS-04 | Phase 1 | Pending |

**Coverage:**
- v1 requirements: 41 total (RULE-04 dropped)
- Mapped to phases: 41 (Phase 1: 17, Phase 2: 10, Phase 3: 9, Phase 4: 5)
- Unmapped: 0

---
*Requirements defined: 2026-11-16*
*Last updated: 2026-11-16 after roadmap creation*
