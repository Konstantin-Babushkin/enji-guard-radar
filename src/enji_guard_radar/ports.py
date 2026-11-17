"""Boundary contracts: Guard tools, notifier, clock. Implemented by shell modules."""

from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from enji_guard_radar.model import OutboundMessage


class GuardFailure(StrEnum):
    """Why a Guard call failed: login problem, Guard or network trouble, or a bad answer."""

    AUTH = "auth"
    UNAVAILABLE = "unavailable"
    DATA = "data"


class GuardCallError(Exception):
    """Message is sanitized text written by the adapter: never a URL, header or credential."""

    def __init__(self, message: str, *, cause: GuardFailure = GuardFailure.UNAVAILABLE) -> None:
        super().__init__(message)
        self.cause = cause


class AuthFailureKind(StrEnum):
    NOT_AUTHENTICATED = "not_authenticated"
    AUTH_EXPIRED = "auth_expired"
    AUTH_CLIENT_GONE = "auth_client_gone"
    AUTH_REJECTED = "auth_rejected"
    AUTH_FORBIDDEN = "auth_forbidden"


class GuardAuthError(GuardCallError):
    """The Guard login cannot be used; the operator must run `enji-guard-radar auth`."""

    def __init__(self, kind: AuthFailureKind, message: str) -> None:
        super().__init__(message, cause=GuardFailure.AUTH)
        self.kind = kind


class AccessTokenSource(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str:
        """Return a bearer token. Raise GuardAuthError or GuardCallError, never leak a secret."""
        ...


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
