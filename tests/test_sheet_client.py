"""The Google Sheets reader against a fake HTTPS transport and a synthetic batchGet answer."""

import base64
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr
from sheet_fakes import PROJECT_A, SPREADSHEET_ID

from enji_guard_radar.ports import SheetFailure, SheetUnavailable
from enji_guard_radar.settings import ENV_SHEET_KEY
from enji_guard_radar.sheet_client import (
    GoogleSheetReader,
    ServiceAccountKey,
    key_secrets,
    parse_service_account_key,
)
from enji_guard_radar.telegram import HttpResponse, TransportFailure

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sheets"
NOW = datetime(2026, 11, 27, 10, tzinfo=UTC)
ACCESS_TOKEN = "ya29.fake-access-token-value"
EMAIL = "radar-reader@example-project.iam.gserviceaccount.com"


def make_key_json() -> str:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    return json.dumps(
        {
            "type": "service_account",
            "project_id": "example-project",
            "private_key_id": "abc123",
            "private_key": pem,
            "client_email": EMAIL,
            "client_id": "1",
        }
    )


KEY_JSON = make_key_json()
KEY = SecretStr(KEY_JSON)


class FakeSheetsHttp:
    def __init__(
        self,
        *,
        token: HttpResponse | None = None,
        values: HttpResponse | None = None,
        failure: TransportFailure | None = None,
    ) -> None:
        self.token = token or HttpResponse(200, json.dumps({"access_token": ACCESS_TOKEN}).encode())
        self.values = values or HttpResponse(
            200, (FIXTURE / "values_batch_get.synthetic.json").read_bytes()
        )
        self.failure = failure
        self.requests: list[tuple[str, str, Mapping[str, str], bytes | None]] = []

    def request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
    ) -> HttpResponse:
        self.requests.append((method, url, headers, body))
        if self.failure is not None:
            raise self.failure
        return self.token if method == "POST" else self.values


def reader(http: FakeSheetsHttp, key: SecretStr | None = KEY) -> GoogleSheetReader:
    return GoogleSheetReader(key=key, spreadsheet_id=SPREADSHEET_ID, http=http, clock=lambda: NOW)


def failure_of(http: FakeSheetsHttp, key: SecretStr | None = KEY) -> SheetUnavailable:
    with pytest.raises(SheetUnavailable) as caught:
        reader(http, key).read()
    return caught.value


def test_reads_both_tabs_into_the_radars_own_rows() -> None:
    data = reader(FakeSheetsHttp()).read()
    assert data.projects[0][:3] == ("project_id", "name", "mode")
    assert data.projects[1][0] == PROJECT_A
    assert data.projects[3] == ()
    assert data.globals == (
        ("key", "value"),
        ("digest.weekday", "friday"),
        ("schedule.every_minutes", "60"),
        ("messages.language", ""),
    )


