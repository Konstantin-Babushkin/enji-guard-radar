# Guard data notes

What the Guard MCP actually returns, observed on 2026-11-16 with read-only calls
(`list_projects`, `list_repositories`, `list_audits`, `list_audit_runs`,
`get_audit_report`) on 4 projects and about 6 repos. Raw output is not stored in
git. `fixtures/guard/` holds synthetic fixtures in the same shape (placeholder
names, ids, paths and SHAs; no real findings).

Tags: **OBSERVED** = seen in real responses. **ASSUMPTION** = not verified; ask
or check before relying on it.

## Tools the radar may use (read only)

`list_projects`, `list_repositories`, `list_audits`, `list_audit_runs`,
`get_audit_report`. `get_audit` returns the Fleet execution status of one task
(not called yet). `run_audit` starts an audit and is **never** used by the radar
(scope `audits:run`).

## Hierarchy and identity

- Project: UUID `id` and `name`. OBSERVED: 16 projects; one has 0 repos.
- Repository: `id` is an opaque string `repo_<uuid>` (not a GitHub id or path),
  plus `project_id`, `name` (`owner/repo`), `provider` (`github` and `gitlab`
  both OBSERVED), `connected` (bool; only `true` seen).
- Run: one audit type on one repo, identified by `task_id` (UUID). There is no
  repo-wide "scan" id. Several audit types are started within seconds of each
  other (a batch), but nothing links them except close `created_at` values.
