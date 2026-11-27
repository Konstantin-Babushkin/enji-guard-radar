# Radar settings in a Google Sheet

Operator idea, 2026-11-23.

**Status: built (T-0164).** Decisions of 2026-11-27: the sheet overrides the YAML field by field;
an unreachable or invalid sheet keeps the last good copy and alerts the ops chat once; auth is a
read-only Google service account whose key lives in the env file; only per-project thresholds
are time settings. How to set it up: `docs/deploy.md`, "Settings from a Google Sheet". The open
questions below are answered there; the audit is the sheet's own version history.

## What

Keep the per-project settings (which projects are watched, high/low priority audit types,
thresholds such as `stale_after_days`) in a Google Sheet that non-developers can edit, instead
of `radar.yaml` on the host.

## Why

Choosing projects and audit priorities is a management decision. Editing YAML on the radar
host needs shell access and a redeploy-free but manual step.

## Open questions

- Auth: a service account with read-only access to one sheet; a new secret in `radar.env`.
- Source of truth: the sheet overrides `radar.yaml`, or generates it? What happens when the
  sheet is unreachable or malformed (keep the last good copy, fail closed, alert ops)?
- Validation: the same strict rules as the YAML config (unknown keys, ranges) and a doctor line.
- Audit: who changed what, when (the sheet's version history may be enough).

## Not now

Build after per-project alerts and high/low priority (T-0051, T-0052) settle what the settings are.
