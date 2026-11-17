"""Guard login shell: credential file with lock, form POST, refresh with rotation.

The credential file `state/guard-auth.json` is the one place a Guard secret lives on disk
(AGENTS.md exception: the refresh token changes on every refresh). It is written only through
`atomic_write`, never backed up, never deleted, and never replaced by an older copy.
"""

import contextlib
import fcntl
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from enji_guard_radar.guard_auth import (
    AUTH_FAILURE_TEXT,
    REFRESH_MARGIN,
    AuthRejected,
    CredentialFormatError,
    GuardCredentials,
    Refreshed,
    access_valid,
    classify_token_response,
    deserialize_credentials,
    merge_refreshed,
    refresh_form,
    serialize_credentials,
)
from enji_guard_radar.ports import AuthFailureKind, Clock, GuardAuthError, GuardCallError
from enji_guard_radar.state_store import DIR_MODE, FILE_MODE, atomic_write
from enji_guard_radar.telegram import HttpResponse, TransportFailure

logger = logging.getLogger(__name__)

CREDENTIALS_FILE = "guard-auth.json"
CREDENTIALS_LOCK = "guard-auth.lock"
LOCK_POLL_S = 0.1


class CredentialStore:
    def __init__(
        self,
        directory: Path,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        lock_wait_s: float = 10.0,
    ) -> None:
        self._directory = directory
        self._sleep = sleep
        self._monotonic = monotonic
        self._lock_wait_s = lock_wait_s

    @property
    def path(self) -> Path:
        return self._directory / CREDENTIALS_FILE

    def load(self) -> GuardCredentials:
        """Read-only: an unreadable or invalid file is reported and left exactly as it is."""
        base = AUTH_FAILURE_TEXT[AuthFailureKind.NOT_AUTHENTICATED]
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            raise GuardAuthError(AuthFailureKind.NOT_AUTHENTICATED, base) from None
        except OSError as exc:
            logger.error("Guard login file unreadable (%s)", type(exc).__name__)  # noqa: TRY400 - no traceback
            message = f"{base} (file unreadable)"
            raise GuardAuthError(AuthFailureKind.NOT_AUTHENTICATED, message) from None
        try:
            return deserialize_credentials(data)
        except CredentialFormatError as exc:
            logger.error("Guard login file invalid (%s)", exc.reason)  # noqa: TRY400 - no traceback
            message = f"{base} (file invalid: {exc.reason})"
            raise GuardAuthError(AuthFailureKind.NOT_AUTHENTICATED, message) from None

    def save(self, credentials: GuardCredentials) -> None:
        self._directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        atomic_write(self._directory, self.path, serialize_credentials(credentials), FILE_MODE)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive across processes; waits up to `lock_wait_s`, then gives up as unavailable."""
        self._directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        fd = os.open(self._directory / CREDENTIALS_LOCK, os.O_RDWR | os.O_CREAT, FILE_MODE)
        acquired = False
        try:
            started = self._monotonic()
            while not acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    waited = self._monotonic() - started
                    if waited >= self._lock_wait_s:
                        logger.warning(
                            "Guard login lock still held after %.1f s", self._lock_wait_s
                        )
                        message = "Guard login is busy in another radar process"
                        raise GuardCallError(message) from None
                    self._sleep(LOCK_POLL_S)
            yield
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


class FormTransport(Protocol):
    def post_form(self, url: str, form: Mapping[str, str], timeout_s: float) -> HttpResponse:
        """Raise TransportFailure when there is no HTTP answer."""
        ...


class UrllibOAuthTransport:
    """stdlib form POST; default TLS verification, and no URL in any error it raises."""

    def post_form(self, url: str, form: Mapping[str, str], timeout_s: float) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(form).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
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


class RefreshingTokenSource:
    """Hands out a valid access token, refreshing under the credential lock when needed."""

    def __init__(
        self,
        store: CredentialStore,
        transport: FormTransport,
        clock: Clock,
        *,
        margin: timedelta = REFRESH_MARGIN,
        timeout_s: float = 30.0,
    ) -> None:
        self._store = store
        self._transport = transport
        self._clock = clock
        self._margin = margin
        self._timeout_s = timeout_s
        self._current: GuardCredentials | None = None

    def access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._current is not None and self._usable(self._current):
            return self._current.access_token.get_secret_value()
        with self._store.lock():
            # Another process may have refreshed while we waited: look at the file again.
            stored = self._store.load()
            if not force_refresh and self._usable(stored):
                self._current = stored
                return stored.access_token.get_secret_value()
            refreshed = self._refresh(stored)
        self._current = refreshed
        return refreshed.access_token.get_secret_value()

    def _usable(self, credentials: GuardCredentials) -> bool:
        return access_valid(credentials, self._clock(), self._margin)

    def _refresh(self, stored: GuardCredentials) -> GuardCredentials:
        try:
            response = self._transport.post_form(
                stored.token_endpoint, refresh_form(stored), self._timeout_s
            )
        except TransportFailure as exc:
            message = f"Guard login service unreachable ({exc.kind})"
            raise GuardCallError(message) from None
        now = self._clock()
        outcome = classify_token_response(response.status, response.body, now)
        if isinstance(outcome, AuthRejected):
            raise GuardAuthError(outcome.kind, AUTH_FAILURE_TEXT[outcome.kind])
        if not isinstance(outcome, Refreshed):
            message = f"Guard login service unavailable (HTTP {outcome.status})"
            raise GuardCallError(message)
        merged = merge_refreshed(stored, outcome, now)
        self._store.save(merged)
        logger.info(
            "Guard access token refreshed (valid until %s, refresh token rotated: %s)",
            merged.expires_at.isoformat(),
            "yes" if outcome.refresh_token is not None else "no",
        )
        return merged
