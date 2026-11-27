# Green line for projects with no high issues

Operator idea, 2026-11-23.

## What

When a watched project has no open High problem, show it with a green light instead of
leaving it out:

```
🟢 <b>hr-edo</b> · нет высоких проблем
🟢 <b>hr-edo</b> · no high issues
```

Today a clean project does not appear at all, so the group cannot tell "clean" from
"not watched" or "not checked".

## Why

The group sees the whole list of watched projects, not only the bad ones, and notices when
a project drops out.

## Open questions (decide before building)

- Where the green lines go. Alerts are sent only on change, so a green line in every alert
  message would repeat the same news. Candidates: the weekly digest (one block of all
  watched projects), and/or the manual summary run.
- Whether a project turning clean (its last High resolved) gets its own green line as an
  alert, once.
- What "no high issues" means: no open condition of a high-priority audit type
  (`high.audit_types`). Should stale / never-ran projects (🟡) and "not checked" projects
  (Guard error) stay out of the green list? Proposed: yes, green only for evaluated,
  fresh projects with zero High.
- Line budget: one line per project, as in T-0051.
