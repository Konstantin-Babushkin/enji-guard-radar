"""Pure rows for the Guard login core: metadata, PKCE, redirect, registration, grant."""

import copy
import json
import urllib.parse
from datetime import UTC, datetime, timedelta

import pytest
from fake_oauth import (
    AS_METADATA_DOC,
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    PRM_DOC,
)
from pydantic import SecretStr

from enji_guard_radar.guard_auth import (
    GUARD_ISSUER,
    GUARD_MCP_URL,
    AuthRejected,
    Refreshed,
    Transient,
)
from enji_guard_radar.guard_login import (
    REDIRECT_URI,
    LoginError,
    ServerMetadata,
    authorize_url,
    code_challenge,
    code_exchange_form,
    credentials_from_grant,
    grant_summary,
    parse_document,
    parse_redirect,
    parse_registration,
    registration_request,
    validate_protected_resource,
    validate_server_metadata,
)
from enji_guard_radar.ports import AuthFailureKind

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
STATE = "state-value-0001"
CODE = "fake-code-0001"
ISS = "https%3A%2F%2Ffleet.enji.ai"
GOOD = f"{REDIRECT_URI}?code={CODE}&state={STATE}&iss={ISS}"
RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def metadata() -> ServerMetadata:
    return validate_server_metadata(AS_METADATA_DOC, expected_issuer=GUARD_ISSUER)


def broken(doc: dict[str, object], **changes: object) -> dict[str, object]:
    copied = copy.deepcopy(doc)
    for key, value in changes.items():
        if value is None:
            copied.pop(key, None)
        else:
            copied[key] = value
    return copied


# --- protected resource and server metadata ---------------------------------------------------


def test_protected_resource_returns_the_pinned_issuer() -> None:
    assert validate_protected_resource(PRM_DOC) == GUARD_ISSUER


@pytest.mark.parametrize(
    "doc",
    [
        broken(PRM_DOC, resource="https://other.example/mcp"),
        broken(PRM_DOC, authorization_servers=[]),
        broken(PRM_DOC, authorization_servers=["https://evil.example"]),
        broken(PRM_DOC, authorization_servers=None),
    ],
    ids=["other_resource", "no_servers", "other_issuer", "servers_missing"],
)
def test_protected_resource_rejects(doc: dict[str, object]) -> None:
    with pytest.raises(LoginError):
        validate_protected_resource(doc)


def test_protected_resource_rejects_a_non_object() -> None:
    with pytest.raises(LoginError):
        validate_protected_resource(["not", "an", "object"])


def test_server_metadata_accepts_the_public_fleet_document() -> None:
    meta = metadata()
    assert meta.token_endpoint == "https://fleet.enji.ai/api/v1/oauth/token"
    assert meta.registration_endpoint == "https://fleet.enji.ai/api/v1/oauth/register"


@pytest.mark.parametrize(
    "doc",
    [
        broken(AS_METADATA_DOC, issuer="https://evil.example"),
        broken(AS_METADATA_DOC, token_endpoint="http://fleet.enji.ai/api/v1/oauth/token"),
        broken(AS_METADATA_DOC, authorization_endpoint=None),
        broken(AS_METADATA_DOC, code_challenge_methods_supported=["plain"]),
        broken(AS_METADATA_DOC, grant_types_supported=["authorization_code"]),
        broken(AS_METADATA_DOC, token_endpoint_auth_methods_supported=["client_secret_post"]),
        broken(AS_METADATA_DOC, registration_endpoint=None),
    ],
    ids=[
        "issuer_mismatch",
        "http_token_endpoint",
        "no_authorization_endpoint",
        "no_s256",
        "no_refresh_grant",
        "no_public_client_method",
        "no_registration_endpoint",
    ],
)
def test_server_metadata_rejects(doc: dict[str, object]) -> None:
    with pytest.raises(LoginError):
        validate_server_metadata(doc, expected_issuer=GUARD_ISSUER)


@pytest.mark.parametrize(("status", "body"), [(404, b"{}"), (200, b"not json")])
def test_parse_document_rejects_bad_answers(status: int, body: bytes) -> None:
    with pytest.raises(LoginError):
        parse_document(status, body, what="x")


