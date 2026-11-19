---
phase: 04-claude-triage
plan: 02
requirements: [ALERT-07]
---

# 04-02 summary: short one-line alerts and summary (en/ru)

## What changed
- `texts.py`: new en/ru entries (`marker.*`, `alert.age`, `alert.high.line`, `alert.high.line_count`,
  `alert.stale.line`, `alert.no_scan_yet.line`, `alert.where_repo`, `alert.triage_line`,
  `summary.short_header`); the old long alert and summary keys are removed. `alert.high.count` and
  `alert.high.seen` stay for reminders and digest.
- `messages.py`: public helpers `marker`, `audit_short`, `age_text`, `ambiguous_series`, `high_line`;
  `render_alert` and `render_summary_item` return one line; `summary_header` gives
  `Radar · N need attention` with the ` (k/n)` suffix; `render_messages` computes `ambiguous_series`
  over the whole rendered set and shows the repo name only for two repos of one project and audit type.
  `LONG_STANDING_PREFIX` is gone.
- Tests and `tests/golden/dry_run_default.txt` follow the new wording; only the "baseline, not sent"
  section of the golden changed.

## Deviations (judged by intent)
- The plan names the never-ran line key `alert.never_ran.line`, but its own acceptance grep for removed
  `alert.never_ran.*` keys would match it. The key is `alert.no_scan_yet.line` instead (wording unchanged).
- The plan's grep for old wording also matched the `ENGLISH_PHRASES` regex in
  `tests/test_language_e2e.py`; the old alert phrases there were replaced by `no scan|need attention`.
- This summary file is the one extra file beyond `files_modified` (the plan's own output).

## Not done
- No human checkpoint task was attempted. Nothing ran against real Guard, Telegram or Claude.
- Reminder and digest wording is unchanged (plans 04-06 and 04-09). State stays schema 3.
