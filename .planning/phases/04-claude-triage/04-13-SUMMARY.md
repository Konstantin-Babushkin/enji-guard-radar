# 04-13 summary: claude -p adapter wired behind triage.enabled

## Built

- `claude_cli_triager.py`: `ClaudeCliTriager` (stdlib only) implementing the `Triager` port. Argv, environment
  and envelope parsing follow the OBSERVED lines of `docs/triage-notes.md`; each fact carries a comment naming
  the notes section, open points are marked `ASSUMPTION:`.
- `tests/fake_claude.py`: `install_fake_claude(tmp_path, *, envelope, exit_code, sleep)` writes a fake `claude`
  that records argv, env key names, pid, cwd and stdin to `record.json` before it sleeps and answers.
- `tests/test_claude_cli_triager.py` (23 tests: tracer, env_, argv_, cwd_, stdin_, fail_, logs_) and
  `tests/test_triage_e2e.py` (7 tests: CLI as its own process with the fake on PATH, live run with a fake
  Telegram transport, adapter imported only when triage is on).
- `cli.py`: `_triage_wiring` (lazy import, `load_claude_token`, `FileTriageCostStore`, read-only in `--dry-run`,
  `known_secrets` with the token); logging is configured again so the token is redacted too.

## Decisions

- Argv: `-p --output-format json --json-schema <schema> --tools "" --no-session-persistence --model <m>
  --max-budget-usd <n>`. Never `--bare`. The budget flag is passed as a per-call backstop (notes: honoured only
  after the cost is spent; the per-day cost file stays the real limit).
- The system prompt goes on stdin above the data block: `--system-prompt` was only seen in `--help`.
- Child environment is exactly `PATH` and `CLAUDE_CODE_OAUTH_TOKEN`; no `HOME`. Fresh empty temp cwd, removed
  in a `finally`; own session, the process group is killed on timeout. stderr is not read, stdout is never logged.
- Failure mapping: no token -> not_logged_in (no process); binary missing -> not_installed; non-zero exit with the
  401 / `authentication_error` / "Not logged in" signature -> not_logged_in, other non-zero (budget exceeded
  included) -> exit_nonzero; exit 0 with non-JSON or no `structured_output` -> not_json; `is_error` not false ->
  refused_or_error; nonce clash three times and the one broad `except Exception` -> refused_or_error.
- A failed call whose envelope parses still carries cost and tokens, so the cost file sees a budget stop.
- Input tokens = `input_tokens` + cache creation + cache read; None unless `usage` has int input and output.
- `settings.py`, `config.py` and the example config are untouched: the notes name the same token variable and
  the same default model (`haiku`).

## Verified

- `make verify` -> exit 0, `2254 passed, 1 skipped`.
- With a booby-trapped `claude` first on PATH, both new test files pass and the trap is never run.
- `pyproject.toml` and `uv.lock` unchanged.

## For the operator (not attempted here)

- The live call with a real token on the radar host and turning `triage.enabled` on (plan 04-15 runbook).
- The first operator run should confirm the `Not logged in` result text and the cold-cache cost against the notes.
