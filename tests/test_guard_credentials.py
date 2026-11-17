"""Token layer shell: refresh, rotation, persistence, lock. Real files in tmp_path, fake network."""

import contextlib
import email.message
import io
import logging
import stat
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn

import pytest
from fake_oauth import (
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    ROTATED_ACCESS_TOKEN,
    ROTATED_REFRESH_TOKEN,
    TOKEN_ENDPOINT,
    FakeOAuthTransport,
    make_credentials,
    oauth_error,
    token_body,
)
from telegram_fakes import FakeTime

from enji_guard_radar.guard_auth import GUARD_MCP_URL, deserialize_credentials
from enji_guard_radar.guard_credentials import (
    CredentialStore,
    RefreshingTokenSource,
    UrllibOAuthTransport,
)
from enji_guard_radar.ports import (
    AuthFailureKind,
    GuardAuthError,
    GuardCallError,
    GuardFailure,
)
from enji_guard_radar.telegram import HttpResponse, TransportFailure

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
ALL_FAKE_TOKENS = (
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    ROTATED_ACCESS_TOKEN,
    ROTATED_REFRESH_TOKEN,
)
AUTH_HINT = "enji-guard-radar auth"


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def fixed_clock() -> datetime:
    return NOW


def make_store(
    tmp_path: Path,
    fake_time: FakeTime | None = None,
    wait: float = 10.0,
    sleep: Callable[[float], None] | None = None,
) -> CredentialStore:
    fake_time = fake_time or FakeTime()
    return CredentialStore(
        tmp_path / "state",
        sleep=sleep or fake_time.sleep,
        monotonic=fake_time.monotonic,
        lock_wait_s=wait,
    )


def store_with_expired_login(
    tmp_path: Path, fake_time: FakeTime | None = None, wait: float = 10.0
) -> CredentialStore:
    store = make_store(tmp_path, fake_time, wait)
    expired = NOW - timedelta(minutes=1)
    store.save(make_credentials(expires_at=expired, obtained_at=NOW - timedelta(hours=1)))
    return store


def rotated() -> HttpResponse:
    return token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)


def test_expired_token_is_refreshed_and_rotation_saved_before_use(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "state")
    expired = NOW - timedelta(minutes=1)
    store.save(make_credentials(expires_at=expired, obtained_at=NOW - timedelta(hours=1)))
    transport = FakeOAuthTransport([token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)])
    source = RefreshingTokenSource(store, transport, fixed_clock)

    assert source.access_token() == ROTATED_ACCESS_TOKEN

    assert len(transport.requests) == 1
    url, form = transport.requests[0]
    assert url == TOKEN_ENDPOINT
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == FAKE_REFRESH_TOKEN
    assert form["resource"] == GUARD_MCP_URL
    saved = deserialize_credentials(store.path.read_bytes())
    assert saved.access_token.get_secret_value() == ROTATED_ACCESS_TOKEN
    assert saved.refresh_token.get_secret_value() == ROTATED_REFRESH_TOKEN
    assert saved.expires_at == NOW + timedelta(seconds=3600)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    assert source.access_token() == ROTATED_ACCESS_TOKEN
    assert len(transport.requests) == 1


def test_directory_is_private(tmp_path: Path) -> None:
    store = store_with_expired_login(tmp_path)
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_refresh_answer_without_refresh_token_keeps_the_old_one(tmp_path: Path) -> None:
    store = store_with_expired_login(tmp_path)
    transport = FakeOAuthTransport([token_body(ROTATED_ACCESS_TOKEN)])

    assert RefreshingTokenSource(store, transport, fixed_clock).access_token() == (
        ROTATED_ACCESS_TOKEN
    )

    saved = deserialize_credentials(store.path.read_bytes())
    assert saved.refresh_token.get_secret_value() == FAKE_REFRESH_TOKEN
    assert saved.access_token.get_secret_value() == ROTATED_ACCESS_TOKEN


