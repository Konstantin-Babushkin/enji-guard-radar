import email.message
import io
import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import NoReturn

import pytest
from pydantic import SecretStr
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, ok_response

from enji_guard_radar import messages
from enji_guard_radar.model import Alert, AlertKey, AlertKind, OutboundMessage, Severity
from enji_guard_radar.ports import Delivered, DeliveryError, NotDelivered
from enji_guard_radar.secret_guard import SecretGatedNotifier
from enji_guard_radar.telegram import (
    HttpResponse,
    TelegramNotifier,
    TransportFailure,
    UrllibTransport,
)

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
