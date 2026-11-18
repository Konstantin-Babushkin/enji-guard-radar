# 03-10 summary: reminders in the run

STATE-02 / D-11 / SC5: an open, alerted High series is re-sent as one separate reminder
message every `reminders.every_days` days (default 7), with no cap; the reminder clock is
written only after Telegram acknowledged the message.

## What changed

- `reminders.py`: `mark_reminded(state, keys, now)` writes or replaces the `ReminderClock` of
  each High series in an acknowledged message (non-High keys ignored, clocks kept sorted).
- `pipeline.py`: the alert loop moved into `_deliver(deps, messages, state, now, actions, mark)`
  (frozen `_Delivery` result); behaviour unchanged, existing tests untouched. The reminder step
  runs right after the alerts, is skipped after a fatal send error, uses `reminders_due` over
  the HIGH_FINDINGS alerts of all evaluated projects and `render_reminders` for the text, and
  delivers through `_deliver` with `mark_reminded`. `RunReport` gained `reminder_messages`,
  `reminders_delivered` and `follow_up_not_delivered`; an undelivered reminder makes the run
  ATTENTION (exit 1). Logs: INFO "reminders: N due, M delivered", WARNING "... retried next run".
- `tests/test_high_pipeline.py`: `QUIET_REMINDERS` (90 days) and `Sim(reminders_on=False)`.
- Tests: `tests/test_reminders.py` (mark_reminded rows), `tests/test_reminders_pipeline.py`
  (tracer plus edge table: failed_send, ack_before_mark, baselined, exclusions, interval_setting,
  fatal, not_fatal, dry_run, logs).
- `docs/deploy.md`: `## Reminders` section.

## Notes

- The reminder text starts with the HTML-bold title (`<b>Reminder: High findings are still
  open</b>`), as rendered by plan 03-04; the tracer checks for that title by intent.
- No human checkpoint task exists in this plan.
