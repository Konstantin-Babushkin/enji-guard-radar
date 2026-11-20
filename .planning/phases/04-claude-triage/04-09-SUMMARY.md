---
phase: 04-claude-triage
plan: 09
requirements: [ALERT-07]
---

# 04-09 summary: short weekly digest, tooling gaps grouped per repo

## What changed
- `texts.py`: `digest.short_title`, `short_poll`, `short_clear`, `short_recurring`, `short_score`, `short_gap`,
  `short_resolved_head`, `short_resolved`, `footer_age` in en and ru, plus three small word entries
  `digest.short_what.{stale,never_ran,other}` for the compact resolved lines. The old digest keys
  (`digest.title`, `poll`, `open_items`, `resolved_head`, `item.*`, `gap.*`, `resolved.*`,
  `alert.high.count`, `alert.high.seen`) are removed from both catalogs.
- `digest.py`: one line per item. High lines reuse `messages.high_line`, stale lines `messages.render_alert`;
  low-score lines show the audit short name, `score/100` and the age in UTC calendar days; tooling gaps are
  grouped by (project, repo) into one line with sorted audit names, ordered by the group's oldest member and
  counted as one item for N and `max_items`. `Digest.has_high` drives the footer. The all-clear is one line.
  `DIGEST_TITLE` is gone (tests match on `<b>Radar weekly`).
- Tests: tracer `test_short_digest_text`, rows for grouping, cap, all-clear, footer, low score, resolved,
  Russian, splitting and "no Claude line"; `test_digest_pipeline.py`, `test_messages_language.py`,
  `test_pipeline_language.py`, `test_language_e2e.py` and the golden rewritten to the new text.
  On the fixtures the digest is `Radar weekly · 8 open` (was 18 items): 1 High, 3 stale, 4 repo gap lines.

## Verification
- `make verify`: exit 0, last line `1942 passed, 1 skipped in 47.93s`.

## Deviations (judged by intent)
- The plan's tracer check says "pytest exits 5 when -k selects nothing"; judged by intent: the test exists and passes.
- `docs/deploy.md` "Weekly digest" still describes the old digest text. It is not in `files_modified`, so it was left alone.
- Extra files beyond `files_modified`: this summary only.

## Not done
- The plan has no human checkpoint task. Nothing ran against real Guard, Telegram or Claude. State stays schema 3.
