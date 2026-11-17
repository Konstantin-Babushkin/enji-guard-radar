# 02-01 Summary: High history in state (schema 2, run fold, per-run re-arm)

## Done
- `ledger.py`: State schema 2 (`RunPoint`, `SeriesHistory`, `PollHealth`, `State.histories`, `State.health`),
  `READABLE_SCHEMA_VERSIONS = (1, 2)`, pure `migrate_state_v1`; schema 3, `"2"` and bool still load as
  Corrupt ("this radar reads 1 and 2"). `reconcile` and `apply_delivery` carry histories and health through.
- `high_history.py` (new, core): `advance_series` folds completed runs by Guard task_id; opens at the first
  High run, closes after M clean runs, ignores Unknown, duplicate and late runs, keeps 20 points.
- `reconcile`: per-run re-arm (STATE-07, D-06). A rise of a counted condition over the count stored at its
  previous evaluation plans it again once and lists it in `Reconciliation.rearmed`. `alerted_at` is still set
  only in `apply_delivery`. `ConditionRecord` keeps its six schema-1 fields.
- Tests: `test_state_v2.py` (tracer: a hand-written schema-1 file through a live fixture run comes out as
  schema 2), `test_high_history.py`, `test_rearm_per_run` and the Phase 1 tests updated for schema 2.

## Decisions
- D-06 per-run rise; the peak/`alerted_count` field is not built.
- No pipeline wiring: histories and health stay empty in real runs until plans 02-02, 02-04 and 02-07.
- The tracer's expected requests will change in plan 02-04.

## Human checkpoints
None in this plan.

## Verification
`make verify` -> `470 passed`.
