# 04-12 summary: triage in the run (FakeTriager, end to end through run_once)

## Built

- `pipeline.py`: `Deps.triager`, `Deps.cost_store`, `Deps.known_secrets`; `PollData.titles` (High finding
  titles of the reports read this run, tracked audit types only, memory only); `_collect_titles`,
  `_fresh_candidates`, `_triage_step`, `_record_triage_cost`. The step runs after `deps.store.save(rec.state)`
  and before `render_messages(..., triage=accepted)`. One `build_request`, at most one `triage()` call,
  `validate_reply` on a reply; the accepted map is the only thing the renderer gets.
- `tests/test_triage_pipeline.py`: 23 tests through `run_once` with the in-memory Guard, a fake notifier,
  a real state file and cost file in `tmp_path`, and `FakeTriager`.

## Decisions

- Fresh = in `rec.to_send`, kind HIGH_FINDINGS, not re-armed, not retried, series has titles read this run.
- `age_days` None maps to 0 in the candidate.
- The triager is not wrapped in try/except (the adapter owns the one broad except); no new broad except.
- Cost: call line, then `add_call` + `save` when there is a store, then the daily line. With no store the daily
  line comes from an in-memory record for today's UTC date.
- `cli.py` is untouched: wiring the real triager, cost store and known secrets is plan 04-13.

## Verified

- `make verify` -> exit 0, `2149 passed, 1 skipped`.
- Removing the re-armed/retried exclusion makes two tests fail (mutation check, reverted).
- No human checkpoint exists in this plan; nothing was left for the operator.
