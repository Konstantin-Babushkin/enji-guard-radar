# Tooling gap lookup by audit type

## Problem

`list_audit_runs` returns only the newest runs of a repository. For a repository with a long run
history the radar cannot tell "never ran" from "ran long ago", so the first tooling gaps wait up
to `tooling_gaps.window_days` after the first run before they are stated.

## Idea

If the tool schema confirms an audit-type filter (`action_key`) on `list_audit_runs`, answer
gap candidates at once: on the digest run, make one filtered call per candidate (repository,
audit type) and state the gap immediately when the newest completed run is older than the
window or missing.

## ASSUMPTION

ASSUMPTION: `list_audit_runs` accepts an `action_key` filter. This is not verified.

## Question to verify first

Run `probe` and read the argument names of `list_audit_runs`: is there an audit-type filter,
and does it return the newest completed runs of that type regardless of other types?