- Audit type: `action_key`, for example `audit.security`, `audit.ai-readiness`,
  `audit.tests`, `audit.dead-code`, `audit.dependency-hygiene`,
  `audit.tech-health`, `audit.recon`, `audit.cicd`, `audit.config-hygiene`,
  `audit.cognitive-debt`, `audit.implementation-unification`,
  `audit.complexity-concentration`. The catalog (`list_audits`) had 18 entries
  for one repo, all billing `included`. Run history can contain keys that the
  catalog of the same repo lacks (for example `audit.web-pentest` appeared in
  one repo's history); hidden or unpublished audits keep history (tool doc).
  The radar must not assume a fixed key set.
- Run page URL: `report_url` (`.../<report_group>?run=<task_id>`). `report_group`
  differs from `action_key` (`audit.security` -> `vulns`, `audit.recon` ->
  `map`).

## Pagination

All list tools: `limit` (default 25, max 50; `list_audit_runs` default 5, max 10),
`cursor`, response `has_more` + `next_cursor`. The cursor is opaque (base64 JSON
today; do not parse it). Tool docs say to continue while `has_more` is true
**even after an empty page**. A cursor is only valid for the same tool and
filters. OBSERVED: one project has more than 25 repos; run lists are newest
first by Guard link time.

## Run statuses and timestamps

- `status`: only `completed` OBSERVED. Other values (failed, running, queued,
  cancelled) are **ASSUMPTION**, never seen. Open item: which statuses count as
  a run. Proposal: a run counts only when `status == "completed"` and
  `report_status == "ready"`; anything else is ignored for rules.
- `report_status`: `ready` OBSERVED; `pending` is named in tool docs.
- `task_available`: bool, `false` means Fleet no longer has the task (tool doc).
- Timestamps: ISO 8601 UTC with `Z`. Fraction length varies: `created_at` and
  `completed_at` have 6 digits, sometimes 4 (`14:04:55.0961Z`); `linked_at` has
  3. Parse tolerantly and cover the 4-digit case in a table test.
- Duration of a run: 5 minutes (recon) to about 2 hours (tests). Do not treat a
  run as stale or failed just because it is young.

## Cadence (drives the stale-project rule)

OBSERVED in one repo: tests and AI readiness about weekly; security about every
1 to 2 weeks; recon about monthly (Aug 4, Aug 12, Sep 1, Sep 23, Oct 21).
OBSERVED in another: last completed run 27 days ago on 2026-11-16 (all audits
in one batch on 2026-10-20). So one threshold N per project can be wrong for
some audit types; "newest completed run of any type per repo" is the simplest
measure. Open item: whether the threshold is per repo or per project.

## Reports

- `get_audit_report` returns `content` as **Markdown**, not JSON. There are no
  structured fields for findings. The radar must parse Markdown.
- Paging is by a UTF-8 byte budget (`limit` 4 to 32000, default 16000), with
  `cursor`, `has_more`, and `content_version` (a sha256-like hash of the
  report; useful as a cheap "report changed" check). Real security reports are
  longer than 12000 bytes, so read until `has_more` is false.
- Layout of audit reports (security, ai-readiness, tests, and others):
  1. `# <title>` (a sentence that summarises the result)
  2. `## Executive summary`
  3. `## <the question the audit answers>`, then
     `**Score: N / 100** · <label> · Confidence: <High|Medium|...>`
  4. `` `owner/repo` · `branch` · `<40-char sha>` ``
  5. `## Findings` (security: `## Security findings`)
  6. per finding: ``### `<ID>` — <title>``, then
     `**Severity:** <High|Medium|Low> · **Confidence:** <...>`, then free
     sections (`Why it matters`, `What was found`, `What to do`, `Related
     recommendations`, `Affected`, `Evidence` as a list of `` `path:line` ``,
     sometimes `Reproductions`, `Links`).
- `audit.recon` is different: `# Repository Overview`, no score, no findings
  list; sections `Audited Target and Revision`, `What Was Checked`,
  `Main Findings`, `Limitations`, `What to Do Next`.
- Severity words OBSERVED: High, Medium, Low. A higher level (Critical) or a
  lower one (Info) is **ASSUMPTION**; parse unknown words as "unknown", do not
  crash and do not drop the finding.
- Score: integer 0 to 100 in the `**Score: ...**` line. The label text after it
  is free prose (`Significant security fixes needed`, `Mostly ready`, ...);
  never match on labels. The previous score appears only inside prose
  ("previous audit: 84"), so the radar must keep its own history of scores.
- Scores move for reasons other than code changes. One repo's security score
  went 63 -> 38 -> 31 while the audit stated the relevant code was unchanged
  (the method corrected earlier omissions). Treat score deltas as hints, not as
  a trigger on their own.
- Reports can contain real vulnerability detail, file paths and code locations.
  Do not send them whole to Claude or Telegram, and do not log them.

## Finding identity is NOT stable

OBSERVED on the same repo and audit type, two consecutive security runs:

| Run | ID style | Example for the same issue |
| --- | --- | --- |
| 5 Nov | slug | `SOME-DESCRIPTIVE-SLUG` |
| 9 Nov | sequence | `F-006` |

Other audits use `F-NNN` numbering (AI readiness, with out-of-order numbers like
`F-003` listed before `F-001`) or slugs (tests). Titles and evidence line ranges
also change between runs (`file:189-215` in one run, separate lines `189`, `196`,
`210` in the next). The 5 Nov run had 8 findings and the 9 Nov run had 9.

Consequences for the design:

- The ID in the heading cannot be the dedup key or the "recurring" key.
- Candidate key: `(repo_id, action_key, normalized evidence file paths)` without
  line numbers, with matching by overlap (a finding keeps its identity when the
  path sets overlap enough), plus a similarity check on the title. Needs a table
  test with these two real shapes. This is a heuristic and can mis-merge.
- **Decided 2026-11-16:** track **counts of High findings per
  `(repo, audit type)`** and alert on increases. Individual finding identity is
  not tracked. "Recurring across N runs" means "the High count has not dropped
  to zero over N consecutive runs of that audit type". Per-finding matching may
  come later if the group asks for it.
- A run's count is only comparable with the previous completed run of the same
  audit type on the same repo.

## Harness, AI readiness, tests, tooling

- No field named "harness" exists. `audit.ai-readiness` reports score 0 to 100
  about agent-led engineering readiness. **ASSUMPTION:** this is the "Guard's own
  harness metric" in the handover. Confirm with the operator.
- Test quality is the score of `audit.tests`. Coverage numbers appear only in
  prose (a percentage of lines in a configured source scope), not as a field.
- `GUARD.md` presence: the reports mention a repo file named **`ENJI_GUARD.md`**
  ("owner guidance", read by audits). There is no tool that checks file
  presence. The only signal is the prose "ENJI_GUARD.md ..." in reports, which
  is not reliable. The "GUARD.md" wording in the requirements likely means this
  file. Open item.
- "Analysis tooling" (dead-code, duplicates, security, dependency scanners) maps
  to audit types `audit.dead-code`, `audit.implementation-unification` (the
  closest to duplicates; **ASSUMPTION**), `audit.security`,
  `audit.dependency-hygiene`. The signal is "a completed run of that type exists
  and is recent for the repo", from `list_audit_runs` with `action_key`. The
  `list_audit_runs` default (no filter) returns the latest 5 to 10 runs only, so
  one call per audit type is needed to be sure something is missing.

## Phase 3 additions

- Scores are kept per (repo, audit type) from the newest completed run of `audit.ai-readiness`
  and `audit.tests`. Deltas are not used (see "Reports").
- `list_audit_runs` returns only the newest runs, so tooling gaps rest on the run history the
  radar has seen. **ASSUMPTION:** the evidence rule (a gap is stated once the whole run list was
  read or the repository was watched for the whole window) and `audit.implementation-unification`
  as the duplicate detector.
- Whether `list_audit_runs` accepts an `action_key` filter is NOT verified. Check the tool
  schema with `probe`, which prints the argument names, before relying on it.

## Cost of a poll

For P projects, R repos and K audit types of interest the minimum is
`1 + P + R*K` calls, plus pagination (one project had more than 25 repos), plus
report reads only when a new completed run appears. `list_audit_runs` is
documented as bounded because it delegates to Fleet. Use `content_version` and
the newest `task_id` per `(repo, action_key)` to skip unchanged reports.
Rate limits are not documented. **ASSUMPTION:** a poll of this size is fine;
measure it before the timer is enabled.

## Auth

Not exercised from the radar yet. This session used Claude Code's own login.
Radar login, token lifetime and refresh behaviour are still unknown (see
handover). Never run `run_audit`; request read scopes only.
