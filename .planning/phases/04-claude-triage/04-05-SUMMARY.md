# 04-05 Summary: strict triage config and Claude token loading

## Done
- `config.py`: `TriageSettings` (frozen, extra forbid) and `RadarConfig.triage`; off by default.
  `backend` is `Literal["claude_cli"]`; `model` matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`;
  `max_items_per_call` 1..100 (30), `timeout_seconds` 5..300 (60), `max_cost_usd_per_call`
  0.01..5 (0.50, `StrictFloat`: ints accepted, bools rejected).
- `config/radar.example.yaml`: documented `triage:` block equal to the defaults.
- `settings.py`: `ENV_CLAUDE_TOKEN`, `load_claude_token`, `known_secrets(settings, claude_token=None)`.
- Tests: `test_triage_*` and `test_example_documents_triage` in `tests/test_config.py` (incl. one
  subprocess test: `triage.bogus` exits 2, one `error:` line naming `triage`, no state dir);
  `*claude_token*` and `known_secrets` tests in `tests/test_settings.py`.

## Assumptions (unchanged from the plan)
- Default model `haiku` (plan 04-01 confirms or changes it).
- Env var name `CLAUDE_CODE_OAUTH_TOKEN` (plan 04-13 corrects it if the notes show another).

## Verification
- `make verify`: exit 0, `1847 passed, 1 skipped`.
- Plan `fails_when` for Task 2 mentions output containing `error:`: judged by intent; a passing run
  prints none, only the E2E test's own subprocess stderr has an `error:` line.
- No human checkpoint task in this plan; none attempted. No network, no real Claude/Guard calls.
