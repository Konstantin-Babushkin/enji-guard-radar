# 04-14 summary: on/off matrix and hostile-text tests

## Built

- `tests/test_triage_matrix.py`: `strip_claude_lines`, an `Outcome` runner (alert keys, state files, report counts,
  exit code, texts of alerts, reminders and digest), six scenarios (fresh, rise, retry, reminder, digest, collapse)
  and ten triage variants (good; the six `TriageFailed` reasons; `schema_invalid`; `unknown_id`; a hostile fake that
  obeys planted text). `test_same_alerts_on_or_off` has 60 cells; each holds the four D-12 equalities against the
  off run. A Claude line appears only for a good reply in fresh and collapse; every other cell has none and at most
  one call is made, only in fresh and collapse. `test_split_chunks_keep_the_off_item_sequence`: 40 fresh alerts with
  60-character causes; the on-run has more chunks than the off-run, the item sequence and key order are equal.
- `tests/test_triage_hostile.py`: `HOSTILE` planted in a title, a project name and an audit key. The title stays
  inside the nonce data block as one JSON string (the sanitiser drops the link token and keeps the sentence); the
  project name is nowhere in the request; the audit key becomes `other`; six obeying replies (unknown id, p9, link in
  a cause, dropped item, extra item, summary field) leave keys, order, texts and state as in the off run.

## Decisions

- Setup polls of a scenario run with triage off; only the measured poll uses the variant (swap of `config_text` and
  `triager` on the shared `TriageSim`).
- `HostileSim` subclasses `TriageSim` to set the project name, because the shared helper hardcodes "Proj" and shared
  files were not to change.
- The hostile audit key is tracked by the default config, so the row asserts `audit_type == "other"` (not the
  "no alert" fallback). The key stays in the alert text and state as run metadata.
- `delivered` counts messages, so the splitting row checks it against each run's own chunk count.

## Verified

- No production file changed (`git diff --name-only <merge-base> HEAD -- src` is empty); no fix commit was needed.
- Mutation checks, each reverted: dropping the re-armed/retried exclusion fails 20 matrix cells (rise, retry with
  every variant that calls); accepting any cause fails the link row; sorting differently when triage is set fails
  four tests.
- The plan's `fails_when` strings ("FAILED", "error:") were judged by intent: pytest exits 0 with a passing summary.
- No human checkpoint exists in this plan; nothing was left for the operator.
