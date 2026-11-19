# 04-03 Summary — parser keeps finding titles in memory only

## Done
- `ReportFinding.title: str = Field(default="", repr=False)` in `guard_report.py`, filled by private `_title(heading)`: the text after the backticked id, with leading `-`, en dash, em dash, `:` and whitespace stripped. No id or no text gives `""`; the report never becomes unknown because of a title.
- Tests (`tests/test_guard_report.py`): tracer `test_finding_title_is_parsed`; table in English and Russian (`test_title_separators_are_stripped`, `test_title_empty_when_unreadable`, `test_title_parsed_from_committed_fixtures`, `test_title_never_in_logs_or_repr` with caplog at DEBUG). The existing "Secret finding title not in log" assertion is unchanged.
- Nothing persisted: `ledger.py`, `state_store.py`, `high_history.py` do not mention `title`; state stays schema 3.

## Verification
`make verify` -> `1801 passed, 1 skipped`, exit 0.

## Notes
- The dash set is written as escapes (`–`, `—`) because ruff RUF001 rejects a literal en dash.
- Only `repr` hides the title; `model_dump_json()` of an `AuditReport` includes it. `AuditReport` is never persisted.
- No human checkpoint task exists in this plan. E2E waived (no user-visible change).
