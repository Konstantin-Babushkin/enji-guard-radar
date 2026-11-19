"""Boundary contracts: Guard tools, notifier, clock. Implemented by shell modules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from enji_guard_radar.model import OutboundMessage
from enji_guard_radar.texts import ActionNotice

if TYPE_CHECKING:
    # Type-only: loading ports must not load triage (secret_guard -> ports, titles -> secret_guard).
    from enji_guard_radar.triage import TriageOutcome, TriageRequest


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


class GuardSession(Protocol):
    """A Guard connection held open for one run: enter to connect, exit to close."""

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        """Raise GuardCallError on any failure."""
        ...

    def tool_names(self) -> frozenset[str]:
        """Names of the tools the server listed on connect."""
        ...

    def tool_arguments(self) -> Mapping[str, tuple[str, ...]]:
        """Sorted input-schema property names per listed tool."""
        ...

    def result_shapes(self) -> Mapping[str, int]:
        """How many results arrived in each shape so far (structured, text_json, wrapped)."""
        ...

    def __enter__(self) -> GuardSession: ...

    def __exit__(self, *exc: object) -> None: ...


# State directory in, unentered session out.
GuardConnector = Callable[[Path], GuardSession]


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
    # The same action as data, so the ops chat can show it in its language.
    action_notice: ActionNotice | None = None


class Printed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NotDelivered(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    error: DeliveryError
    detail: str
    stop_sending: bool
    operator_action: str | None = None
    action_notice: ActionNotice | None = None


SendOutcome = Delivered | Printed | NotDelivered
Clock = Callable[[], datetime]


class Notifier(Protocol):
    def send(self, message: OutboundMessage) -> SendOutcome: ...


class Triager(Protocol):
    def triage(self, request: TriageRequest) -> TriageOutcome:
        """Never raises: every failure comes back as a TriageFailed with a reason.

        No retry inside a run; the next run tries again.
        """
        ...
