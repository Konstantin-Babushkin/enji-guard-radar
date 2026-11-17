# Plan 02-03 summary: Guard token layer

## Built

- `ports.py`: `GuardFailure`, `GuardCallError(message, *, cause)`, `AuthFailureKind`, `GuardAuthError`, `AccessTokenSource`.
- `guard_auth.py` (core): credential model and strict file format, `access_valid` (120 s margin), pure total `classify_token_response`, `scope_ok` (read scopes only, `audits:run` refused), `refresh_form`, `merge_refreshed`, fixed `AUTH_FAILURE_TEXT`, plus `auth_failure_text` (adds an RFC 6749 error code, nothing else).
- `guard_credentials.py` (shell): `CredentialStore` (`state/guard-auth.json` 0600 in a 0700 directory, atomic write, `guard-auth.lock` flock with timeout), `UrllibOAuthTransport`, `RefreshingTokenSource` (lock, re-read, refresh, save, then return; forced refresh reuses a token another process already saved; a failed save keeps the token in memory and logs an error naming `enji-guard-radar auth`).
- `state_store.py`: `atomic_write` extracted, `FileStateStore` behaviour unchanged (its tests untouched).
- Tests: `fake_oauth.py`, `test_guard_auth.py`, `test_guard_credentials.py`, `test_architecture.py` (shell modules registered, only `urllib.parse` allowed in core).

## Verification

`make verify` (ruff, format check, mypy strict, marker scan, pytest): last line `517 passed`.
No network, credentials or `audits:run` were used; `pyproject.toml` and `uv.lock` are unchanged.

## Left for the operator

No human checkpoint exists in this plan. The first real login (`enji-guard-radar auth`, plan 02-05) and the
real-Guard check (plan 02-11) are still to be done. ASSUMPTION A2 (Guard rotates refresh tokens) and A3
(token lifetimes) stay unverified until then.
