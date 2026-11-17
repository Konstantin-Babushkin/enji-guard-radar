# 02-12 Summary: skip reading reports whose task_id is already counted

- `guard.py`: `SkipReport` type; `fetch_project` and `_repo_runs` take a keyword-only `skip_report`. A skipped report is not read and has no entry in `RepoRuns.reports` (DEBUG log with task id and action key only). Without it, behaviour is unchanged.
- `high_rules.py`: pure `skip_report_for(project_id, histories, audit_types)`. It skips out-of-scope action keys (recon included) and task_ids already in the series history. A failed or never-read report is not in history, so it is requested again next poll (RULE-09).
- `pipeline.py`: passes the predicate, built from the histories stored at the start of the run.
- Tests: tracer `test_unchanged_run_is_not_read_again`, reader rows (`skip`), predicate table, reread, new-run-read-once and idempotence rows.
- Accepted limitation: a rewritten report of an already counted run is never seen again.
- Verification: `make verify` -> `745 passed`. No human checkpoint task in this plan.
