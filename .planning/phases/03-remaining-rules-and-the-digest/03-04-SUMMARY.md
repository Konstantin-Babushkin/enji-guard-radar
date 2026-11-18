# 03-04 summary: pure reminders

- `messages.py`: `_days`, `_pack`, `_high_where`, `_high_findings` are now `days_text`, `pack_items`, `high_where`, `high_findings_text`; no text or signature change.
- `reminders.py` (core): `reminders_due` (interval boundary, clock = later of alerted_at and last reminder, never-alerted and planned series excluded, worst-first order) and `render_reminders` (one counts-only "Reminder" message, split into (k/n) chunks only when over the limit, each chunk owning its keys).
- `tests/test_reminders.py`: tracer plus boundary, clock, never_alerted, excluded, no_cap, order, render, split and invalid tables.
- Not done here: wiring into the run and writing reminder clocks (plan 03-10), `reminders.every_days` config (03-02). No human checkpoint in this plan.
- Verified: `make verify` exit 0, `1074 passed, 1 skipped`.
