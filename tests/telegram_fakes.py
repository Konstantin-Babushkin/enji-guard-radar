"""Fakes shared by the Telegram tests (and by plan 01-07): no network, no real time."""

import json
from collections.abc import Mapping

from enji_guard_radar.telegram import HttpResponse, TransportFailure

FAKE_TOKEN = "123456789:test-token-for-the-radar-unit-tests"


class FakeTransport:
    def __init__(self, script: list[HttpResponse | TransportFailure]) -> None:
        self._script = list(script)
        self.requests: list[tuple[str, dict[str, object]]] = []

    def post_json(self, url: str, payload: Mapping[str, object], timeout_s: float) -> HttpResponse:
        del timeout_s
        self.requests.append((url, dict(payload)))
        if not self._script:
            message = "FakeTransport script ran out: unexpected extra request"
            raise AssertionError(message)
        item = self._script.pop(0)
        if isinstance(item, TransportFailure):
            raise item
        return item


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def ok_response(message_id: int = 42) -> HttpResponse:
    body = {"ok": True, "result": {"message_id": message_id}}
    return HttpResponse(status=200, body=json.dumps(body).encode())


def error_response(
    status: int,
    error_code: int,
    description: str,
    parameters: dict[str, object] | None = None,
) -> HttpResponse:
    body: dict[str, object] = {"ok": False, "error_code": error_code, "description": description}
    if parameters is not None:
        body["parameters"] = parameters
    return HttpResponse(status=status, body=json.dumps(body).encode())
