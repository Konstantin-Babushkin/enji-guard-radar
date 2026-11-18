# 03-07 summary: doctor lines for digest and score thresholds

## What changed
- `doctor.py`: `NAME_DIGEST`, `NAME_SCORES`, `TIMER_INTERVAL_HOURS = 3` (ASSUMPTION: mirrors the timer unit), `verdict_digest`, `verdict_score_rules`.
- `cli_doctor.py`: both verdicts appended after the unevaluated-projects check.
- `tests/test_doctor.py`: healthy tracer now expects both PASS lines and `doctor: 11 pass, 0 fail, 0 warn, 2 skip`; verdict tables, WARN-exits-0, invalid-config SKIP and no-leak tests.

## Verification
`make verify` -> exit 0, `1285 passed, 1 skipped`. `uv.lock` and `pyproject.toml` unchanged.

## Decisions
- A score threshold of 0 renders as `off` (config says 0 disables); a project override counts when either score key is set.
- Missing or corrupt state adds no `last digest` part; only a loaded state shows `sent`, `skipped` or `none yet`. (The plan's Task 2 behaviour list says Missing shows `none yet`, which contradicts its own must_haves and Task 1; the must_haves were followed.)
- The `no_leak` test uses a valid config: the existing invalid-config FAIL line prints the config path, which is unchanged behaviour.

## Not done
No human checkpoint task exists in this plan.
