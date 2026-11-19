# Phase 4: Claude Triage and Short Templates - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-11-19
**Phase:** 4-Claude Triage and Short Templates
**Areas discussed:** How Claude is reached, What Claude sees and delimiting, Typed output and rendering, Fallback and on/off equality, Cost log, Short templates
**Mode:** Three passes. First pass: no operator was available, each question was answered from the repo docs, the locked decisions and the operator's stated values. Second pass (same day): the operator answered the four open questions and widened the scope. Third pass (same day): the operator withdrew persisted titles, narrowed the Claude line, tied the cause language to `messages.language` and added reminders and T-0033. Choices marked "revised" or "superseded" replace earlier ones.

---

## How Claude is reached (TRI-05)

| Option | Description | Selected |
|--------|-------------|----------|
| Anthropic API SDK | Typed output built in, easy to fake | |
| Claude Agent SDK | Bundled binary, agent loop | |
| `claude -p` behind a port | Standard library subprocess, subscription login | ✓ (candidate) |
| T3 route | Only if a documented non-interactive interface exists | ✓ (only if verified) |

**Choice:** a swappable `Triager` port; `claude -p` is the candidate adapter, T3 only if verified.
**Notes:** The SDK options are ruled out by the operator's "no API key" choice (HANDOVER, REQUIREMENTS Out of Scope), which overrides the STACK.md recommendation. Observed locally: `claude --help` v2.1.322 lists `--json-schema`, `--output-format json`, `--tools`, `--max-budget-usd`, `--no-session-persistence`, `--permission-prompts`; `t3 --help` describes running T3 Code and shows no headless prompt command. Neither was called: no network, no credentials, and `claude -p` was "Not logged in" in this container.

| Option | Description | Selected |
|--------|-------------|----------|
| Assume `claude -p` works and build the adapter | Faster | |
| Verify first, as plan 04-01, operator-gated | Nothing assumed | ✓ |

**Choice:** verifying the mechanism is the first piece of Phase 4 work. Pure parts may proceed in parallel; no adapter before 04-01. If nothing verifies, ship the templates, port, fake, validation and fallback with triage off and record the blocker.

| Option | Description | Selected |
|--------|-------------|----------|
| Triage on by default once built | Feature visible at once | |
| Ships off, operator enables after a dry-run | Same send path when off | ✓ |

---

## Operator answers (second pass, 2026-11-19)

| Question | Answer | Recorded as |
|----------|--------|-------------|
| Q1: is unattended use of a Claude subscription login by the scheduled job allowed? | Yes | D-02 |
| Q2: which login, where is the credential? | Operator's own account; token from `claude setup-token` in `.env`, mode 0600, on the radar host | D-02, D-15 |
| Q3: generic cause only, or finding titles? | Send finding titles, sanitised (strip IDs, paths, links, code spans; length cap; delimited as data). Evidence, paths, report bodies and links never go. The parser must keep titles: Phase 4 work | D-05, D-06, D-07 |
| Q4: limits on Claude text? | Drop the summary. Claude returns priority and one short cause, one line, at most 60 characters, shown as `P1 · <cause>`. Amend TRI-01 | D-08, D-09, D-10 |
| Scope: shorter templates | Alert and digest shrink to one line per item, before the triage line is added; new requirement | D-04, ALERT-07 |

### Third pass (2026-11-19)

| Point | Answer | Recorded as |
|-------|--------|-------------|
| D-06 titles | Do NOT persist titles; no state schema 4, state stays schema 3; titles live in memory for the run only. Claude cause only on fresh High alerts read in this run; digest and re-armed alerts carry no cause | D-05, D-06 |
| D-05 scope | Confirmed: Claude line on High items only | D-05 |
| Cause language | Follows `messages.language` (en or ru, from T-0033); Russian cause when ru | D-09 |
| Reminders | Shorten too in Phase 4, same one-line shape; removed from Deferred | D-04 |
| Order | Phase 4 plans start after T-0033 (`messages.language` and the `texts.py` catalog) is merged; short templates are written into that catalog in both languages; T-0033 added to Phase 4 "Depends on" | domain, D-04 |
| Accepted | `state/triage-cost.json` as a separate file (not state schema); token via env per D-15 | D-14, D-15 |

T-0033 is not in the repo yet, so the key and catalog shape are an ASSUMPTION until it is merged.

