# Digest no longer reads recurring runs, low scores or tooling gaps

- Location: `recurring_rules.py`, `score_rules.py` / `tooling_rules.py` outputs kept in `PollData`
  (`low_scores`, `tooling_gaps`), config `recurring.after_runs`, `scores.*`, `tooling_gaps.*`.
- Smell: T-0203 made the digest a per-project overview (high counts, audit words, age), so the
  `recurring N runs` marker, low-score lines and tooling-gap lines are not rendered anywhere.
  The rules still run and their results are collected but nothing shows them.
- Fix: decide per rule — show it again in the per-project digest, or remove the rule, its config
  keys and its state with a planned migration.
