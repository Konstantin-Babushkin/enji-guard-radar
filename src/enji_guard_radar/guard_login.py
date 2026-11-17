"""Guard one-time login core: metadata checks, PKCE, authorize URL, redirect check, grant check.

Pure: no I/O and no clock reads (callers pass `now`). The HTTP calls, the hidden prompt and the
credential file live in `cli_auth`. A `LoginError.reason` is fixed text plus at most an error
code or an HTTP status: never a code, state, verifier, token or the pasted address.
"""

import base64
import hashlib
import hmac
import json
import re
import urllib.parse
from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from enji_guard_radar.guard_auth import (
    GUARD_ISSUER,
    GUARD_MCP_URL,
    READ_SCOPES,
    AuthRejected,
    GuardCredentials,
    Refreshed,
    TokenOutcome,
    scope_ok,
)

REDIRECT_URI = "http://127.0.0.1:8765/callback"
CLIENT_NAME = "enji-guard-radar"
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"

HTTP_OK = 200
HTTP_CREATED = 201
DESCRIPTION_LIMIT = 200
HTTPS = "https://"
VERIFIER_PATTERN = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
ERROR_CODE_PATTERN = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
NO_REFRESH_TOKEN = (
    "no refresh token was issued: unattended runs are impossible; the login was not stored"
)


class LoginError(Exception):
    """The login cannot be completed. `reason` is safe to log; `detail` is for the operator."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail

    def operator_text(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


class ServerMetadata(BaseModel):
    """The validated Fleet authorization-server endpoints."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    revocation_endpoint: str | None = None


def parse_document(status: int, body: bytes, *, what: str) -> object:
    """JSON of a discovery answer; anything else is a fixed-text failure."""
    if status != HTTP_OK:
        message = f"{what} request failed (HTTP {status})"
        raise LoginError(message)
    try:
        return json.loads(body)
    except ValueError:
        message = f"{what} is not valid JSON"
        raise LoginError(message) from None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def validate_protected_resource(doc: object) -> str:
    """Return the pinned issuer when the document names Guard's MCP resource and Fleet."""
    if not isinstance(doc, dict) or doc.get("resource") != GUARD_MCP_URL:
        message = "protected resource metadata does not describe the Guard MCP resource"
        raise LoginError(message)
    servers = _string_list(doc.get("authorization_servers"))
    if not servers or servers[0] != GUARD_ISSUER:
        message = "protected resource metadata names an unexpected authorization server"
        raise LoginError(message)
    return servers[0]


def _https_endpoint(doc: Mapping[str, object], key: str) -> str:
    value = doc.get(key)
    if not isinstance(value, str) or not value.startswith(HTTPS):
        message = f"authorization server metadata has no https {key}"
        raise LoginError(message)
    return value


def validate_server_metadata(doc: object, *, expected_issuer: str) -> ServerMetadata:
    if not isinstance(doc, dict) or doc.get("issuer") != expected_issuer:
        message = "authorization server metadata has an unexpected issuer"
        raise LoginError(message)
    if "S256" not in _string_list(doc.get("code_challenge_methods_supported")):
        message = "authorization server does not support PKCE S256"
        raise LoginError(message)
    grants = _string_list(doc.get("grant_types_supported"))
    if "authorization_code" not in grants or "refresh_token" not in grants:
        message = "authorization server does not support authorization_code and refresh_token"
        raise LoginError(message)
    if "none" not in _string_list(doc.get("token_endpoint_auth_methods_supported")):
        message = "authorization server does not support public clients"
        raise LoginError(message)
    return ServerMetadata(
        issuer=expected_issuer,
        authorization_endpoint=_https_endpoint(doc, "authorization_endpoint"),
        token_endpoint=_https_endpoint(doc, "token_endpoint"),
        registration_endpoint=_https_endpoint(doc, "registration_endpoint"),
        revocation_endpoint=(
            _https_endpoint(doc, "revocation_endpoint") if "revocation_endpoint" in doc else None
        ),
    )


def registration_request() -> dict[str, object]:
    return {
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": " ".join(READ_SCOPES),
    }


def _safe_code(value: object) -> str:
    """An error code from the server, only if it looks like one."""
    if isinstance(value, str) and ERROR_CODE_PATTERN.fullmatch(value):
        return value
    return "unknown_error"


def _printable(text: str, limit: int) -> str:
    return "".join(ch if ch.isprintable() else " " for ch in text)[:limit]


