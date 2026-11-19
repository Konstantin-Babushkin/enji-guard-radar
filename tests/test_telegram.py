import email.message
import io
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import NoReturn

import pytest
from pydantic import SecretStr
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, error_response, ok_response

from enji_guard_radar import messages
from enji_guard_radar.model import Alert, AlertKey, AlertKind, OutboundMessage, Severity
from enji_guard_radar.ports import Delivered, DeliveryError, NotDelivered
from enji_guard_radar.secret_guard import SecretGatedNotifier
from enji_guard_radar.telegram import (
    HttpResponse,
    TelegramNotifier,
    TransportFailure,
    UrllibTransport,
    call_method,
    chat_payload_id,
    classify_result,
    parse_telegram_response,
)
from enji_guard_radar.texts import ActionCode, render_action

CHAT_ID = "-1001234567890"


def stale_alert() -> Alert:
    return Alert(
        key=AlertKey(project_id="p-b", level="stale"),
        kind=AlertKind.STALE,
        project_name="Project B",
        severity=Severity.HIGH,
        age_days=12,
        last_run_at=datetime(2026, 1, 1, tzinfo=UTC),
        repo_count=2,
    )


def rendered_message() -> OutboundMessage:
    alert = stale_alert()
    return OutboundMessage(text=messages.render_alert(alert), keys=(alert.key,))


def make_notifier(
    fake: FakeTransport, fake_time: FakeTime, chat_id: str = CHAT_ID
) -> TelegramNotifier:
    return TelegramNotifier(
        SecretStr(FAKE_TOKEN),
        chat_id,
        fake,
        sleep=fake_time.sleep,
        monotonic=fake_time.monotonic,
    )


def test_delivers_rendered_alert_over_fake_transport(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeTransport([ok_response(42)])
    fake_time = FakeTime()
    gated = SecretGatedNotifier(make_notifier(fake, fake_time), [FAKE_TOKEN])

    with caplog.at_level(logging.DEBUG):
        outcome = gated.send(rendered_message())

    assert outcome == Delivered(message_id=42)
    assert len(fake.requests) == 1
    url, payload = fake.requests[0]
    assert url == "https://api.telegram.org/bot" + FAKE_TOKEN + "/sendMessage"
    assert set(payload) == {"chat_id", "text", "parse_mode"}
    assert payload["chat_id"] == -1001234567890
    assert isinstance(payload["chat_id"], int)
    assert payload["parse_mode"] == "HTML"
    assert isinstance(payload["text"], str)
    assert payload["text"]
    assert FAKE_TOKEN not in json.dumps(payload)
    assert FAKE_TOKEN not in caplog.text


def test_gate_blocks_message_containing_token() -> None:
    fake = FakeTransport([])
    gated = SecretGatedNotifier(make_notifier(fake, FakeTime()), [FAKE_TOKEN])
    message = OutboundMessage(text=f"oops {FAKE_TOKEN}", keys=(stale_alert().key,))

    outcome = gated.send(message)

    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.BLOCKED_SECRET
    assert fake.requests == []


def _refuse(error: BaseException) -> NoReturn:
    raise error


def test_urllib_transport_errors_carry_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    http_error = urllib.error.HTTPError(
        url, 401, "Unauthorized", email.message.Message(), io.BytesIO(b'{"ok": false}')
    )
    failures: list[BaseException] = [
        http_error,
        urllib.error.URLError(OSError(f"cannot reach {url}")),
        TimeoutError(url),
    ]
    transport = UrllibTransport()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _refuse(http_error))
    response = transport.post_json(url, {"chat_id": 1}, 1.0)
    assert response == HttpResponse(status=401, body=b'{"ok": false}')
    assert FAKE_TOKEN not in repr(response)

    kinds = []
    for failure in failures[1:]:
        monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, _f=failure, **_k: _refuse(_f))
        with pytest.raises(TransportFailure) as info:
            transport.post_json(url, {"chat_id": 1}, 1.0)
        kinds.append(info.value.kind)
        assert info.value.__cause__ is None
        assert info.value.__suppress_context__
        for text in (str(info.value), repr(info.value)):
            assert FAKE_TOKEN not in text
            assert "api.telegram.org" not in text
    assert kinds == ["network", "timeout"]


