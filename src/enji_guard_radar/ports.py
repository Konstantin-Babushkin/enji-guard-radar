"""Boundary contracts: Guard tools, notifier, clock. Implemented by shell modules."""

from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from enji_guard_radar.model import OutboundMessage


class GuardCallError(Exception):
    """Message is sanitized text written by the adapter: never a URL, header or credential."""


class GuardTools(Protocol):
    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        """Raise GuardCallError on any failure."""
        ...


class DeliveryError(StrEnum):
    RATE_LIMITED = "rate_limited"
    CHAT_MIGRATED = "chat_migrated"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    BAD_REQUEST = "bad_request"
    TRANSIENT = "transient"
    BLOCKED_SECRET = "blocked_secret"


class Delivered(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    message_id: int | None = None
    operator_action: str | None = None


class Printed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NotDelivered(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    error: DeliveryError
    detail: str
    stop_sending: bool
    operator_action: str | None = None


SendOutcome = Delivered | Printed | NotDelivered
Clock = Callable[[], datetime]


class Notifier(Protocol):
    def send(self, message: OutboundMessage) -> SendOutcome: ...
