# Plan 03-03 summary: pure digest due decision

Delivered `src/enji_guard_radar/digest_schedule.py` (core, pure): `planned_moment`, the `NotDue`,
`Due`, `Deferred` and `Skipped` decisions and `decide_digest` (D-01, D-02, D-03; DIG-02, DIG-03).
Tests in `tests/test_digest_schedule.py`: the default Wednesday 05:00 UTC / 24 h timeline as a
table, then boundary, schedule, offset, year, backwards-clock, sweep and naive-`now` cases.

Decision order: `handled_for >= planned moment` is NotDue; elapsed below grace is Due (poll ok) or
Deferred (poll failed); elapsed at or above grace is Skipped, or NotDue with no history.

Interpretation: the plan table is silent on a `handled_for` later than the planned moment inside
the window; it is NotDue there too (never a double send, never Skipped).

Verification: `make verify` -> `1083 passed, 1 skipped`, exit 0. `uv.lock` and `pyproject.toml`
unchanged. No human checkpoint tasks in this plan; nothing left for the operator.
