# Tooling gaps: look the history up instead of waiting

## Problem

`list_audit_runs` returns only the newest runs. For a repository with a long run history the
radar cannot tell whether an audit type was run recently, so the first tooling gaps wait up
to `tooling_gaps.window_days` (default 60) days after the first run.

## Idea

If the tool schema confirms an audit-type filter on `list_audit_runs`, answer the gap
candidates at once: one filtered call per candidate on the digest run, instead of waiting for
the radar to watch the repository for the whole window.

## ASSUMPTION

ASSUMPTION: `list_audit_runs` accepts an `action_key` argument. It is not verified.

## Verify first

What argument names does the real `list_audit_runs` tool take? Run `probe` and read the
printed schema before building anything.
