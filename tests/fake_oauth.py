"""Fakes for the Guard token layer: no network, no real time, obviously fake tokens."""

import json
from collections.abc import Mapping
from datetime import datetime

from pydantic import SecretStr

from enji_guard_radar.guard_auth import GUARD_ISSUER, GUARD_MCP_URL, GuardCredentials
from enji_guard_radar.telegram import HttpResponse, TransportFailure

FAKE_ACCESS_TOKEN = "fake-access-token-0123456789abcdefghij"
FAKE_REFRESH_TOKEN = "fake-refresh-token-0123456789abcdefghij"
ROTATED_ACCESS_TOKEN = "fake-rotated-access-token-9876543210zyxw"
ROTATED_REFRESH_TOKEN = "fake-rotated-refresh-token-9876543210zyxw"
TOKEN_ENDPOINT = "https://fleet.enji.ai/api/v1/oauth/token"


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
