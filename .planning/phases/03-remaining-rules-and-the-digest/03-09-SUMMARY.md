# 03-09 summary: collect scores, tooling and resolved log in the run

## What changed
- `high_rules.py`: `skip_report_for(project_id, histories, audit_types, scores=None)`. For a score audit type (`SCORE_ACTION_KEYS`) the report is read until its task_id is recorded as a score, High scope or not; otherwise the previous rule. Without `scores` it behaves as before.
- `pipeline.py`: `_evaluate_projects` takes the loaded `State` and returns the frozen `PollData`. Each evaluated project folds `evaluate_scores` and `evaluate_tooling` into the stored records (thresholds from `config.score_threshold_for`, window from `tooling_window_for`) and logs one INFO line each. Paused, excluded and unknown projects and a failed listing leave the records as stored. `_run_locked` puts `scores` and `tooling` into the state before `reconcile`, applies `append_resolved(rec.state, rec.resolved, now)` and saves that state before any message. `RunReport.low_scores` and `tooling_gaps` carry the findings; nothing is sent for them.
- `tests/test_collect_pipeline.py`: tracer through `cli.main` on the fixtures, then 31 rows over in-memory worlds (thresholds, unknown reports, read cost, frozen projects, tooling, resolved log, dry-run, nothing sent, the `skip_report_for` score table).

## Verification
`make verify` -> exit 0, `1405 passed, 1 skipped`. `uv.lock` and `pyproject.toml` unchanged. Two mutations (no `append_resolved`, no `scores` argument to `skip_report_for`) each fail three of the new tests.

## Decisions
- `_evaluate_projects` takes the `State` instead of a histories dict, so it can build the three stored dicts itself. The `skip_report_for` call is assigned to a variable first so the line stays under 100 characters.
- The score-and-tooling fold lives in a small helper `_fold_scores_and_tooling` to keep `_evaluate_projects` readable.

## Not done
No human checkpoint task exists in this plan.