# --- classification ---------------------------------------------------------------------------

NEW_CHAT = -1009876543210


def raw(status: int, body: bytes) -> HttpResponse:
    return HttpResponse(status=status, body=body)


CLASSIFY_ROWS = [
    ("ok", ok_response(), None),
    (
        "migrate_with_400",
        error_response(400, 400, "Bad Request: group chat was upgraded", {"migrate_to_chat_id": 1}),
        DeliveryError.CHAT_MIGRATED,
    ),
    (
        "migrate_with_403",
        error_response(403, 403, "Forbidden", {"migrate_to_chat_id": 1}),
        DeliveryError.CHAT_MIGRATED,
    ),
    (
        "429",
        error_response(429, 429, "Too Many Requests", {"retry_after": 5}),
        DeliveryError.RATE_LIMITED,
    ),
    (
        "retry_after_alone",
        error_response(400, 400, "flood", {"retry_after": 3}),
        DeliveryError.RATE_LIMITED,
    ),
    ("401", error_response(401, 401, "Unauthorized"), DeliveryError.UNAUTHORIZED),
    ("404", error_response(404, 404, "Not Found"), DeliveryError.UNAUTHORIZED),
    ("403", error_response(403, 403, "Forbidden: bot was kicked"), DeliveryError.FORBIDDEN),
    ("400", error_response(400, 400, "Bad Request: chat not found"), DeliveryError.BAD_REQUEST),
    ("500", error_response(500, 500, "Internal"), DeliveryError.TRANSIENT),
    ("502", error_response(502, 502, "Bad Gateway"), DeliveryError.TRANSIENT),
    ("503", error_response(503, 503, "Unavailable"), DeliveryError.TRANSIENT),
    ("418", error_response(418, 418, "teapot"), DeliveryError.BAD_REQUEST),
    ("non_json_502", raw(502, b"<html>bad gateway</html>"), DeliveryError.TRANSIENT),
    ("non_json_400", raw(400, b"nope"), DeliveryError.BAD_REQUEST),
    ("empty_body_200", raw(200, b""), DeliveryError.BAD_REQUEST),
    ("json_without_ok", raw(200, b'{"result": {"message_id": 1}}'), DeliveryError.BAD_REQUEST),
    ("ok_not_bool", raw(200, b'{"ok": "true"}'), DeliveryError.BAD_REQUEST),
    ("no_error_code_uses_status", raw(503, b'{"ok": false}'), DeliveryError.TRANSIENT),
]


@pytest.mark.parametrize(
    ("response", "expected"),
    [pytest.param(r, e, id=name) for name, r, e in CLASSIFY_ROWS],
)
def test_classify_result_table(response: HttpResponse, expected: DeliveryError | None) -> None:
    assert classify_result(parse_telegram_response(response)) is expected


def test_parse_telegram_response_reads_fields() -> None:
    result = parse_telegram_response(
        error_response(429, 429, "slow down", {"retry_after": 7, "migrate_to_chat_id": NEW_CHAT})
    )
    assert (result.ok, result.error_code, result.description) == (False, 429, "slow down")
    assert (result.retry_after, result.migrate_to_chat_id) == (7, NEW_CHAT)
    assert parse_telegram_response(ok_response(9)).message_id == 9


# --- send behavior ----------------------------------------------------------------------------


def send_with(
    script: list[HttpResponse | TransportFailure],
) -> tuple[object, FakeTransport, FakeTime, TelegramNotifier]:
    fake = FakeTransport(script)
    fake_time = FakeTime()
    notifier = make_notifier(fake, fake_time)
    return notifier.send(rendered_message()), fake, fake_time, notifier


def rate_limited(seconds: int) -> HttpResponse:
    return error_response(429, 429, "Too Many Requests: retry later", {"retry_after": seconds})


