"""Guard login core: token-endpoint classification, scope check, credential file format."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fake_oauth import (
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    ROTATED_REFRESH_TOKEN,
    make_credentials,
)

from enji_guard_radar.guard_auth import (
    AUTH_FAILURE_TEXT,
    DEFAULT_ACCESS_LIFETIME,
    AuthRejected,
    CredentialFormatError,
    Refreshed,
    Transient,
    access_valid,
    auth_failure_text,
    classify_token_response,
    deserialize_credentials,
    merge_refreshed,
    refresh_form,
    scope_ok,
    serialize_credentials,
)
from enji_guard_radar.ports import AuthFailureKind

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def body(**fields: object) -> bytes:
    return json.dumps(fields).encode()


def refreshed(
    access: str = "a", refresh: str | None = "r", lifetime: timedelta = timedelta(hours=1)
) -> Refreshed:
    outcome = classify_token_response(
        200,
        body(
            access_token=access,
            token_type="Bearer",
            expires_in=int(lifetime.total_seconds()),
            **({"refresh_token": refresh} if refresh else {}),
            scope="projects:read audits:read",
        ),
        NOW,
    )
    assert isinstance(outcome, Refreshed)
    return outcome


def test_success_with_rotation_keeps_refresh_token_and_scope() -> None:
    outcome = refreshed()
    assert outcome.access_token.get_secret_value() == "a"
    assert outcome.refresh_token is not None
    assert outcome.refresh_token.get_secret_value() == "r"
    assert outcome.expires_at == NOW + timedelta(hours=1)
    assert outcome.scope == "projects:read audits:read"


def test_success_without_refresh_token_is_not_rotated() -> None:
    outcome = refreshed(refresh=None)
    assert outcome.refresh_token is None
    creds = make_credentials(expires_at=NOW, obtained_at=NOW - timedelta(hours=1))
    merged = merge_refreshed(creds, outcome, NOW)
    assert merged.refresh_token.get_secret_value() == FAKE_REFRESH_TOKEN
    assert merged.access_token.get_secret_value() == "a"
    assert merged.obtained_at == NOW


def test_merge_replaces_the_refresh_token_when_one_is_issued() -> None:
    creds = make_credentials(expires_at=NOW, obtained_at=NOW - timedelta(hours=1))
    merged = merge_refreshed(creds, refreshed(refresh=ROTATED_REFRESH_TOKEN), NOW)
    assert merged.refresh_token.get_secret_value() == ROTATED_REFRESH_TOKEN


def test_missing_expires_in_uses_the_default_lifetime() -> None:
    outcome = classify_token_response(200, body(access_token="a"), NOW)
    assert isinstance(outcome, Refreshed)
    assert outcome.expires_at == NOW + DEFAULT_ACCESS_LIFETIME


@pytest.mark.parametrize("expires_in", [0, -5, "3600", True, 1.5])
def test_bad_expires_in_is_transient(expires_in: object) -> None:
    outcome = classify_token_response(200, body(access_token="a", expires_in=expires_in), NOW)
    assert outcome == Transient(status=200)


@pytest.mark.parametrize(
    "raw",
    [
        body(access_token="a", token_type="DPoP"),
        b"{",
        b"[]",
        b"",
        body(token_type="Bearer"),
        body(access_token=""),
        body(access_token=5),
    ],
    ids=["dpop", "bad-json", "list", "empty", "no-token", "empty-token", "int-token"],
)
def test_unusable_success_answers_are_transient(raw: bytes) -> None:
    assert classify_token_response(200, raw, NOW) == Transient(status=200)


@pytest.mark.parametrize(
    ("status", "code", "kind"),
    [
        (400, "invalid_grant", AuthFailureKind.AUTH_EXPIRED),
        (401, "invalid_grant", AuthFailureKind.AUTH_EXPIRED),
        (400, "invalid_client", AuthFailureKind.AUTH_CLIENT_GONE),
        (401, "invalid_client", AuthFailureKind.AUTH_CLIENT_GONE),
        (400, "invalid_request", AuthFailureKind.AUTH_REJECTED),
        (400, "unauthorized_client", AuthFailureKind.AUTH_REJECTED),
        (400, "unsupported_grant_type", AuthFailureKind.AUTH_REJECTED),
        (400, "invalid_scope", AuthFailureKind.AUTH_REJECTED),
    ],
)
def test_error_codes_need_a_new_login(status: int, code: str, kind: AuthFailureKind) -> None:
    outcome = classify_token_response(status, body(error=code), NOW)
    assert outcome == AuthRejected(kind=kind, error_code=code)


@pytest.mark.parametrize(
    ("status", "raw"),
    [
        (400, body(error="something_new")),
        (400, b"<html>bad gateway</html>"),
        (400, b"[]"),
        (400, body(error=7)),
        (429, body(error="invalid_grant")),
        (500, b""),
        (503, b"unavailable"),
        (302, b""),
    ],
)
def test_everything_else_is_a_retry(status: int, raw: bytes) -> None:
    assert classify_token_response(status, raw, NOW) == Transient(status=status)


@pytest.mark.parametrize(
    "scope",
    [None, "projects:read", "audits:read projects:read", "projects:read audits:read", ""],
)
def test_scope_ok_accepts_read_only_scopes(scope: str | None) -> None:
    assert scope_ok(scope)


@pytest.mark.parametrize(
    "scope", ["projects:read audits:run", "audits:run", "projects:read openid", "admin"]
)
def test_scope_ok_refuses_anything_beyond_read(scope: str) -> None:
    assert not scope_ok(scope)


def test_rejected_text_names_a_known_error_code_only() -> None:
    assert auth_failure_text(AuthFailureKind.AUTH_REJECTED, "invalid_scope") == (
        "Guard login rejected (invalid_scope): run `enji-guard-radar auth`"
    )
    assert (
        auth_failure_text(AuthFailureKind.AUTH_REJECTED, "token=abc")
        == (AUTH_FAILURE_TEXT[AuthFailureKind.AUTH_REJECTED])
    )
    assert (
        auth_failure_text(AuthFailureKind.AUTH_EXPIRED, "invalid_scope")
        == (AUTH_FAILURE_TEXT[AuthFailureKind.AUTH_EXPIRED])
    )


@pytest.mark.parametrize("kind", list(AuthFailureKind))
def test_failure_text_names_the_auth_command_and_no_url(kind: AuthFailureKind) -> None:
    assert "enji-guard-radar auth" in AUTH_FAILURE_TEXT[kind]
    assert "http" not in AUTH_FAILURE_TEXT[kind]


def test_credentials_round_trip_and_keep_secrets_out_of_reprs() -> None:
    creds = make_credentials(expires_at=NOW, obtained_at=NOW - timedelta(hours=1))
    data = serialize_credentials(creds)
    assert FAKE_ACCESS_TOKEN.encode() in data
    assert FAKE_REFRESH_TOKEN.encode() in data
    assert data.endswith(b"\n")
    assert deserialize_credentials(data) == creds
    for text in (repr(creds), str(creds)):
        assert FAKE_ACCESS_TOKEN not in text
        assert FAKE_REFRESH_TOKEN not in text


def _tampered(**changes: object) -> bytes:
    raw = json.loads(
        serialize_credentials(make_credentials(expires_at=NOW, obtained_at=NOW)).decode()
    )
    for key, value in changes.items():
        if value is None:
            raw.pop(key)
        else:
            raw[key] = value
    return json.dumps(raw).encode()


@pytest.mark.parametrize(
    "data",
    [
        b"{",
        b"[]",
        b"",
        _tampered(refresh_token=None),
        _tampered(schema_version=2),
        _tampered(token_endpoint="http://fleet.enji.ai/api/v1/oauth/token"),
        _tampered(expires_at="2026-11-17T12:00:00"),
        _tampered(surprise="x"),
    ],
    ids=["brace", "list", "empty", "no-refresh", "schema2", "http", "naive-time", "extra-field"],
)
def test_invalid_credential_files_are_reported_without_secrets(data: bytes) -> None:
    with pytest.raises(CredentialFormatError) as caught:
        deserialize_credentials(data)
    reason = caught.value.reason
    assert reason
    for secret in (FAKE_ACCESS_TOKEN, FAKE_REFRESH_TOKEN, "fake-client-id", "fleet.enji.ai"):
        assert secret not in reason
        assert secret not in str(caught.value)


def test_access_valid_boundary_is_120_seconds() -> None:
    margin = timedelta(seconds=120)
    at_edge = make_credentials(expires_at=NOW + margin, obtained_at=NOW)
    just_past = make_credentials(expires_at=NOW + margin + timedelta(seconds=1), obtained_at=NOW)
    assert not access_valid(at_edge, NOW)
    assert access_valid(just_past, NOW)


@pytest.mark.parametrize("send_resource", [True, False])
def test_refresh_form_sends_resource_only_when_asked(send_resource: bool) -> None:
    creds = make_credentials(expires_at=NOW, obtained_at=NOW, send_resource=send_resource)
    form = refresh_form(creds)
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == FAKE_REFRESH_TOKEN
    assert form["client_id"] == "fake-client-id"
    assert ("resource" in form) is send_resource
