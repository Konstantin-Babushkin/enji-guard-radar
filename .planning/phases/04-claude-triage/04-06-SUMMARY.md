---
phase: 04-claude-triage
plan: 06
requirements: [ALERT-07]
---

# 04-06 summary: short reminders (Radar reminder · N open)

## What changed
- `texts.py`: `reminder.short_title` in en (`Radar reminder · {count} open`) and ru
  (`Радар · напоминание · открыто: {count}`); `reminder.title` is removed.
- `reminders.py`: `_title(index, count, total, language)` (N = all due series, same in every chunk,
  `(k/n)` inside the bold title); `_item(alert, language, show_repo)` is `messages.high_line(..., arrow=False)`;
  `render_reminders` shows the repo only for series flagged by `messages.ambiguous_series`.
  `REMINDER_TITLE` is gone. `reminders_due`, `mark_reminded`, the interval, clocks and the pipeline are unchanged.
- Tests: tracer `test_short_reminder_text`, rows for Russian, repo disambiguation, an 80-series split,
  no priority/Claude text and escaping; old-wording asserts in `test_reminders.py`,
  `test_reminders_pipeline.py`, `test_pipeline_language.py` and `test_messages_language.py` rewritten.

## Deviations (judged by intent)
- The reminder-invalid-input row for `seen_since=None` is dropped: a reminder line no longer reads `seen_since`.
- The Russian reminder findings-plural test is removed: the plural no longer appears in reminders
  (it stays covered by `test_high_findings_text_in_russian`).
- This summary file is the one extra file beyond `files_modified` (the plan's own output).

## Not done
- The plan has no human checkpoint task. Nothing ran against real Guard, Telegram or Claude. State stays schema 3.