**Second-pass choices the spec author had derived, and what the third pass did to them:**
- Triage covers High-findings items only: confirmed, and narrowed to fresh High alerts (first alert of a series, read in this run). A High item with no usable title stays rule-only.
- Sanitised titles stored in state schema 4: **superseded**; the operator chose memory only, so digests, reminders, re-armed and retried alerts carry no cause (plan 02-12 skips re-reading reports, which is why).
- The Phase 2 D-05 rule "counts only in the group" is amended: the group may read the Claude cause. The operator's example makes this explicit; recorded as an accepted risk.
- The "Seen by the radar since" sentence and the "Long-standing" prefix leave the line; one digest footer states that ages count from the radar's first sight.
- Tooling gaps become one line per repo, so the digest cap and the "N open" count lines, not (repo, audit type) pairs.
- Reminders keep the Phase 3 long text: **superseded**; reminders are shortened too.
- Cause in English: **superseded**; the cause follows `messages.language`.

---

## What Claude sees and delimiting (TRI-04)

| Option | Description | Selected |
|--------|-------------|----------|
| Code facts only, opaque ids (first pass) | Nothing hostile to delimit; weak cause | superseded |
| Code facts plus sanitised High finding titles, in memory only (revised twice) | Real cause on fresh High alerts, titles are untrusted data and delimited, nothing stored | ✓ |
| Persist sanitised titles in state schema 4 | Cause on digests and re-armed alerts | declined by the operator |
| Send titles unsanitised | Simplest | |

| Option | Description | Selected |
|--------|-------------|----------|
| Delimiters alone as the defence | Simple | |
| Data block with a per-call nonce, plus sanitising, fixed fields, typed output, id cross-check, escaping | Layers | ✓ |

**Notes:** Names of projects and repos are still not sent; names in messages come from code.

---

## Typed output and rendering (TRI-01, TRI-02)

| Option | Description | Selected |
|--------|-------------|----------|
| Summary, probable cause and priority (first pass) | Matches the old TRI-01 | superseded |
| Priority and one cause only, 60 characters (revised) | Operator's choice | ✓ |

| Option | Description | Selected |
|--------|-------------|----------|
| One bad item rejects the whole response | Simple | |
| Structural faults reject all; text faults reject only that item | Less waste, still safe | ✓ |

| Option | Description | Selected |
|--------|-------------|----------|
| Claude line on its own line under the alert item; none on digest or reminders | Matches the operator's target shape and the memory-only decision | ✓ |

**Notes:** Cause is plain text in the `messages.language` language, no link or handle, no digits (**ASSUMPTION**, revisit with live data). Digits stay banned because counts and ages come from code. The Claude line appears only on fresh High alerts, so there is no digest variant.

---

## Short templates (ALERT-07)

| Option | Description | Selected |
|--------|-------------|----------|
| Keep the Phase 3 wording and add the Claude line | No rewrite | |
| Shorten first: one line per item with a colour marker, then add the Claude line | Operator's target shape | ✓ |

**Choice:** 🔴 for High items, 🟡 for the rest, marker chosen by code; digest title `Radar weekly · N open`; reminders `Radar reminder · N open`; tooling gaps grouped per repo. Texts are catalog entries in en and ru (T-0033). Escape, split, secret gate and ack-before-mark unchanged. The old text tests are rewritten.

---

## Fallback and on/off equality (TRI-02, TRI-03)

| Option | Description | Selected |
|--------|-------------|----------|
| Triage fills the message before dedup | Cache by key | |
| Triage adds a line after reconcile and before delivery | Cannot touch keys, state or order | ✓ |

| Option | Description | Selected |
|--------|-------------|----------|
| Triage failure marks the run "attention" and tells the ops chat | Visible | |
| Log and `doctor` only; exit code and sends unchanged | No extra noise | ✓ |

---

## Cost log (TRI-05)

| Option | Description | Selected |
|--------|-------------|----------|
| Log line only | Simple, no daily total | |
| Log line per call plus a separate `state/triage-cost.json`, one record per UTC day | Daily total, dedup state untouched | ✓ |
| Put it in the main state file | One file | |

**Choice:** separate file, atomic write, 35 days kept; dry-run logs only and writes nothing. If the CLI reports no cost, log `unknown` and count calls and seconds.

| Option | Description | Selected |
|--------|-------------|----------|
| One call per item | Simple prompts | |
| One call per run for the fresh High alerts, batched, capped at 30 items; none for digest or reminders | Bounded cost | ✓ |

---

## Claude's Discretion

- System prompt wording, token variable name and CLI flags as 04-01 verifies, module names, compact line wording and the Russian catalog texts, repo form for same-audit-type alerts, how titles ride in memory, plan sizes and waves.

## Deferred Ideas

- A cause on digests, reminders, re-armed and retried alerts (needs persisted titles); ops-chat notice for repeated triage failure; daily budget cap; Claude lines on non-High items; failover adapter or in-run retry; cache across delivery retries; using Claude's priority for ordering; trend (DIG-05).

## Questions for the operator

None open. All questions are answered above (three passes).
