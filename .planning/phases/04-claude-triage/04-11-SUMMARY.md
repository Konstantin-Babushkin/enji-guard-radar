---
phase: 04-claude-triage
plan: 11
requirements: [TRI-05]
---

# 04-11 summary: triage cost record and cost file

## What changed
- `triage_cost.py` (core): `CostDay`, `TriageCostStore` protocol, `KEEP_DAYS = 35`, `add_call`
  (one record per UTC day, newest 35 kept, oldest first), `call_line`, `daily_line` (`$unknown` when no call
  that day reported a cost), `serialize_days` / `deserialize_days` (strict document, `{"version": 1, "days": [...]}`,
  `ValueError` on a bad one).
- `triage_cost_store.py` (shell): `FileTriageCostStore` writes only `triage-cost.json` through
  `state_store.atomic_write` (0600). A missing file is empty history; a corrupt or unreadable one logs one WARNING
  and is treated as empty; a failed write logs one WARNING with the error class and does not raise.
  `ReadOnlyTriageCostStore` loads from the inner store and writes nothing (DEBUG line) for `--dry-run`.
- `tests/test_architecture.py`: `triage_cost_store` and `claude_cli_triager` listed as shell modules.
- `tests/test_triage_cost.py`: tracer `test_add_one_call`, then unknown, failed, days, round-trip and store rows.

## Deviations (judged by intent)
- The WARNING reason for a corrupt file is the exception class name (`ValidationError`), not `JSONDecodeError`:
  the document is parsed by pydantic. Contents are never logged.
- This summary file is the one extra file beyond `files_modified` (the plan's own output).

## Not done
- The plan has no human checkpoint task. Nothing ran against real Guard, Telegram or Claude; nothing is wired
  into the pipeline or CLI (plans 04-12, 04-13). State stays schema 3; `state_store.py` and `ledger.py` are untouched.