def test_send_retry_after_30_waits_and_delivers() -> None:
    outcome, fake, fake_time, _ = send_with([rate_limited(30), ok_response()])
    assert outcome == Delivered(message_id=42)
    assert 30.0 in fake_time.sleeps
    assert len(fake.requests) == 2


def test_send_retry_after_31_is_left_for_next_run() -> None:
    outcome, fake, fake_time, _ = send_with([rate_limited(31)])
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.RATE_LIMITED
    assert outcome.stop_sending is True
    assert "31" in outcome.detail
    assert fake_time.sleeps == []
    assert len(fake.requests) == 1


def test_send_gives_up_after_three_rate_limited_attempts() -> None:
    outcome, fake, fake_time, _ = send_with([rate_limited(1)] * 3)
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.RATE_LIMITED
    assert outcome.stop_sending is True
    assert len(fake.requests) == 3
    assert fake_time.sleeps == [1.0, 1.0]


def test_send_429_without_retry_after_is_transient() -> None:
    outcome, fake, _, _ = send_with([error_response(429, 429, "Too Many Requests")])
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.TRANSIENT
    assert outcome.stop_sending is True
    assert len(fake.requests) == 1


def migrated(code: int = 400) -> HttpResponse:
    return error_response(
        code, code, "Bad Request: group chat was upgraded", {"migrate_to_chat_id": NEW_CHAT}
    )


def test_send_migrated_chat_resends_once_to_new_id() -> None:
    outcome, fake, _, notifier = send_with([migrated(), ok_response(7)])
    assert isinstance(outcome, Delivered)
    assert outcome.message_id == 7
    assert outcome.operator_action is not None
    assert str(NEW_CHAT) in outcome.operator_action
    assert "TELEGRAM_CHAT_ID" in outcome.operator_action
    assert [payload["chat_id"] for _, payload in fake.requests] == [-1001234567890, NEW_CHAT]
    assert notifier.chat_id == str(NEW_CHAT)


def test_send_migrated_twice_stops_after_two_requests() -> None:
    outcome, fake, _, _ = send_with([migrated(), migrated(403)])
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.CHAT_MIGRATED
    assert outcome.stop_sending is True
    assert outcome.operator_action is not None
    assert "TELEGRAM_CHAT_ID" in outcome.operator_action
    assert len(fake.requests) == 2


def test_send_migrated_on_last_attempt_keeps_new_id_for_next_run() -> None:
    outcome, fake, _, notifier = send_with([rate_limited(1), rate_limited(1), migrated()])
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.CHAT_MIGRATED
    assert len(fake.requests) == 3
    assert notifier.chat_id == str(NEW_CHAT)


def test_send_unauthorized_names_the_variable_not_the_token() -> None:
    outcome, _, _, _ = send_with([error_response(401, 401, f"Unauthorized {FAKE_TOKEN}")])
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.UNAUTHORIZED
    assert outcome.stop_sending is True
    assert outcome.operator_action is not None
    assert "TELEGRAM_BOT_TOKEN" in outcome.operator_action
    assert FAKE_TOKEN not in outcome.operator_action
    assert FAKE_TOKEN not in outcome.detail


def test_send_forbidden_tells_the_operator_to_add_the_bot_back() -> None:
    outcome, _, _, _ = send_with([error_response(403, 403, "Forbidden: bot was kicked")])
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.FORBIDDEN
    assert outcome.stop_sending is True
    assert outcome.operator_action is not None
    assert "-1001234567890" in outcome.operator_action
    assert "TELEGRAM_CHAT_ID" in outcome.operator_action


def test_send_bad_request_does_not_stop_the_run() -> None:
    outcome, fake, _, _ = send_with([error_response(400, 400, "Bad Request: chat not found")])
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.BAD_REQUEST
    assert outcome.stop_sending is False
    assert "chat not found" in outcome.detail
    assert len(fake.requests) == 1


def test_send_server_error_is_transient_and_stops_the_run() -> None:
    outcome, fake, _, _ = send_with([error_response(502, 502, "Bad Gateway")])
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.TRANSIENT
    assert outcome.stop_sending is True
    assert len(fake.requests) == 1