def parse_registration(status: int, body: bytes) -> str:
    """The new client id, or a LoginError naming the server's error code."""
    try:
        raw: object = json.loads(body)
    except ValueError:
        raw = None
    if status in (HTTP_OK, HTTP_CREATED):
        client_id = raw.get("client_id") if isinstance(raw, dict) else None
        if isinstance(client_id, str) and client_id:
            return client_id
        message = "client registration answer has no client_id"
        raise LoginError(message)
    if isinstance(raw, dict) and "error" in raw:
        description = raw.get("error_description")
        detail = _printable(description, DESCRIPTION_LIMIT) if isinstance(description, str) else ""
        reason = f"client registration refused ({_safe_code(raw.get('error'))})"
        raise LoginError(reason, detail)
    message = f"client registration failed (HTTP {status})"
    raise LoginError(message)


def code_challenge(verifier: str) -> str:
    if not VERIFIER_PATTERN.fullmatch(verifier):
        message = "PKCE verifier must be 43 to 128 unreserved characters"
        raise LoginError(message)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def authorize_url(
    meta: ServerMetadata, *, client_id: str, state: str, challenge: str, send_resource: bool
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(READ_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if send_resource:
        params["resource"] = GUARD_MCP_URL
    return f"{meta.authorization_endpoint}?{urllib.parse.urlencode(params)}"


def _single(query: Mapping[str, list[str]], key: str) -> str | None:
    values = query.get(key, [])
    return values[0] if len(values) == 1 else None


def parse_redirect(pasted: str, *, expected_state: str, issuer: str) -> str:
    """The authorization code from the pasted redirect address, after every check."""
    expected = urllib.parse.urlsplit(REDIRECT_URI)
    try:
        parts = urllib.parse.urlsplit(pasted.strip())
        same = (
            parts.scheme == expected.scheme
            and parts.hostname == expected.hostname
            and parts.port == expected.port
            and parts.path == expected.path
        )
    except ValueError:
        same = False
    if not same:
        message = "the pasted address is not the expected redirect address"
        raise LoginError(message)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if "error" in query:
        reason = f"authorization was refused ({_safe_code(_single(query, 'error'))})"
        raise LoginError(reason)
    state = _single(query, "state")
    if state is None or not hmac.compare_digest(state.encode(), expected_state.encode()):
        message = "the pasted address has a missing or wrong state"
        raise LoginError(message)
    if _single(query, "iss") != issuer:
        message = "the pasted address has a missing or wrong iss"
        raise LoginError(message)
    code = _single(query, "code")
    if not code:
        message = "the pasted address has no code"
        raise LoginError(message)
    return code


def code_exchange_form(
    *, code: str, client_id: str, verifier: str, send_resource: bool
) -> dict[str, str]:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if send_resource:
        form["resource"] = GUARD_MCP_URL
    return form


def credentials_from_grant(
    outcome: TokenOutcome,
    meta: ServerMetadata,
    *,
    client_id: str,
    send_resource: bool,
    now: datetime,
) -> GuardCredentials:
    """Credentials to store, or a LoginError: nothing unsafe or unusable gets through."""
    if isinstance(outcome, AuthRejected):
        code = _safe_code(outcome.error_code)
        message = f"the token request was refused ({outcome.kind.value}, {code})"
        raise LoginError(message)
    if not isinstance(outcome, Refreshed):
        message = f"the token endpoint gave no usable answer (HTTP {outcome.status})"
        raise LoginError(message)
    if not scope_ok(outcome.scope):
        message = "the grant is broader than read-only (projects:read audits:read): not stored"
        raise LoginError(message)
    if outcome.refresh_token is None:
        raise LoginError(NO_REFRESH_TOKEN)
    return GuardCredentials(
        issuer=meta.issuer,
        token_endpoint=meta.token_endpoint,
        revocation_endpoint=meta.revocation_endpoint,
        resource=GUARD_MCP_URL,
        send_resource=send_resource,
        client_id=client_id,
        scope=outcome.scope or " ".join(READ_SCOPES),
        access_token=outcome.access_token,
        refresh_token=outcome.refresh_token,
        expires_at=outcome.expires_at,
        obtained_at=now,
    )


def grant_summary(creds: GuardCredentials) -> str:
    lifetime = int((creds.expires_at - creds.obtained_at).total_seconds())
    return (
        f"stored: scope={creds.scope}, access token lifetime {lifetime} s, "
        "refresh token issued: yes"
    )