# --- registration -----------------------------------------------------------------------------


def test_registration_request_asks_for_a_public_read_only_client() -> None:
    request = registration_request()
    assert request["scope"] == "projects:read audits:read"
    assert request["token_endpoint_auth_method"] == "none"
    assert request["redirect_uris"] == [REDIRECT_URI]
    assert "audits:run" not in json.dumps(request)


def test_parse_registration_returns_the_client_id() -> None:
    assert parse_registration(201, b'{"client_id": "abc"}') == "abc"


def test_parse_registration_names_the_error_code_and_cuts_the_description() -> None:
    body = json.dumps({"error": "invalid_redirect_uri", "error_description": "d" * 300}).encode()
    with pytest.raises(LoginError) as caught:
        parse_registration(400, body)
    assert "invalid_redirect_uri" in caught.value.reason
    assert caught.value.detail == "d" * 200
    assert "dddd" not in caught.value.reason


@pytest.mark.parametrize(
    ("status", "body"), [(201, b"{}"), (201, b"nope"), (500, b"oops"), (400, b'{"error": "a b c"}')]
)
def test_parse_registration_rejects_unusable_answers(status: int, body: bytes) -> None:
    with pytest.raises(LoginError):
        parse_registration(status, body)


# --- PKCE and the authorize URL ---------------------------------------------------------------


def test_code_challenge_matches_the_rfc_7636_vector() -> None:
    assert code_challenge(RFC_VERIFIER) == RFC_CHALLENGE


@pytest.mark.parametrize("verifier", [RFC_VERIFIER[:-1], RFC_VERIFIER[:-1] + "+", "a" * 129, ""])
def test_code_challenge_rejects_a_bad_verifier(verifier: str) -> None:
    with pytest.raises(LoginError):
        code_challenge(verifier)