def test_send_timeout_may_have_delivered_so_a_duplicate_is_possible() -> None:
    outcome, fake, fake_time, _ = send_with([TransportFailure("timeout", "request timed out")])
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.TRANSIENT
    assert outcome.stop_sending is True
    assert "a duplicate is possible on the next run" in outcome.detail
    assert len(fake.requests) == 1
    assert fake_time.sleeps == []


def test_send_network_failure_is_transient() -> None:
    outcome, _, _, _ = send_with([TransportFailure("network", "network error: URLError")])
    assert isinstance(outcome, NotDelivered)
    assert outcome.error is DeliveryError.TRANSIENT
    assert outcome.stop_sending is True
    assert "duplicate" not in outcome.detail


def test_send_spaces_consecutive_requests_by_min_interval() -> None:
    fake = FakeTransport([ok_response(1), ok_response(2)])
    fake_time = FakeTime()
    notifier = make_notifier(fake, fake_time)
    notifier.send(rendered_message())
    fake_time.now += 0.25
    notifier.send(rendered_message())
    assert fake_time.sleeps == [0.75]
    assert fake_time.now == 1.0


def test_send_description_is_redacted_then_truncated() -> None:
    description = "x" * 150 + FAKE_TOKEN + "y" * 300
    outcome, _, _, _ = send_with([error_response(400, 400, description)])
    assert isinstance(outcome, NotDelivered)
    assert FAKE_TOKEN not in outcome.detail
    assert "[REDACTED]" in outcome.detail
    assert outcome.operator_action is not None
    assert FAKE_TOKEN not in outcome.operator_action
    prefix = "Telegram rejected the message: "
    assert len(outcome.detail) == len(prefix) + 200


