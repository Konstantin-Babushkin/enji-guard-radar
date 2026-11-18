# Phase 3: Remaining Rules and the Digest - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-11-18
**Phase:** 3-Remaining Rules and the Digest
**Areas discussed:** Digest schedule and delivery, Digest content and format, Rules without real data, Reminders (STATE-02)

---

## Digest schedule and delivery

| Option | Description | Selected |
|--------|-------------|----------|
| Monday 09:00 | Start of the work week | |
| Friday 16:00 | End-of-week summary | |
| Config, no default | Digest off until the operator sets it | |
| Other: config + default Wed 11:00 UTC+6 | Operator's own wording | ✓ |

**User's choice:** Config with default Wednesday 11:00 UTC+6; timezone question answered "UTC only".
**Notes:** Two answers conflicted; resolved with the operator as config in UTC, default Wednesday 05:00 UTC (= 11:00 UTC+6), sent on the first timer run after that (06:00 UTC).

| Option | Description | Selected |
|--------|-------------|----------|
| IANA timezone in config | digest.timezone, default UTC | |
| UTC only | Simpler, no tzdata | ✓ |

**User's choice:** UTC only.

| Option | Description | Selected |
|--------|-------------|----------|
| Catch up on the first run | Send on the next successful run this week | |
| Skip, wait for next week | No late messages | ✓ |

**User's choice:** Skip a missed week. Follow-up: grace window to retry before skipping — 24 hours (recommended), end of the same week, or 6 hours.

| Option | Description | Selected |
|--------|-------------|----------|
| Send on old data with a note | Digest goes out, flags the stale poll | |
| Defer until a successful poll | Nothing sent while Guard is down | ✓ |

**User's choice:** Defer until a successful poll (inside the 24-hour window).

---

## Digest content and format

| Option | Description | Selected |
|--------|-------------|----------|
| All open items | Include items already alerted | ✓ |
| Only what the group has not seen | Shorter, no repeats | |

**User's choice:** All open items.

| Option | Description | Selected |
|--------|-------------|----------|
| By type, then by age | High, recurring, stale, low scores, tooling gaps | ✓ |
| By age only | Longest-open first | |

**User's choice:** By type, then by age.

| Option | Description | Selected |
|--------|-------------|----------|
| 10 total, configurable | digest.max_items, 3..50, one message | ✓ |
| 5 per section | Per-section caps | |

**User's choice:** 10 total, configurable.

| Option | Description | Selected |
|--------|-------------|----------|
| Closed since the previous digest | Each item appears once | ✓ |
| Closed in the last 7 days | Simpler, can repeat or lose items | |

**User's choice:** Closed since the previous digest.

---

## Rules without real data

| Option | Description | Selected |
|--------|-------------|----------|
| 50 as ASSUMPTION | Default threshold, revisit with data | |
| Off until the operator sets a threshold | No default, no false flags | ✓ |

**User's choice:** Off until the operator sets a threshold.

| Option | Description | Selected |
|--------|-------------|----------|
| Current score and run date | Deltas stay out | ✓ |
| Current and previous score | Needs score history in state | |

**User's choice:** Current score and run date.

| Option | Description | Selected |
|--------|-------------|----------|
| Recurring N = 3 (2..20) | ~3-6 weeks of unresolved High | ✓ |
| N = 5 | Less noise, slower | |

**User's choice:** N = 3, range 2..20.

| Option | Description | Selected |
|--------|-------------|----------|
| 60 days, configurable | Tooling-gap window | ✓ |
| 30 days | Faster, more false gaps | |

**User's choice:** 60 days, configurable.

---

## Reminders (STATE-02)

| Option | Description | Selected |
|--------|-------------|----------|
| 7 days (1..90) | Weekly pace | ✓ |
| 14 days | Less pressure | |
| Off until set | Safer, easy to forget | |

**User's choice:** 7 days, range 1..90.

| Option | Description | Selected |
|--------|-------------|----------|
| Separate message, one per run | Ack-before-mark path | ✓ |
| Only a mark in the digest | No separate message | |

**User's choice:** Separate message, one per run.

| Option | Description | Selected |
|--------|-------------|----------|
| Open High series only | STATE-02 wording | ✓ |
| High series and stale projects | Widens the requirement | |

**User's choice:** Open High series only.

| Option | Description | Selected |
|--------|-------------|----------|
| No cap while open | Interval is the throttle | ✓ |
| Max 3, then digest only | Less noise, series goes quiet | |

**User's choice:** No cap while open.

---

## Claude's Discretion

- State schema 3 layout, config key names, digest wording and line layout, resolved-line cap, how scores interact with the report skip rule, dry-run digest.

## Deferred Ideas

- Ops-chat notice for a skipped digest week; score trend (DIG-05); reminders for stale projects or capped reminders; High history backfill; real thresholds after 02-11 data.
