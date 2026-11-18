# 03-06 summary: tooling gap rule

- `guard.py`: `RepoRuns.runs_complete` (default False). `_read_runs` now returns the runs and whether the listing was read to its end (Guard's last page said has_more false); it is False only when paging stopped early on the 24 h bound. No other reader behaviour changed; no existing reader row was edited.
- `tooling_rules.py` (core): `ToolingGap`, `ToolingEvaluation`, `evaluate_tooling`. Per (repo, configured audit type) the later of the stored and the polled newest counting run is kept; a gap needs no run in the last `window_days` (exactly the window is a gap; a future-dated run is recent) and either a complete run list or `window_days` of observation, else the pair is `undecided`. `observed_since` never moves; records are dropped for unlisted repositories, unconfigured types and other projects.
- Tests: `tests/test_tooling_rules.py` (tracer through the real `fetch_project`, then window, evidence, accumulation, newest, configured, order, invalid and idempotent tables) and four `runs_complete` rows in `tests/test_guard_reader.py`.
- ASSUMPTION (D-10, Claude's Discretion): the evidence rule bridges Guard's truncated run list, so first gaps for repositories with long run histories appear `window_days` after the radar first polled them.
- Not done here: wiring into the pipeline and digest (later plans), the `docs/ideas` item (03-11). No human checkpoint in this plan.
- Verified: `make verify` exit 0, `1310 passed, 1 skipped`.
