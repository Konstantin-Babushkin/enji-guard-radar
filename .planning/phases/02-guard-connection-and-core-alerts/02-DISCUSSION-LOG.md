# Phase 2: Guard Connection and Core Alerts - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-11-17
**Phase:** 2-Guard Connection and Core Alerts
**Areas discussed:** History for RULE-02/09, Radar login failure, Group visibility of security findings

Areas offered but not selected: timer / poll cost / ops chat (depends on deployment host and real data), probe / fixtures / alert text (routine for the planner).

---

## History for RULE-02/09

| Option | Description | Selected |
|--------|-------------|----------|
| Accumulate in state | Record High count and task_id per poll; age starts at first poll | ✓ |
| Backfill from older reports | Reconstruct streak from older run reports | |
| Accumulate, backfill once | Accumulate, seed age from older reports on the first run | |

**User's choice:** Accumulate in state
**Notes:** Age will look young for long-standing Highs; backfill deferred.

---

## Radar login failure

| Option | Description | Selected |
|--------|-------------|----------|
| Count as failed poll, then ops | Unknown poll, ops message after K failures, one recovery message | ✓ |
| Ops message immediately | Auth errors skip the K counter | |

**User's choice:** Count as failed poll, then ops

---

## Group visibility of security findings

| Option | Description | Selected |
|--------|-------------|----------|
| Yes, counts only | Group sees repo, audit type, High count, age; no evidence | ✓ |
| Counts, security via ops only | Security rises go to ops chat | |
| Decide later | Config flag, default off | |

**User's choice:** Yes, counts only

---

## Claude's Discretion

Probe/sanitization design, timer mechanism and K/M values, `auth` command UX and token storage.

## Deferred Ideas

Backfill of High-count history from older reports; Phase 1 open points carried forward.
