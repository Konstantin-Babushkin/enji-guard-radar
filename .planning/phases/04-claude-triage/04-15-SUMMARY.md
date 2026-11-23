# 04-15 summary: doctor triage line and runbook

## Built

- `doctor.py`: `ENV_CLAUDE_TOKEN`, `CLAUDE_BINARY`, `NAME_TRIAGE`, `TriageFacts`, `verdict_triage`. Pure: off is
  SKIP, a missing `claude` or token variable is FAIL naming it, every call today failed (calls > 0, failed ==
  calls) is WARN, anything else PASS; always `calls N, failed M` when on. Core code does not import `settings`.
- `cli_doctor.py`: `_triage_facts` reads the config, `shutil.which("claude")` (process PATH, as the adapter does),
  the token through `load_claude_token` (presence only; the value is added to the redaction list) and today's
  UTC record from `FileTriageCostStore.load()` (never saved). A config that cannot be read gives SKIP.
  No triager is built and no Claude call is made.
- `tests/test_doctor.py`: 16 `triage` tests (tracer, verdict table, binary/token FAIL through the CLI, 3/3 WARN with
  the cost file bytes unchanged, 3/1 PASS, other-day record ignored, unreadable config SKIP, token never
  printed, fake `claude` never run). The healthy-setup total changed from `2 skip` to `3 skip`.
- `docs/deploy.md`: `## Triage` section (8 steps), short reminder and digest shapes, the first-sight age note,
  and a Doctor note that the `triage` line is SKIP while off and needs `--config`.

## Decisions

- The WARN detail adds "every call today failed: see the journal" after the `calls N, failed N` fragment.
- Doctor reads the PATH of the process, not the `environ` mapping, to match the adapter's lookup.

## Verified

- `make verify` -> exit 0, `2270 passed, 1 skipped`.
- Spec checks 1-12 run literally; checks 4-7 with the real CLI and a fake `claude` (never executed, cost file
  hash unchanged, token string absent from output).

## For the operator (not attempted here)

- Real-chat check that the 3-space indent of the Claude line survives (D-10 ASSUMPTION).
- A dry-run with a real token and triage on (makes one real Claude call).
