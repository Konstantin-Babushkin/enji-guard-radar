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
from enji_guard_radar.secret_guard import redact

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_MESSAGE_METHOD = "sendMessage"
MAX_DESCRIPTION_CHARS = 200

_NUMERIC_CHAT_ID = re.compile(r"-?\d+")

ENV_BOT_TOKEN_NAME = "TELEGRAM_BOT_TOKEN"

HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR_MIN = 500
HTTP_SERVER_ERROR_MAX = 599


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


def classify_result(result: TelegramResult) -> DeliveryError | None:
    """Map a response to its error class; the order is fixed. None means delivered."""
    if result.ok:
        return None
    if result.migrate_to_chat_id is not None:
        return DeliveryError.CHAT_MIGRATED
    # error_code may change (Bot API page), so fall back to the HTTP status when it is missing.
    code = result.error_code if result.error_code is not None else result.status
    if code == HTTP_TOO_MANY_REQUESTS or result.retry_after is not None:
        return DeliveryError.RATE_LIMITED
    # ASSUMPTION: Telegram answers 404 for a malformed token path
    if code in (HTTP_UNAUTHORIZED, HTTP_NOT_FOUND):
        return DeliveryError.UNAUTHORIZED
    if code == HTTP_FORBIDDEN:
        return DeliveryError.FORBIDDEN
    if code == HTTP_BAD_REQUEST:
        return DeliveryError.BAD_REQUEST
    if HTTP_SERVER_ERROR_MIN <= code <= HTTP_SERVER_ERROR_MAX:
        return DeliveryError.TRANSIENT
    return DeliveryError.BAD_REQUEST


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

    def _sanitize(self, text: str | None) -> str:
        """Redact first, then cut, so a token split by the cut cannot leak a fragment."""
        clean = redact(text or "", [self._token.get_secret_value()])
        return clean[:MAX_DESCRIPTION_CHARS]

    def _fail(
        self,
        error: DeliveryError,
        detail: str,
        *,
        stop_sending: bool,
        operator_action: str | None = None,
    ) -> NotDelivered:
        if error in (DeliveryError.RATE_LIMITED, DeliveryError.TRANSIENT):
            logger.warning("telegram delivery failed (%s): %s", error.value, detail)
        else:
            logger.error("telegram delivery failed (%s): %s", error.value, detail)
        return NotDelivered(
            error=error, detail=detail, stop_sending=stop_sending, operator_action=operator_action
        )

    def _failure_outcome(self, error: DeliveryError, result: TelegramResult) -> NotDelivered:
        """Every class except a rate limit and a migration, which send() handles."""
        description = self._sanitize(result.description)
        code = result.error_code if result.error_code is not None else result.status
        if error is DeliveryError.UNAUTHORIZED:
            return self._fail(
                error,
                f"Telegram rejected the bot token (HTTP {code}): {description}",
                stop_sending=True,
                operator_action=(
                    f"Telegram rejected the bot token (HTTP {code}); check {ENV_BOT_TOKEN_NAME}"
                ),
            )
        if error is DeliveryError.FORBIDDEN:
            return self._fail(
                error,
                f"Telegram refused to post: {description}",
                stop_sending=True,
                operator_action=(
                    f"Telegram refused to post to chat {self.chat_id} (HTTP 403): the bot was "
                    f"removed, blocked or cannot post there; add it back or fix "
                    f"{self._chat_env_name}"
                ),
            )
        if error is DeliveryError.BAD_REQUEST:
            detail = f"Telegram rejected the message: {description}"
            return self._fail(error, detail, stop_sending=False, operator_action=detail)
        return self._fail(
            DeliveryError.TRANSIENT,
            f"Telegram answered HTTP {result.status}; left for the next run: {description}",
            stop_sending=True,
        )

    def send(self, message: OutboundMessage) -> SendOutcome:
        # The URL carries the token: keep it in this local variable only.
        url = f"{TELEGRAM_API_BASE}/bot{self._token.get_secret_value()}/{SEND_MESSAGE_METHOD}"
        original_chat = self.chat_id
        migration_action: str | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._wait_for_interval()
            self._last_request_at = self._monotonic()
            payload: dict[str, object] = {
                "chat_id": self._chat_payload_id(),
                "text": message.text,
                "parse_mode": "HTML",
            }
            try:
                response = self._transport.post_json(url, payload, self._timeout_s)
            except TransportFailure as failure:
                if failure.kind == "timeout":
                    detail = (
                        "request timed out; it may have been delivered, "
                        "so a duplicate is possible on the next run"
                    )
                else:
                    detail = "network error; left for the next run"
                return self._fail(DeliveryError.TRANSIENT, detail, stop_sending=True)

            result = parse_telegram_response(response)
            error = classify_result(result)
            if error is None:
                return Delivered(message_id=result.message_id, operator_action=migration_action)

            if error is DeliveryError.CHAT_MIGRATED:
                if migration_action is None:
                    new_chat = str(result.migrate_to_chat_id)
                    migration_action = (
                        f"Telegram chat {original_chat} was upgraded to {new_chat}; "
                        f"set {self._chat_env_name}={new_chat}"
                    )
                    logger.error(
                        "Telegram chat %s was upgraded to %s; update %s",
                        original_chat,
                        new_chat,
                        self._chat_env_name,
                    )
                    self.chat_id = new_chat
                    if attempt < self._max_attempts:
                        continue
                return self._fail(
                    error,
                    "Telegram chat was upgraded; the message was left for the next run",
                    stop_sending=True,
                    operator_action=migration_action,
                )

            if error is DeliveryError.RATE_LIMITED:
                if result.retry_after is None:
                    return self._failure_outcome(DeliveryError.TRANSIENT, result)
                if result.retry_after <= self._max_retry_wait_s and attempt < self._max_attempts:
                    self._sleep(float(result.retry_after))
                    continue
                return self._fail(
                    error,
                    f"Telegram asked to wait {result.retry_after} s; left for the next run",
                    stop_sending=True,
                )

            return self._failure_outcome(error, result)

        # Unreachable while max_attempts >= 1; kept so a bad value fails closed.
        return self._fail(DeliveryError.TRANSIENT, "no attempt was made", stop_sending=True)