def test_authorize_url_carries_pkce_scope_and_resource() -> None:
    url = authorize_url(
        metadata(), client_id="c1", state=STATE, challenge=RFC_CHALLENGE, send_resource=True
    )
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parts.query)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == metadata().authorization_endpoint
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["projects:read audits:read"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["code_challenge"] == [RFC_CHALLENGE]
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == [GUARD_MCP_URL]


def test_authorize_url_without_resource_indicator() -> None:
    url = authorize_url(
        metadata(), client_id="c1", state=STATE, challenge=RFC_CHALLENGE, send_resource=False
    )
    assert "resource" not in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


@pytest.mark.parametrize("send_resource", [True, False])
def test_code_exchange_form(send_resource: bool) -> None:
    form = code_exchange_form(
        code=CODE, client_id="c1", verifier=RFC_VERIFIER, send_resource=send_resource
    )
    assert form["grant_type"] == "authorization_code"
    assert form["code_verifier"] == RFC_VERIFIER
    assert form["redirect_uri"] == REDIRECT_URI
    assert ("resource" in form) is send_resource


# --- the pasted redirect address --------------------------------------------------------------


def test_parse_redirect_returns_the_code() -> None:
    assert parse_redirect(f"  {GOOD}\n", expected_state=STATE, issuer=GUARD_ISSUER) == CODE


@pytest.mark.parametrize(
    "pasted",
    [
        f"{REDIRECT_URI}?code={CODE}&state=wrong&iss={ISS}",
        f"{REDIRECT_URI}?code={CODE}&iss={ISS}",
        f"{GOOD}&state=again",
        f"{REDIRECT_URI}?code={CODE}&state={STATE}",
        f"{REDIRECT_URI}?code={CODE}&state={STATE}&iss=https%3A%2F%2Fevil.example",
        f"{GOOD}&iss={ISS}",
        f"{REDIRECT_URI}?error=access_denied&state={STATE}&iss={ISS}",
        f"{REDIRECT_URI}?state={STATE}&iss={ISS}",
        f"{REDIRECT_URI}?code=&state={STATE}&iss={ISS}",
        GOOD.replace("http://", "https://", 1),
        GOOD.replace("127.0.0.1", "localhost", 1),
        GOOD.replace(":8765", ":8766", 1),
        GOOD.replace("/callback", "/other", 1),
        GOOD.replace(":8765", ":notaport", 1),
        "not an address at all",
        "",
    ],
    ids=[
        "wrong_state",
        "missing_state",
        "two_state_values",
        "missing_iss",
        "foreign_iss",
        "two_iss_values",
        "error_param",
        "missing_code",
        "empty_code",
        "https_scheme",
        "other_host",
        "other_port",
        "other_path",
        "bad_port",
        "garbage",
        "empty",
    ],
)
def test_parse_redirect_rejects_and_never_echoes_the_input(pasted: str) -> None:
    with pytest.raises(LoginError) as caught:
        parse_redirect(pasted, expected_state=STATE, issuer=GUARD_ISSUER)
    shown = caught.value.operator_text()
    for secret in (CODE, STATE, pasted):
        if secret:
            assert secret not in shown


def test_parse_redirect_error_reason_names_the_error_code() -> None:
    pasted = f"{REDIRECT_URI}?error=access_denied&state={STATE}&iss={ISS}"
    with pytest.raises(LoginError) as caught:
        parse_redirect(pasted, expected_state=STATE, issuer=GUARD_ISSUER)
    assert "access_denied" in caught.value.reason


# --- the grant --------------------------------------------------------------------------------


def refreshed(
    *, refresh: str | None = FAKE_REFRESH_TOKEN, scope: str | None = "projects:read audits:read"
) -> Refreshed:
    return Refreshed(
        access_token=SecretStr(FAKE_ACCESS_TOKEN),
        refresh_token=SecretStr(refresh) if refresh else None,
        expires_at=NOW + timedelta(seconds=3600),
        scope=scope,
    )


def test_credentials_from_grant_builds_the_stored_login() -> None:
    creds = credentials_from_grant(
        refreshed(), metadata(), client_id="c1", send_resource=True, now=NOW
    )
    assert creds.issuer == GUARD_ISSUER
    assert creds.resource == GUARD_MCP_URL
    assert creds.client_id == "c1"
    assert creds.refresh_token.get_secret_value() == FAKE_REFRESH_TOKEN
    assert creds.obtained_at == NOW
    assert grant_summary(creds) == (
        "stored: scope=projects:read audits:read, access token lifetime 3600 s, "
        "refresh token issued: yes"
    )


def test_credentials_from_grant_defaults_the_scope_and_keeps_send_resource() -> None:
    creds = credentials_from_grant(
        refreshed(scope=None), metadata(), client_id="c1", send_resource=False, now=NOW
    )
    assert creds.scope == "projects:read audits:read"
    assert creds.send_resource is False


def test_credentials_from_grant_refuses_a_grant_without_a_refresh_token() -> None:
    with pytest.raises(LoginError) as caught:
        credentials_from_grant(
            refreshed(refresh=None), metadata(), client_id="c1", send_resource=True, now=NOW
        )
    assert caught.value.reason == (
        "no refresh token was issued: unattended runs are impossible; the login was not stored"
    )


@pytest.mark.parametrize(
    "scope",
    ["projects:read audits:read audits:run", "audits:run", "projects:write", "projects:read x"],
    ids=["audits_run_added", "audits_run_only", "other_scope", "unknown_extra"],
)
def test_credentials_from_grant_refuses_audits_run_and_broader_scopes(scope: str) -> None:
    with pytest.raises(LoginError) as caught:
        credentials_from_grant(
            refreshed(scope=scope), metadata(), client_id="c1", send_resource=True, now=NOW
        )
    assert "broader than read-only" in caught.value.reason
    assert scope not in caught.value.reason


def test_credentials_from_grant_reports_a_rejected_token_request() -> None:
    outcome = AuthRejected(kind=AuthFailureKind.AUTH_EXPIRED, error_code="invalid_grant")
    with pytest.raises(LoginError) as caught:
        credentials_from_grant(outcome, metadata(), client_id="c1", send_resource=True, now=NOW)
    assert "invalid_grant" in caught.value.reason


def test_credentials_from_grant_reports_a_transient_answer_by_status() -> None:
    with pytest.raises(LoginError) as caught:
        credentials_from_grant(
            Transient(status=503), metadata(), client_id="c1", send_resource=True, now=NOW
        )
    assert "HTTP 503" in caught.value.reason
