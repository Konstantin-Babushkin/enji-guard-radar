# Plan 02-05 summary: `auth` command (one-time read-only Guard login)

## Built

- `guard_login.py` (core, pure): protected-resource and server metadata validation (https only, pinned issuer, S256, both grants, auth method `none`, registration endpoint), registration request and answer parsing, PKCE `code_challenge` (RFC 7636 vector tested), authorize URL, strict redirect parsing (exact loopback address, one matching `state` via `hmac.compare_digest`, `iss`, no `error`, a `code`), code-exchange form, `credentials_from_grant` (refuses no refresh token and any scope outside the read scopes through `scope_ok`), `grant_summary`. `LoginError.reason` is fixed text plus an error code or status; the server's `error_description` (cut to 200 characters) is only `detail`, shown on stderr and never logged.
- `guard_credentials.py`: `LoginTransport` protocol, `UrllibOAuthTransport.get_json` and `.post_json` (one shared request helper with `post_form`).
- `cli_auth.py` (shell): `auth --state-dir DIR --no-resource-indicator`; runs under the credential lock, hidden `getpass` prompt, saves only after every check; Ctrl-C or end of input prints `aborted; nothing stored` (exit 1); failures print `error: <reason>` on stderr (exit 1) and leave an existing `guard-auth.json` untouched.
- `cli.py`: `auth` registered and dispatched; `main` takes `oauth_transport` and `prompt`.
- Tests: `fake_oauth.py` (public Fleet metadata, `FakeLoginTransport`, `login_routes`), `test_guard_login.py` (59 rows), `test_auth_command.py` (27: tracer, redirect rows, refresh/scope refusals, abort, keeps-existing-file, `--no-resource-indicator`, secrecy over stdout, stderr and DEBUG logs, urllib transport).

## Verification

`make verify`: last line `709 passed` (see the PR for the final run). No network, credentials or `audits:run` used; `pyproject.toml` and `uv.lock` unchanged.

## Left for the operator

No human checkpoint exists in this plan and none was attempted. The real login (`enji-guard-radar auth` on the production host) is plan 02-11. ASSUMPTIONS A1 (refresh token issued without `offline_access`), A4 (loopback redirect accepted at registration) and A5 (RFC 8707 `resource` accepted; `--no-resource-indicator` is the escape hatch) stay unverified until then.
