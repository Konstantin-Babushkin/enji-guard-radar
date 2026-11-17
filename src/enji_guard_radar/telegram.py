"""Telegram delivery over the Bot API. Shell module: all network I/O of the radar's alerts."""

import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import SecretStr

from enji_guard_radar.model import OutboundMessage
from enji_guard_radar.ports import Delivered, DeliveryError, NotDelivered, SendOutcome

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_MESSAGE_METHOD = "sendMessage"
MAX_DESCRIPTION_CHARS = 200

_NUMERIC_CHAT_ID = re.compile(r"-?\d+")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class TransportFailure(Exception):  # noqa: N818 - named for what the transport reports
    """The request got no HTTP answer. The message never contains the URL."""

    def __init__(self, kind: Literal["timeout", "network"], message: str) -> None:
        super().__init__(message)
        self.kind = kind


class HttpTransport(Protocol):
    def post_json(self, url: str, payload: Mapping[str, object], timeout_s: float) -> HttpResponse:
        """Raise TransportFailure when there is no HTTP answer."""
        ...


class UrllibTransport:
    """stdlib transport; default TLS verification, and no URL in any error it raises."""

    def post_json(self, url: str, payload: Mapping[str, object], timeout_s: float) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
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


@dataclass(frozen=True)
class TelegramResult:
    ok: bool
    status: int
    error_code: int | None = None
    description: str | None = None
    retry_after: int | None = None
    migrate_to_chat_id: int | None = None
    message_id: int | None = None


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def parse_telegram_response(response: HttpResponse) -> TelegramResult:
    """A body that is not a JSON object with a Boolean `ok` is never a success."""
    try:
        data = json.loads(response.body)
    except ValueError:
        return TelegramResult(ok=False, status=response.status)
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        return TelegramResult(ok=False, status=response.status)
    parameters = _as_mapping(data.get("parameters"))
    description = data.get("description")
    return TelegramResult(
        ok=data["ok"],
        status=response.status,
        error_code=_as_int(data.get("error_code")),
        description=description if isinstance(description, str) else None,
        retry_after=_as_int(parameters.get("retry_after")),
        migrate_to_chat_id=_as_int(parameters.get("migrate_to_chat_id")),
        message_id=_as_int(_as_mapping(data.get("result")).get("message_id")),
    )


class TelegramNotifier:
    def __init__(
        self,
        token: SecretStr,
        chat_id: str,
        transport: HttpTransport,
        *,
        chat_env_name: str = "TELEGRAM_CHAT_ID",
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_s: float = 10.0,
        max_retry_wait_s: float = 30.0,
        max_attempts: int = 3,
        min_interval_s: float = 1.0,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self._transport = transport
        self._chat_env_name = chat_env_name
        self._sleep = sleep
        self._monotonic = monotonic
        self._timeout_s = timeout_s
        self._max_retry_wait_s = max_retry_wait_s
        self._max_attempts = max_attempts
        self._min_interval_s = min_interval_s
        self._last_request_at: float | None = None

    def _chat_payload_id(self) -> int | str:
        return int(self.chat_id) if _NUMERIC_CHAT_ID.fullmatch(self.chat_id) else self.chat_id

    def _wait_for_interval(self) -> None:
        if self._last_request_at is None:
            return
        wait = self._last_request_at + self._min_interval_s - self._monotonic()
        if wait > 0:
            self._sleep(wait)

    def send(self, message: OutboundMessage) -> SendOutcome:
        # The URL carries the token: keep it in this local variable only.
        url = f"{TELEGRAM_API_BASE}/bot{self._token.get_secret_value()}/{SEND_MESSAGE_METHOD}"
        self._wait_for_interval()
        self._last_request_at = self._monotonic()
        payload: dict[str, object] = {
            "chat_id": self._chat_payload_id(),
            "text": message.text,
            "parse_mode": "HTML",
        }
        response = self._transport.post_json(url, payload, self._timeout_s)
        result = parse_telegram_response(response)
        if not result.ok:
            return NotDelivered(
                error=DeliveryError.TRANSIENT,
                detail=f"Telegram did not accept the message (HTTP {result.status})",
                stop_sending=True,
            )
        return Delivered(message_id=result.message_id)