def test_asks_for_the_read_only_scope_and_both_tabs_with_a_bearer_token() -> None:
    http = FakeSheetsHttp()
    reader(http).read()
    (token_call, values_call) = http.requests
    assert token_call[0] == "POST"
    assert urlparse(token_call[1]).netloc == "oauth2.googleapis.com"
    assert token_call[3] is not None
    form = parse_qs(token_call[3].decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    payload = form["assertion"][0].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["scope"] == "https://www.googleapis.com/auth/spreadsheets.readonly"
    assert claims["iss"] == EMAIL
    assert claims["iat"] == int(NOW.timestamp())
    method, url, headers, _ = values_call
    assert method == "GET"
    parsed = urlparse(url)
    assert parsed.path == f"/v4/spreadsheets/{SPREADSHEET_ID}/values:batchGet"
    assert parse_qs(parsed.query)["ranges"] == ["projects", "globals"]
    assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


def test_an_empty_tab_comes_back_as_an_empty_grid() -> None:
    body = {
        "valueRanges": [{"range": "projects!A1:Z1000"}, {"range": "globals!A1:B1000"}],
    }
    data = reader(FakeSheetsHttp(values=HttpResponse(200, json.dumps(body).encode()))).read()
    assert data.projects == ()
    assert data.globals == ()


def test_numbers_and_booleans_become_text() -> None:
    body = {
        "valueRanges": [
            {"range": "projects!A1:B2", "values": [["project_id", "stale_after_days"], ["p", 7]]},
            {"range": "globals!A1:B2", "values": [["key", "value"], ["a", 2.0], ["b", True]]},
        ]
    }
    data = reader(FakeSheetsHttp(values=HttpResponse(200, json.dumps(body).encode()))).read()
    assert data.projects[1] == ("p", "7")
    assert data.globals[1:] == (("a", "2"), ("b", "true"))


@pytest.mark.parametrize(
    ("values_status", "cause"),
    [
        (401, SheetFailure.DENIED),
        (403, SheetFailure.DENIED),
        (404, SheetFailure.NOT_FOUND),
        (429, SheetFailure.NETWORK),
        (503, SheetFailure.NETWORK),
        (400, SheetFailure.RESPONSE),
    ],
)
def test_http_failures_map_to_a_failure_class(values_status: int, cause: SheetFailure) -> None:
    error = failure_of(FakeSheetsHttp(values=HttpResponse(values_status, b"{}")))
    assert error.cause is cause


@pytest.mark.parametrize(
    ("token", "cause"),
    [
        (HttpResponse(400, b'{"error": "invalid_grant"}'), SheetFailure.KEY),
        (HttpResponse(401, b"{}"), SheetFailure.KEY),
        (HttpResponse(500, b"{}"), SheetFailure.NETWORK),
        (HttpResponse(200, b"not json"), SheetFailure.RESPONSE),
        (HttpResponse(200, b'{"access_token": ""}'), SheetFailure.RESPONSE),
    ],
)
def test_token_failures_map_to_a_failure_class(token: HttpResponse, cause: SheetFailure) -> None:
    assert failure_of(FakeSheetsHttp(token=token)).cause is cause


@pytest.mark.parametrize("kind", ["timeout", "network"])
def test_no_http_answer_is_a_network_failure(kind: Literal["timeout", "network"]) -> None:
    error = failure_of(FakeSheetsHttp(failure=TransportFailure(kind, "boom")))
    assert error.cause is SheetFailure.NETWORK


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"{}",
        json.dumps({"valueRanges": [{"range": "projects!A1"}]}).encode(),
        json.dumps({"valueRanges": [{"range": "globals!A1"}, {"range": "projects!A1"}]}).encode(),
        json.dumps(
            {"valueRanges": [{"range": "projects!A1", "values": "x"}, {"range": "globals!A1"}]}
        ).encode(),
    ],
)
def test_a_malformed_answer_is_a_response_failure(body: bytes) -> None:
    assert failure_of(FakeSheetsHttp(values=HttpResponse(200, body))).cause is SheetFailure.RESPONSE


@pytest.mark.parametrize(
    "key",
    [
        None,
        SecretStr("not json"),
        SecretStr('{"client_email": "a@b.c"}'),
        SecretStr(json.dumps({"client_email": EMAIL, "private_key": "not a pem"})),
    ],
)
def test_a_missing_or_unusable_key_names_the_variable_and_makes_no_request(
    key: SecretStr | None,
) -> None:
    http = FakeSheetsHttp()
    error = failure_of(http, key)
    assert error.cause is SheetFailure.KEY
    assert ENV_SHEET_KEY in str(error)
    assert http.requests == []


def test_no_failure_message_carries_a_url_token_or_key(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    http = FakeSheetsHttp(values=HttpResponse(403, b'{"error": "secret detail"}'))
    error = failure_of(http)
    private_key = parse_service_account_key(KEY).private_key.get_secret_value()
    for text in (str(error), caplog.text):
        assert "googleapis.com" not in text
        assert ACCESS_TOKEN not in text
        assert private_key not in text
        assert KEY_JSON not in text
        assert "secret detail" not in text


def test_key_secrets_cover_the_whole_value_and_the_private_key() -> None:
    parsed: ServiceAccountKey = parse_service_account_key(KEY)
    secrets = key_secrets(KEY)
    assert KEY_JSON in secrets
    assert parsed.private_key.get_secret_value() in secrets
    assert key_secrets(None) == ()
    assert key_secrets(SecretStr("not json")) == ("not json",)
