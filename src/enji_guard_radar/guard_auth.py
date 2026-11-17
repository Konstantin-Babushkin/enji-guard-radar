"""Guard login core: credential format, expiry, token-endpoint answers, scope check.

Pure: no I/O and no clock reads (callers pass `now`). The credential file, the lock and the
HTTP form post live in `guard_credentials`. Nothing here ever puts a token, code or URL into
an exception text.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationError,
    field_validator,
)

from enji_guard_radar.ports import AuthFailureKind

GUARD_MCP_URL = "https://guard.enji.ai/mcp"
GUARD_PRM_URL = "https://guard.enji.ai/.well-known/oauth-protected-resource/mcp"
GUARD_ISSUER = "https://fleet.enji.ai"
READ_SCOPES = ("projects:read", "audits:read")
FORBIDDEN_SCOPES = frozenset({"audits:run"})
REFRESH_MARGIN = timedelta(seconds=120)
DEFAULT_ACCESS_LIFETIME = timedelta(minutes=5)
CREDENTIALS_SCHEMA_VERSION = 1
AUTH_COMMAND = "enji-guard-radar auth"

AUTH_FAILURE_TEXT: dict[AuthFailureKind, str] = {
    AuthFailureKind.NOT_AUTHENTICATED: f"Guard login missing or unreadable: run `{AUTH_COMMAND}`",
    AuthFailureKind.AUTH_EXPIRED: f"Guard login expired or revoked: run `{AUTH_COMMAND}`",
    AuthFailureKind.AUTH_CLIENT_GONE: (
        f"Guard login client registration is gone: run `{AUTH_COMMAND}`"
    ),
    AuthFailureKind.AUTH_REJECTED: f"Guard login rejected: run `{AUTH_COMMAND}`",
    AuthFailureKind.AUTH_FORBIDDEN: (
        f"Guard refused access (scope or permission): run `{AUTH_COMMAND}`"
    ),
}

RFC6749_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
    }
)

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401


class GuardCredentials(BaseModel):
    """The radar's own Guard login, as stored in state/guard-auth.json."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    issuer: str
    token_endpoint: str
    revocation_endpoint: str | None = None
    resource: str
    send_resource: bool = True
    client_id: str
    scope: str
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: AwareDatetime
    obtained_at: AwareDatetime

    @field_validator("token_endpoint")
    @classmethod
    def _https_only(cls, value: str) -> str:
        if not value.startswith("https://"):
            message = "token_endpoint must be an https URL"
            raise ValueError(message)
        return value


class Refreshed(BaseModel):
    """The token endpoint issued a new access token (and maybe a rotated refresh token)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    access_token: SecretStr
    refresh_token: SecretStr | None
    expires_at: AwareDatetime
    scope: str | None


class AuthRejected(BaseModel):
    """The login itself is no good: the operator has to run `auth` again."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AuthFailureKind
    error_code: str | None = None


