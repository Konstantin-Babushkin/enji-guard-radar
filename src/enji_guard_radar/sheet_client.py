"""The Google Sheets reader: service account login and a read-only fetch of the two tabs.

Shell module, and the only one that knows Google: the key file layout, the token exchange and the
Sheets answer shape stop here. It returns the radar's own `SheetData` or raises SheetUnavailable
with a sanitized message (never a URL, a key, a token or a cell). The key is signed with
google-auth; the two HTTPS calls are plain requests through a transport the tests replace.
"""

import contextlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Literal, Protocol

from google.auth import crypt, jwt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from enji_guard_radar.config import SheetSettings
from enji_guard_radar.ports import SheetFailure, SheetReader, SheetUnavailable
from enji_guard_radar.settings import ENV_SHEET_KEY
from enji_guard_radar.sheet_settings import GLOBALS_TAB, PROJECTS_TAB, SheetData
from enji_guard_radar.telegram import HttpResponse, TransportFailure

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"
READ_ONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
JWT_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
ASSERTION_LIFETIME_SECONDS = 3600
TIMEOUT_SECONDS = 20.0
HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
TABS = (PROJECTS_TAB, GLOBALS_TAB)


class SheetsHttp(Protocol):
    def request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
    ) -> HttpResponse:
        """Raise TransportFailure when there is no HTTP answer."""
        ...


class UrllibSheetsHttp:
    """stdlib transport; default TLS verification, and no URL in any error it raises."""

    def request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
    ) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return HttpResponse(status=int(response.status), body=response.read())
        except urllib.error.HTTPError as error:
            try:
                return HttpResponse(status=error.code, body=error.read())
            finally:
                error.close()
        except TimeoutError:
            raise TransportFailure("timeout", "request timed out") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise TransportFailure("timeout", "request timed out") from None
            raise TransportFailure("network", f"network error: {type(error).__name__}") from None
        except OSError as error:
            raise TransportFailure("network", f"network error: {type(error).__name__}") from None


class ServiceAccountKey(BaseModel):
    """The two fields of Google's key file the radar needs; the rest of the file is ignored."""

    model_config = ConfigDict(extra="ignore")

    client_email: str = Field(min_length=3)
    private_key: SecretStr


def parse_service_account_key(raw: SecretStr | None) -> ServiceAccountKey:
    if raw is None:
        message = f"service account key is not set: put the key JSON in {ENV_SHEET_KEY}"
        raise SheetUnavailable(message, cause=SheetFailure.KEY)
    try:
        return ServiceAccountKey.model_validate_json(raw.get_secret_value())
    except ValidationError:
        message = f"service account key in {ENV_SHEET_KEY} is not a valid key file"
        raise SheetUnavailable(message, cause=SheetFailure.KEY) from None


def key_secrets(raw: SecretStr | None) -> tuple[str, ...]:
    """What must never reach a log or a message: the whole value and the private key inside."""
    if raw is None:
        return ()
    secrets = [raw.get_secret_value()]
    with contextlib.suppress(SheetUnavailable):
        secrets.append(parse_service_account_key(raw).private_key.get_secret_value())
    return tuple(secrets)


class _ValueRange(BaseModel):
    model_config = ConfigDict(extra="ignore")

    range: str
    values: list[list[str | int | float | bool]] = Field(default_factory=list)


class _BatchGet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    valueRanges: list[_ValueRange]  # noqa: N815 - Google's field name


