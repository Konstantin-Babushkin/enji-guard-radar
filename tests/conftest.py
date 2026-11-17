import socket
from typing import NoReturn

import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may open a network connection (SRC-06)."""

    def refuse(*_args: object, **_kwargs: object) -> NoReturn:
        message = "network access is not allowed in tests"
        raise RuntimeError(message)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