def test_valid_token_is_used_without_any_request(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save(make_credentials(expires_at=NOW + timedelta(hours=1), obtained_at=NOW))
    transport = FakeOAuthTransport([])

    assert RefreshingTokenSource(store, transport, fixed_clock).access_token() == FAKE_ACCESS_TOKEN
    assert transport.requests == []


@pytest.mark.parametrize(("seconds_left", "refreshes"), [(120, True), (121, False)])
def test_expiry_boundary_refresh_margin(tmp_path: Path, seconds_left: int, refreshes: bool) -> None:
    store = make_store(tmp_path)
    store.save(make_credentials(expires_at=NOW + timedelta(seconds=seconds_left), obtained_at=NOW))
    transport = FakeOAuthTransport([rotated()] if refreshes else [])

    token = RefreshingTokenSource(store, transport, fixed_clock).access_token()

    assert token == (ROTATED_ACCESS_TOKEN if refreshes else FAKE_ACCESS_TOKEN)
    assert len(transport.requests) == (1 if refreshes else 0)


def test_refresh_without_resource_when_the_login_says_so(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save(
        make_credentials(
            expires_at=NOW - timedelta(minutes=1), obtained_at=NOW, send_resource=False
        )
    )
    transport = FakeOAuthTransport([rotated()])

    RefreshingTokenSource(store, transport, fixed_clock).access_token()

    assert "resource" not in transport.requests[0][1]


# --- lock and race ----------------------------------------------------------------------------


def test_race_second_source_waits_then_uses_the_token_the_first_saved(tmp_path: Path) -> None:
    fake_time = FakeTime()
    store_a = store_with_expired_login(tmp_path, fake_time)
    holder = contextlib.ExitStack()
    holder.enter_context(store_a.lock())

    def while_b_waits(seconds: float) -> None:
        """A finishes its refresh during B's first wait: saves the rotated login, lets go."""
        fake_time.sleep(seconds)
        store_a.save(
            make_credentials(
                expires_at=NOW + timedelta(hours=1),
                obtained_at=NOW,
                access=ROTATED_ACCESS_TOKEN,
                refresh=ROTATED_REFRESH_TOKEN,
            )
        )
        holder.close()

    store_b = make_store(tmp_path, fake_time, sleep=while_b_waits)
    transport_b = FakeOAuthTransport([])  # B must never post: any request fails the test

    with holder:
        token = RefreshingTokenSource(store_b, transport_b, fixed_clock).access_token()

    assert token == ROTATED_ACCESS_TOKEN
    assert transport_b.requests == []
    assert len(fake_time.sleeps) == 1


def test_busy_lock_times_out_as_unavailable_not_as_auth(tmp_path: Path) -> None:
    fake_time = FakeTime()
    store = store_with_expired_login(tmp_path, fake_time=fake_time, wait=0.3)
    other = make_store(tmp_path, fake_time)
    transport = FakeOAuthTransport([])

    with other.lock(), pytest.raises(GuardCallError) as caught:
        RefreshingTokenSource(store, transport, fixed_clock).access_token()

    assert not isinstance(caught.value, GuardAuthError)
    assert caught.value.cause is GuardFailure.UNAVAILABLE
    assert str(caught.value) == "Guard login is busy in another radar process"
    assert transport.requests == []
    assert sum(fake_time.sleeps) >= 0.3


def test_lock_is_free_again_after_a_failed_refresh(tmp_path: Path) -> None:
    store = store_with_expired_login(tmp_path)
    source = RefreshingTokenSource(
        store, FakeOAuthTransport([oauth_error(400, "invalid_grant")]), fixed_clock
    )
    with pytest.raises(GuardAuthError):
        source.access_token()
    with store.lock():
        pass


# --- forced refresh ---------------------------------------------------------------------------


def test_force_refresh_posts_even_when_the_stored_token_is_valid(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save(make_credentials(expires_at=NOW + timedelta(hours=1), obtained_at=NOW))
    transport = FakeOAuthTransport([rotated()])
    source = RefreshingTokenSource(store, transport, fixed_clock)

    assert source.access_token() == FAKE_ACCESS_TOKEN
    assert source.access_token(force_refresh=True) == ROTATED_ACCESS_TOKEN
    assert len(transport.requests) == 1


def test_force_refresh_uses_the_file_when_another_process_refreshed_first(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.save(make_credentials(expires_at=NOW + timedelta(hours=1), obtained_at=NOW))
    transport = FakeOAuthTransport([])
    source = RefreshingTokenSource(store, transport, fixed_clock)
    assert source.access_token() == FAKE_ACCESS_TOKEN

    store.save(
        make_credentials(
            expires_at=NOW + timedelta(hours=1),
            obtained_at=NOW,
            access=ROTATED_ACCESS_TOKEN,
            refresh=ROTATED_REFRESH_TOKEN,
        )
    )

    assert source.access_token(force_refresh=True) == ROTATED_ACCESS_TOKEN
    assert transport.requests == []


# --- failure classes --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "kind"),
    [
        (oauth_error(400, "invalid_grant"), AuthFailureKind.AUTH_EXPIRED),
        (oauth_error(401, "invalid_grant"), AuthFailureKind.AUTH_EXPIRED),
        (oauth_error(400, "invalid_client"), AuthFailureKind.AUTH_CLIENT_GONE),
        (oauth_error(400, "invalid_scope"), AuthFailureKind.AUTH_REJECTED),
    ],
)
def test_invalid_grant_and_friends_need_a_new_login_and_leave_the_file_alone(
    tmp_path: Path, answer: HttpResponse, kind: AuthFailureKind
) -> None:
    store = store_with_expired_login(tmp_path)
    before = store.path.read_bytes()
    source = RefreshingTokenSource(store, FakeOAuthTransport([answer]), fixed_clock)

    with pytest.raises(GuardAuthError) as caught:
        source.access_token()

    assert caught.value.kind is kind
    assert caught.value.cause is GuardFailure.AUTH
    assert AUTH_HINT in str(caught.value)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(
    ("answer", "text"),
    [
        (HttpResponse(503, b"down"), "Guard login service unavailable (HTTP 503)"),
        (HttpResponse(429, b""), "Guard login service unavailable (HTTP 429)"),
        (HttpResponse(200, b"not json"), "Guard login service unavailable (HTTP 200)"),
        (
            TransportFailure("timeout", "request timed out"),
            "Guard login service unreachable (timeout)",
        ),
        (
            TransportFailure("network", "network error: URLError"),
            "Guard login service unreachable (network)",
        ),
    ],
)
def test_outages_are_unavailable_never_auth(
    tmp_path: Path, answer: HttpResponse | TransportFailure, text: str
) -> None:
    store = store_with_expired_login(tmp_path)
    before = store.path.read_bytes()
    source = RefreshingTokenSource(store, FakeOAuthTransport([answer]), fixed_clock)

    with pytest.raises(GuardCallError) as caught:
        source.access_token()

    assert not isinstance(caught.value, GuardAuthError)
    assert caught.value.cause is GuardFailure.UNAVAILABLE
    assert str(caught.value) == text
    assert AUTH_HINT not in str(caught.value)
    assert store.path.read_bytes() == before


def test_missing_login_file_needs_auth(tmp_path: Path) -> None:
    source = RefreshingTokenSource(make_store(tmp_path), FakeOAuthTransport([]), fixed_clock)

    with pytest.raises(GuardAuthError) as caught:
        source.access_token()

    assert caught.value.kind is AuthFailureKind.NOT_AUTHENTICATED
    assert AUTH_HINT in str(caught.value)


def test_corrupt_login_file_is_reported_and_never_deleted(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.path.parent.mkdir(parents=True)
    corrupt = b'{"refresh_token": "' + FAKE_REFRESH_TOKEN.encode() + b'", '
    store.path.write_bytes(corrupt)
    transport = FakeOAuthTransport([])

    with pytest.raises(GuardAuthError) as caught:
        RefreshingTokenSource(store, transport, fixed_clock).access_token()

    assert caught.value.kind is AuthFailureKind.NOT_AUTHENTICATED
    assert FAKE_REFRESH_TOKEN not in str(caught.value)
    assert store.path.read_bytes() == corrupt
    assert transport.requests == []


def test_scope_audits_run_is_refused_and_nothing_is_stored(tmp_path: Path) -> None:
    store = store_with_expired_login(tmp_path)
    before = store.path.read_bytes()
    answer = token_body(
        ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN, scope="projects:read audits:run"
    )
    source = RefreshingTokenSource(store, FakeOAuthTransport([answer]), fixed_clock)

    with pytest.raises(GuardAuthError) as caught:
        source.access_token()

    assert caught.value.kind is AuthFailureKind.AUTH_REJECTED
    assert store.path.read_bytes() == before


def test_scope_missing_from_the_answer_is_accepted(tmp_path: Path) -> None:
    store = store_with_expired_login(tmp_path)
    source = RefreshingTokenSource(
        store, FakeOAuthTransport([token_body(ROTATED_ACCESS_TOKEN, scope=None)]), fixed_clock
    )
    assert source.access_token() == ROTATED_ACCESS_TOKEN


def test_save_failure_keeps_the_run_going_and_tells_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = store_with_expired_login(tmp_path)

    def broken_save(self: CredentialStore, credentials: object) -> NoReturn:
        raise OSError(28, f"No space left on device: {self.path} {ROTATED_REFRESH_TOKEN}")

    monkeypatch.setattr(CredentialStore, "save", broken_save)
    source = RefreshingTokenSource(store, FakeOAuthTransport([rotated()]), fixed_clock)

    with caplog.at_level(logging.DEBUG):
        assert source.access_token() == ROTATED_ACCESS_TOKEN
        assert source.access_token() == ROTATED_ACCESS_TOKEN  # kept in memory, no second refresh

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert AUTH_HINT in errors[0].getMessage()
    assert "OSError" in errors[0].getMessage()
    assert ROTATED_REFRESH_TOKEN not in caplog.text


def test_no_backup_and_no_leftovers_after_several_refreshes(tmp_path: Path) -> None:
    store = store_with_expired_login(tmp_path)
    clock = FakeClock()
    transport = FakeOAuthTransport(
        [
            token_body(
                "fake-access-one-0123456789abcdefghijk",
                "fake-refresh-one-0123456789abcdefghij",
                200,
            ),
            token_body(
                "fake-access-two-0123456789abcdefghijk",
                "fake-refresh-two-0123456789abcdefghij",
                200,
            ),
            token_body(
                "fake-access-three-0123456789abcdefghij",
                "fake-refresh-three-0123456789abcdefgh",
                200,
            ),
        ]
    )
    source = RefreshingTokenSource(store, transport, clock)

    for _ in range(3):
        source.access_token()
        clock.now += timedelta(hours=1)

    assert len(transport.requests) == 3
    assert transport.requests[1][1]["refresh_token"] == "fake-refresh-one-0123456789abcdefghij"
    assert sorted(path.name for path in store.path.parent.iterdir()) == [
        "guard-auth.json",
        "guard-auth.lock",
    ]


# --- hygiene ----------------------------------------------------------------------------------


def run_success(source: RefreshingTokenSource) -> None:
    source.access_token()


def run_failing(source: RefreshingTokenSource) -> None:
    with pytest.raises(GuardCallError):
        source.access_token()


@pytest.mark.parametrize(
    ("script", "run"),
    [
        ([rotated()], run_success),
        ([oauth_error(400, "invalid_grant")], run_failing),
        ([oauth_error(400, "invalid_client")], run_failing),
        ([HttpResponse(503, ROTATED_REFRESH_TOKEN.encode())], run_failing),
        ([HttpResponse(400, ROTATED_ACCESS_TOKEN.encode())], run_failing),
        ([TransportFailure("timeout", "request timed out")], run_failing),
        (
            [token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN, scope="audits:run")],
            run_failing,
        ),
    ],
    ids=["success", "invalid_grant", "invalid_client", "503-body", "400-body", "timeout", "scope"],
)
def test_hygiene_no_token_reaches_logs_errors_or_reprs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    script: list[HttpResponse | TransportFailure],
    run: Callable[[RefreshingTokenSource], None],
) -> None:
    store = store_with_expired_login(tmp_path)
    source = RefreshingTokenSource(store, FakeOAuthTransport(script), fixed_clock)
    seen: list[str] = [repr(store), repr(source)]

    with caplog.at_level(logging.DEBUG):
        try:
            run(source)
        except GuardCallError as exc:
            seen += [str(exc), repr(exc)]

    seen.append(caplog.text)
    for text in seen:
        for token in ALL_FAKE_TOKENS:
            assert token not in text


def test_hygiene_for_login_file_problems(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    store = make_store(tmp_path)
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(b'{"access_token": "' + FAKE_ACCESS_TOKEN.encode() + b'"}')
    source = RefreshingTokenSource(store, FakeOAuthTransport([]), fixed_clock)

    with caplog.at_level(logging.DEBUG), pytest.raises(GuardAuthError) as caught:
        source.access_token()

    for text in (str(caught.value), repr(caught.value), caplog.text):
        for token in ALL_FAKE_TOKENS:
            assert token not in text
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# --- urllib transport -------------------------------------------------------------------------


def _refuse(error: BaseException) -> NoReturn:
    raise error


def test_urllib_transport_errors_carry_no_url(monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"https://fleet.enji.ai/api/v1/oauth/token?leak={FAKE_REFRESH_TOKEN}"
    http_error = urllib.error.HTTPError(
        url, 400, "Bad Request", email.message.Message(), io.BytesIO(b'{"error": "invalid_grant"}')
    )
    transport = UrllibOAuthTransport()
    form = {"grant_type": "refresh_token", "refresh_token": FAKE_REFRESH_TOKEN}

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _refuse(http_error))
    assert transport.post_form(url, form, 1.0) == HttpResponse(400, b'{"error": "invalid_grant"}')

    kinds = []
    for failure in (urllib.error.URLError(OSError(f"cannot reach {url}")), TimeoutError(url)):
        monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, _f=failure, **_k: _refuse(_f))
        with pytest.raises(TransportFailure) as info:
            transport.post_form(url, form, 1.0)
        kinds.append(info.value.kind)
        for text in (str(info.value), repr(info.value)):
            assert FAKE_REFRESH_TOKEN not in text
            assert "fleet.enji.ai" not in text
    assert kinds == ["network", "timeout"]


def test_urllib_transport_posts_a_form(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []

    class Answer:
        status = 200

        def __enter__(self) -> "Answer":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> Answer:
        del timeout
        seen.append(request)
        return Answer()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    response = UrllibOAuthTransport().post_form(
        TOKEN_ENDPOINT, {"grant_type": "refresh_token"}, 1.0
    )

    assert response == HttpResponse(200, b"{}")
    request = seen[0]
    assert request.get_method() == "POST"
    assert request.data == b"grant_type=refresh_token"
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert request.get_header("Accept") == "application/json"