class GoogleSheetReader:
    """Reads the `projects` and `globals` tabs of one spreadsheet. Built per poll."""

    def __init__(
        self,
        *,
        key: SecretStr | None,
        spreadsheet_id: str,
        http: SheetsHttp,
        clock: Callable[[], datetime],
    ) -> None:
        self._key = key
        self._spreadsheet_id = spreadsheet_id
        self._http = http
        self._clock = clock

    def read(self) -> SheetData:
        token = self._access_token(parse_service_account_key(self._key))
        data = self._fetch(token)
        logger.info(
            "sheet read: %d project row(s), %d globals row(s)",
            max(len(data.projects) - 1, 0),
            max(len(data.globals) - 1, 0),
        )
        return data

    def _access_token(self, key: ServiceAccountKey) -> str:
        issued = int(self._clock().timestamp())
        claims: dict[str, str | int] = {
            "iss": key.client_email,
            "scope": READ_ONLY_SCOPE,
            "aud": TOKEN_URL,
            "iat": issued,
            "exp": issued + ASSERTION_LIFETIME_SECONDS,
        }
        try:
            assertion = _sign(key, claims)
        except (ValueError, TypeError):
            message = f"service account key in {ENV_SHEET_KEY} cannot sign: not an RSA private key"
            raise SheetUnavailable(message, cause=SheetFailure.KEY) from None
        form = urllib.parse.urlencode({"grant_type": JWT_GRANT, "assertion": assertion}).encode()
        response = self._send(
            "POST",
            TOKEN_URL,
            {"Content-Type": "application/x-www-form-urlencoded"},
            form,
        )
        if response.status in (HTTP_BAD_REQUEST, HTTP_UNAUTHORIZED):
            message = "Google rejected the service account key"
            raise SheetUnavailable(message, cause=SheetFailure.KEY)
        _require_ok(response)
        try:
            body = json.loads(response.body)
            token = body["access_token"]
        except (ValueError, KeyError, TypeError):
            message = "Google token answer has no access token"
            raise SheetUnavailable(message, cause=SheetFailure.RESPONSE) from None
        if not isinstance(token, str) or not token:
            message = "Google token answer has no access token"
            raise SheetUnavailable(message, cause=SheetFailure.RESPONSE)
        return token

    def _fetch(self, token: str) -> SheetData:
        query = urllib.parse.urlencode(
            [
                *(("ranges", tab) for tab in TABS),
                ("majorDimension", "ROWS"),
                ("valueRenderOption", "FORMATTED_VALUE"),
            ]
        )
        url = f"{SHEETS_URL}/{self._spreadsheet_id}/values:batchGet?{query}"
        response = self._send("GET", url, {"Authorization": f"Bearer {token}"}, None)
        _require_ok(response)
        return _map_batch(response.body)

    def _send(
        self,
        method: Literal["GET", "POST"],
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> HttpResponse:
        try:
            return self._http.request(method, url, headers, body, TIMEOUT_SECONDS)
        except TransportFailure as error:
            message = f"Google is unreachable ({error.kind})"
            raise SheetUnavailable(message, cause=SheetFailure.NETWORK) from None


def _sign(key: ServiceAccountKey, claims: Mapping[str, str | int]) -> str:
    # google-auth ships no type information for these two calls.
    signer = crypt.RSASigner.from_string(key.private_key.get_secret_value())  # type: ignore[no-untyped-call]
    encoded: bytes = jwt.encode(signer, dict(claims))  # type: ignore[no-untyped-call]
    return encoded.decode("ascii")


def _require_ok(response: HttpResponse) -> None:
    if response.status == HTTP_OK:
        return
    if response.status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
        message = f"Google refused access (HTTP {response.status})"
        raise SheetUnavailable(message, cause=SheetFailure.DENIED)
    if response.status == HTTP_NOT_FOUND:
        message = "Google did not find the spreadsheet (HTTP 404)"
        raise SheetUnavailable(message, cause=SheetFailure.NOT_FOUND)
    cause = SheetFailure.NETWORK if response.status >= 429 else SheetFailure.RESPONSE
    message = f"Google answered HTTP {response.status}"
    raise SheetUnavailable(message, cause=cause)


def _map_batch(body: bytes) -> SheetData:
    try:
        parsed = _BatchGet.model_validate_json(body)
    except ValidationError:
        message = "Google answer is not a values batch"
        raise SheetUnavailable(message, cause=SheetFailure.RESPONSE) from None
    if len(parsed.valueRanges) != len(TABS):
        message = "Google answer does not hold both tabs"
        raise SheetUnavailable(message, cause=SheetFailure.RESPONSE)
    grids: dict[str, tuple[tuple[str, ...], ...]] = {}
    for tab, value_range in zip(TABS, parsed.valueRanges, strict=True):
        if value_range.range.split("!")[0].strip("'") != tab:
            message = "Google answer lists the tabs in an unexpected order"
            raise SheetUnavailable(message, cause=SheetFailure.RESPONSE)
        grids[tab] = tuple(tuple(_cell_text(cell) for cell in row) for row in value_range.values)
    return SheetData(projects=grids[PROJECTS_TAB], globals=grids[GLOBALS_TAB])


def _cell_text(cell: str | int | float | bool) -> str:
    if isinstance(cell, bool):
        return "true" if cell else "false"
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return str(cell)


def make_sheet_reader(
    sheet: SheetSettings,
    key: SecretStr | None,
    *,
    injected: SheetReader | None,
    http: SheetsHttp | None,
    clock: Callable[[], datetime],
) -> SheetReader:
    """The reader of the configured sheet; `injected` (a test double) wins over the real one."""
    if injected is not None:
        return injected
    return GoogleSheetReader(
        key=key,
        spreadsheet_id=sheet.spreadsheet_id or "",
        http=http if http is not None else UrllibSheetsHttp(),
        clock=clock,
    )
