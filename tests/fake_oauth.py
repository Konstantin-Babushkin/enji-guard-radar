"""Fakes for the Guard token layer: no network, no real time, obviously fake tokens."""

import json
from collections.abc import Mapping
from datetime import datetime

from pydantic import SecretStr

from enji_guard_radar.guard_auth import (
    GUARD_ISSUER,
    GUARD_MCP_URL,
    GUARD_PRM_URL,
    GuardCredentials,
)
from enji_guard_radar.guard_login import AS_METADATA_PATH
from enji_guard_radar.telegram import HttpResponse, TransportFailure

FAKE_ACCESS_TOKEN = "fake-access-token-0123456789abcdefghij"
FAKE_REFRESH_TOKEN = "fake-refresh-token-0123456789abcdefghij"
ROTATED_ACCESS_TOKEN = "fake-rotated-access-token-9876543210zyxw"
ROTATED_REFRESH_TOKEN = "fake-rotated-refresh-token-9876543210zyxw"
TOKEN_ENDPOINT = "https://fleet.enji.ai/api/v1/oauth/token"
REGISTRATION_ENDPOINT = "https://fleet.enji.ai/api/v1/oauth/register"
AS_METADATA_URL = GUARD_ISSUER + AS_METADATA_PATH

# Public Fleet metadata (02-RESEARCH.md Pattern 3), verbatim.
PRM_DOC: dict[str, object] = {
    "resource": "https://guard.enji.ai/mcp",
    "authorization_servers": ["https://fleet.enji.ai"],
    "scopes_supported": ["projects:read", "audits:read", "audits:run"],
    "bearer_methods_supported": ["header"],
}
AS_METADATA_DOC: dict[str, object] = {
    "issuer": "https://fleet.enji.ai",
    "authorization_endpoint": "https://fleet.enji.ai/api/v1/oauth/authorize",
    "token_endpoint": TOKEN_ENDPOINT,
    "registration_endpoint": REGISTRATION_ENDPOINT,
    "revocation_endpoint": "https://fleet.enji.ai/api/v1/oauth/revoke",
    "code_challenge_methods_supported": ["S256"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "response_types_supported": ["code"],
    "token_endpoint_auth_methods_supported": ["none"],
    "authorization_response_iss_parameter_supported": True,
}


class FakeOAuthTransport:
    """Scripted token endpoint: records (url, form) and fails loudly on an unexpected request."""

    def __init__(self, script: list[HttpResponse | TransportFailure]) -> None:
        self._script = list(script)
        self.requests: list[tuple[str, dict[str, str]]] = []

    def post_form(self, url: str, form: Mapping[str, str], timeout_s: float) -> HttpResponse:
        del timeout_s
        self.requests.append((url, dict(form)))
        if not self._script:
            message = "FakeOAuthTransport script ran out: unexpected extra request"
            raise AssertionError(message)
        item = self._script.pop(0)
        if isinstance(item, TransportFailure):
            raise item
        return item


def token_body(
    access: str,
    refresh: str | None = None,
    expires_in: int = 3600,
    scope: str | None = "projects:read audits:read",
) -> HttpResponse:
    body: dict[str, object] = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": expires_in,
    }
    if refresh is not None:
        body["refresh_token"] = refresh
    if scope is not None:
        body["scope"] = scope
    return HttpResponse(status=200, body=json.dumps(body).encode())


def oauth_error(status: int, code: str) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps({"error": code}).encode())


def make_credentials(
    *,
    expires_at: datetime,
    obtained_at: datetime,
    access: str = FAKE_ACCESS_TOKEN,
    refresh: str = FAKE_REFRESH_TOKEN,
    send_resource: bool = True,
) -> GuardCredentials:
    return GuardCredentials(
        issuer=GUARD_ISSUER,
        token_endpoint=TOKEN_ENDPOINT,
        revocation_endpoint="https://fleet.enji.ai/api/v1/oauth/revoke",
        resource=GUARD_MCP_URL,
        send_resource=send_resource,
        client_id="fake-client-id",
        scope="projects:read audits:read",
        access_token=SecretStr(access),
        refresh_token=SecretStr(refresh),
        expires_at=expires_at,
        obtained_at=obtained_at,
    )


def json_response(doc: object, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, body=json.dumps(doc).encode())


def registration_body(client_id: str = "fake-client-0001") -> HttpResponse:
    return json_response({"client_id": client_id}, status=201)


def login_routes(
    *, token: HttpResponse | TransportFailure | None = None
) -> dict[tuple[str, str], list[HttpResponse | TransportFailure]]:
    """The four answers of a good login; a test overrides one entry to break it."""
    return {
        ("GET", GUARD_PRM_URL): [json_response(PRM_DOC)],
        ("GET", AS_METADATA_URL): [json_response(AS_METADATA_DOC)],
        ("POST", REGISTRATION_ENDPOINT): [registration_body()],
        ("POST", TOKEN_ENDPOINT): [
            token if token is not None else token_body(FAKE_ACCESS_TOKEN, FAKE_REFRESH_TOKEN)
        ],
    }


class FakeLoginTransport:
    """Scripted login servers keyed by (method, url); records every request."""

    def __init__(
        self, routes: dict[tuple[str, str], list[HttpResponse | TransportFailure]]
    ) -> None:
        self._routes = {key: list(answers) for key, answers in routes.items()}
        self.requests: list[tuple[str, str, object]] = []

    def _answer(self, method: str, url: str, payload: object) -> HttpResponse:
        self.requests.append((method, url, payload))
        answers = self._routes.get((method, url))
        if not answers:
            message = f"FakeLoginTransport: unexpected request {method} {url}"
            raise AssertionError(message)
        item = answers.pop(0)
        if isinstance(item, TransportFailure):
            raise item
        return item

    def get_json(self, url: str, timeout_s: float) -> HttpResponse:
        del timeout_s
        return self._answer("GET", url, None)

    def post_json(self, url: str, payload: Mapping[str, object], timeout_s: float) -> HttpResponse:
        del timeout_s
        return self._answer("POST", url, dict(payload))

    def post_form(self, url: str, form: Mapping[str, str], timeout_s: float) -> HttpResponse:
        del timeout_s
        return self._answer("POST", url, dict(form))

    def payloads(self, url: str) -> list[object]:
        return [payload for _method, requested, payload in self.requests if requested == url]