def test_send_logs_each_failure_once_without_the_token(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        send_with([error_response(403, 403, f"Forbidden {FAKE_TOKEN}")])
    records = [r for r in caplog.records if r.name.endswith("telegram")]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert FAKE_TOKEN not in caplog.text


def get_me_response() -> HttpResponse:
    body = {"ok": True, "result": {"id": 777, "is_bot": True, "username": "radar_bot"}}
    return HttpResponse(status=200, body=json.dumps(body).encode())


def test_call_method_returns_the_result_object_and_builds_the_url() -> None:
    fake = FakeTransport([get_me_response()])
    result = call_method(SecretStr(FAKE_TOKEN), "getMe", {}, fake)
    assert result.ok
    assert result.result == {"id": 777, "is_bot": True, "username": "radar_bot"}
    ((url, payload),) = fake.requests
    assert url.endswith(f"/bot{FAKE_TOKEN}/getMe")
    assert payload == {}


@pytest.mark.parametrize("raw", [[1, 2], True, "text", None])
def test_call_method_result_is_none_unless_it_is_an_object(raw: object) -> None:
    body = json.dumps({"ok": True, "result": raw}).encode()
    fake = FakeTransport([HttpResponse(status=200, body=body)])
    assert call_method(SecretStr(FAKE_TOKEN), "getChat", {"chat_id": 1}, fake).result is None


def test_call_method_redacts_the_token_from_the_description() -> None:
    description = f"Unauthorized {FAKE_TOKEN} for you"
    fake = FakeTransport([error_response(401, 401, description)])
    result = call_method(SecretStr(FAKE_TOKEN), "getMe", {}, fake)
    assert not result.ok
    assert result.error_code == 401
    assert FAKE_TOKEN not in repr(result)
    assert result.description is not None
    assert "[REDACTED]" in result.description


def test_call_method_lets_a_transport_failure_through_without_the_url() -> None:
    fake = FakeTransport([TransportFailure("timeout", "request timed out")])
    with pytest.raises(TransportFailure) as raised:
        call_method(SecretStr(FAKE_TOKEN), "getMe", {}, fake)
    assert FAKE_TOKEN not in str(raised.value)


@pytest.mark.parametrize(
    ("chat_id", "expected"),
    [("-1001234567890", -1001234567890), ("42", 42), ("@radar_group", "@radar_group")],
)
def test_chat_payload_id(chat_id: str, expected: int | str) -> None:
    assert chat_payload_id(chat_id) == expected


# --- operator actions travel as typed notices, so the ops chat can translate them ---


def assert_notice_matches(outcome: object, code: ActionCode, *, bits: tuple[str, ...]) -> None:
    assert isinstance(outcome, Delivered | NotDelivered)
    notice = outcome.action_notice
    assert notice is not None
    assert notice.code is code
    assert outcome.operator_action == render_action(notice, "en")
    russian = render_action(notice, "ru")
    assert re.search("[А-Яа-яЁё]", russian)
    for bit in bits:  # ids, codes and env names are inserted verbatim in both languages
        assert bit in outcome.operator_action
        assert bit in russian
    assert FAKE_TOKEN not in russian


def test_notice_for_a_rejected_token() -> None:
    outcome, *_ = send_with([error_response(401, 401, f"Unauthorized {FAKE_TOKEN}")])
    assert_notice_matches(outcome, ActionCode.TOKEN_REJECTED, bits=("401", "TELEGRAM_BOT_TOKEN"))
    assert isinstance(outcome, NotDelivered)
    assert outcome.operator_action == (
        "Telegram rejected the bot token (HTTP 401); check TELEGRAM_BOT_TOKEN"
    )


def test_notice_for_a_forbidden_chat() -> None:
    outcome, *_ = send_with([error_response(403, 403, "Forbidden: bot was kicked")])
    assert_notice_matches(
        outcome, ActionCode.CHAT_FORBIDDEN, bits=("-1001234567890", "403", "TELEGRAM_CHAT_ID")
    )
    assert isinstance(outcome, NotDelivered)
    assert outcome.operator_action == (
        "Telegram refused to post to chat -1001234567890 (HTTP 403): the bot was removed, "
        "blocked or cannot post there; add it back or fix TELEGRAM_CHAT_ID"
    )


def test_notice_for_a_bad_request_keeps_the_telegram_description() -> None:
    outcome, *_ = send_with([error_response(400, 400, "Bad Request: can't parse entities")])
    assert_notice_matches(
        outcome, ActionCode.BAD_REQUEST, bits=("Bad Request: can't parse entities",)
    )
    assert isinstance(outcome, NotDelivered)
    assert outcome.detail == outcome.operator_action
    assert outcome.operator_action.startswith("Telegram rejected the message: ")


def test_notice_for_a_migrated_chat_on_success_and_on_failure() -> None:
    delivered, *_ = send_with([migrated(), ok_response(7)])
    assert_notice_matches(
        delivered,
        ActionCode.CHAT_MIGRATED,
        bits=(str(NEW_CHAT), "-1001234567890", "TELEGRAM_CHAT_ID"),
    )
    assert isinstance(delivered, Delivered)
    assert delivered.operator_action == (
        f"Telegram chat -1001234567890 was upgraded to {NEW_CHAT}; set TELEGRAM_CHAT_ID={NEW_CHAT}"
    )
    failed, *_ = send_with([migrated(), migrated(403)])
    assert_notice_matches(failed, ActionCode.CHAT_MIGRATED, bits=(str(NEW_CHAT),))


def test_a_transient_failure_has_no_notice() -> None:
    outcome, *_ = send_with([error_response(500, 500, "Internal Server Error")])
    assert isinstance(outcome, NotDelivered)
    assert outcome.operator_action is None
    assert outcome.action_notice is None


def test_the_secret_gate_attaches_a_notice() -> None:
    class Inner:
        def send(self, message: OutboundMessage) -> Delivered:
            return Delivered()

    gate = SecretGatedNotifier(Inner(), [FAKE_TOKEN])
    outcome = gate.send(OutboundMessage(text=f"leak {FAKE_TOKEN}"))
    assert_notice_matches(outcome, ActionCode.SECRET_WITHHELD, bits=())
    assert isinstance(outcome, NotDelivered)
    assert outcome.operator_action == (
        "A message was withheld because it looked like it contained a secret; "
        "check the radar log and the Guard data it came from"
    )