class Transient(BaseModel):
    """No usable answer (busy, down, garbled): retry later, do not tell anyone to re-auth."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int


TokenOutcome = Refreshed | AuthRejected | Transient


class CredentialFormatError(Exception):
    """The credential file is not valid. `reason` holds field names and error types only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def serialize_credentials(credentials: GuardCredentials) -> bytes:
    """JSON bytes with the secret values revealed on purpose: this is the one place they go."""
    data = credentials.model_dump(mode="json")
    data["access_token"] = credentials.access_token.get_secret_value()
    data["refresh_token"] = credentials.refresh_token.get_secret_value()
    return (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8")


def deserialize_credentials(data: bytes) -> GuardCredentials:
    try:
        raw = json.loads(data)
    except ValueError:
        message = "not valid JSON"
        raise CredentialFormatError(message) from None
    if not isinstance(raw, dict):
        message = "not a JSON object"
        raise CredentialFormatError(message)
    try:
        return GuardCredentials.model_validate(raw)
    except ValidationError as exc:
        problems = sorted(
            {
                f"{'.'.join(str(part) for part in error['loc']) or 'file'}: {error['type']}"
                for error in exc.errors(
                    include_input=False, include_url=False, include_context=False
                )
            }
        )
        raise CredentialFormatError("; ".join(problems)) from None


def access_valid(
    credentials: GuardCredentials, now: datetime, margin: timedelta = REFRESH_MARGIN
) -> bool:
    """True when the access token outlives `margin` from now (exactly `margin` left: not valid)."""
    return now + margin < credentials.expires_at


def refresh_form(credentials: GuardCredentials) -> dict[str, str]:
    form = {
        "grant_type": "refresh_token",
        "refresh_token": credentials.refresh_token.get_secret_value(),
        "client_id": credentials.client_id,
    }
    if credentials.send_resource:
        form["resource"] = credentials.resource
    return form


def merge_refreshed(
    credentials: GuardCredentials, refreshed: Refreshed, now: datetime
) -> GuardCredentials:
    """Keep the old refresh token and scope when the answer carries none (not rotated)."""
    return credentials.model_copy(
        update={
            "access_token": refreshed.access_token,
            "refresh_token": refreshed.refresh_token or credentials.refresh_token,
            "expires_at": refreshed.expires_at,
            "scope": refreshed.scope or credentials.scope,
            "obtained_at": now,
        }
    )


def classify_token_response(status: int, body: bytes, now: datetime) -> TokenOutcome:
    """Total over (status, body): anything that is not a clear success is a retry for now."""
    if status == HTTP_OK:
        return _parse_success(body, now.astimezone(UTC))
    if status in (HTTP_BAD_REQUEST, HTTP_UNAUTHORIZED):
        return _parse_error(status, body)
    return Transient(status=status)


def _parse_error(status: int, body: bytes) -> TokenOutcome:
    """RFC 6749 section 5.2 error codes; anything unknown or unparseable is a retry."""
    try:
        raw = json.loads(body)
    except ValueError:
        return Transient(status=status)
    code = raw.get("error") if isinstance(raw, dict) else None
    if code == "invalid_grant":
        return AuthRejected(kind=AuthFailureKind.AUTH_EXPIRED, error_code=code)
    if code == "invalid_client":
        return AuthRejected(kind=AuthFailureKind.AUTH_CLIENT_GONE, error_code=code)
    if isinstance(code, str) and code in RFC6749_ERROR_CODES:
        return AuthRejected(kind=AuthFailureKind.AUTH_REJECTED, error_code=code)
    return Transient(status=status)


def scope_ok(scope: str | None) -> bool:
    """Only the read scopes may be granted. None means the server did not narrow the request."""
    if scope is None:
        return True
    granted = set(scope.split())
    return granted <= set(READ_SCOPES) and granted.isdisjoint(FORBIDDEN_SCOPES)


def auth_failure_text(kind: AuthFailureKind, error_code: str | None = None) -> str:
    """Fixed operator text; only a known RFC 6749 error code may be added, nothing else."""
    text = AUTH_FAILURE_TEXT[kind]
    if kind is AuthFailureKind.AUTH_REJECTED and error_code in RFC6749_ERROR_CODES:
        return text.replace(":", f" ({error_code}):", 1)
    return text


def _parse_success(body: bytes, now: datetime) -> TokenOutcome:
    try:
        raw = json.loads(body)
    except ValueError:
        return Transient(status=HTTP_OK)
    if not isinstance(raw, dict):
        return Transient(status=HTTP_OK)
    access = raw.get("access_token")
    if not isinstance(access, str) or not access:
        return Transient(status=HTTP_OK)
    token_type = raw.get("token_type")
    if token_type is not None and not (
        isinstance(token_type, str) and token_type.lower() == "bearer"
    ):
        return Transient(status=HTTP_OK)
    expires_in = raw.get("expires_in")
    if expires_in is None:
        lifetime = DEFAULT_ACCESS_LIFETIME
    elif isinstance(expires_in, int) and not isinstance(expires_in, bool) and expires_in > 0:
        lifetime = timedelta(seconds=expires_in)
    else:
        return Transient(status=HTTP_OK)
    refresh = raw.get("refresh_token")
    scope = raw.get("scope")
    return Refreshed(
        access_token=SecretStr(access),
        refresh_token=SecretStr(refresh) if isinstance(refresh, str) and refresh else None,
        expires_at=now + lifetime,
        scope=scope if isinstance(scope, str) else None,
    )
