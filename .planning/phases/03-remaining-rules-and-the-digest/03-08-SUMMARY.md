# 03-08 summary: digest builder and text

## What changed
- `digest.py` (new, core): `DigestTier`, `TIER_ORDER`, `RESOLVED_LINE_CAP = 5`, `DIGEST_TITLE`, `DigestInput`, `DigestItem`, `Digest`, `build_digest`, `render_digest`. Pure: all times come from the input; text reuses the `messages.py` helpers and the reserve-and-repack "(k/n)" loop.
- `tests/test_digest.py` (new): tracer `test_mixed_digest_text` with the exact text, then tables for tier order, sorting (including shuffled input), recurring, cap, all-clear, unread, resolved, escaping, counts-only, UTC, splitting and invalid input.

## Verification
`make verify` -> exit 0, `1399 passed, 1 skipped`. `uv.lock` and `pyproject.toml` unchanged.

## Decisions
- Recurring series are shown once inside the High tier with the "Recurring" marker; there is no separate recurring tier (D-05 / D-09 interpretation from the plan Notes, flagged for the operator).
- A resolved item whose level is not "high", "stale" or "never_ran" prints "no longer open." so an unknown level never breaks the digest (not in the plan; no id or path can leak).
- A ToolingGap with no run ever seen ends with "(none seen)" instead of "(last run: date)".
- Duplicate items with the same key collapse to one line, so a series never appears twice in a tier.

## Not done
No human checkpoint task exists in this plan. Wiring the digest into the pipeline is plan 03-11.
