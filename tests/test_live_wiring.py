"""`run` without --fixtures: real-Guard wiring through a fake connector, no network.

Tracer first, then the unattended-run traps: dry-run keeps the rotated login, a blocked or
halted run never connects, a dead login is a counted failed poll, tokens follow the system clock.
"""

import io
import logging
import stat
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import pytest
from fake_oauth import (
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    ROTATED_ACCESS_TOKEN,
    ROTATED_REFRESH_TOKEN,
    FakeOAuthTransport,
    make_credentials,
    token_body,
)
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, ok_response

from enji_guard_radar import cli
from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard_auth import AUTH_FAILURE_TEXT
from enji_guard_radar.guard_credentials import CredentialStore, RefreshingTokenSource
from enji_guard_radar.ledger import State, deserialize_state
from enji_guard_radar.ports import (
    AuthFailureKind,
    GuardAuthError,
    GuardCallError,
    GuardSession,
)
from enji_guard_radar.state_store import FileStateStore

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
NOW = "2026-11-16T12:00:00Z"
GROUP = "-1001234567890"
OPS = "-1009990001"
ENVIRON = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": GROUP}
OPS_ENVIRON = {**ENVIRON, "TELEGRAM_OPS_CHAT_ID": OPS}
SECRETS = (
    FAKE_TOKEN,
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    ROTATED_ACCESS_TOKEN,
    ROTATED_REFRESH_TOKEN,
)


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


class FakeSession:
    """A GuardSession that serves the fixture recordings and counts enter and exit."""

    def __init__(
        self,
        *,
        enter_error: GuardCallError | None = None,
        before_call: Callable[[], object] | None = None,
    ) -> None:
        self._inner = FixtureGuardTools(FIXTURES)
        self._enter_error = enter_error
        self._before_call = before_call
        self.entered = 0
        self.exited = 0

    def __enter__(self) -> Self:
        self.entered += 1
        if self._enter_error is not None:
            raise self._enter_error
        return self

    def __exit__(self, *exc: object) -> None:
        self.exited += 1

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        if self._before_call is not None:
            self._before_call()
        return self._inner.call(tool, args)

    def tool_names(self) -> frozenset[str]:
        return frozenset()

    def tool_arguments(self) -> Mapping[str, tuple[str, ...]]:
        return {}

    def result_shapes(self) -> Mapping[str, int]:
        return {}


class Connector:
    """Records how often the radar asked for a session."""

    def __init__(self, session: FakeSession | None) -> None:
        self.session = session
        self.requested: list[Path] = []

    def __call__(self, state_dir: Path) -> GuardSession:
        self.requested.append(state_dir)
        if self.session is None:
            message = "the Guard connector must not be called"
            raise AssertionError(message)
        return self.session


class Run:
    def __init__(self, tmp_path: Path) -> None:
        self.state_dir = tmp_path / "state"
        self.tmp_path = tmp_path
        self.out = io.StringIO()

    def main(
        self,
        connector: Connector,
        *,
        extra: list[str] | None = None,
        environ: Mapping[str, str] = ENVIRON,
        transport: FakeTransport | None = None,
        now: str = NOW,
        config: Path | None = None,
    ) -> int:
        self.out = io.StringIO()
        return cli.main(
            [
                "run",
                "--config",
                str(config or FIXTURES / "radar.yaml"),
                "--state-dir",
                str(self.state_dir),
                "--env-file",
                str(self.tmp_path / "none.env"),
                "--now",
                now,
                *(extra or []),
            ],
            environ=environ,
            transport=transport if transport is not None else FakeTransport([]),
            sleep=FakeTime().sleep,
            stdout=self.out,
            guard_connector=connector,
        )

    def state(self) -> State:
        return deserialize_state((self.state_dir / "state.json").read_bytes())


def ops_config(tmp_path: Path) -> Path:
    path = tmp_path / "radar.yaml"
    path.write_text("ops:\n  failures_before_alert: 1\n", encoding="utf-8")
    return path


def token_connector(
    state_dir: Path, source_script: FakeOAuthTransport, expires_in: timedelta
) -> tuple[Connector, FakeSession]:
    """A connector built on the production credential store and token source."""
    real_now = datetime.now(UTC)
    store = CredentialStore(state_dir)
    store.save(make_credentials(expires_at=real_now + expires_in, obtained_at=real_now))
    source = RefreshingTokenSource(store, source_script, cli.system_clock)
    session = FakeSession(before_call=source.access_token)
    return Connector(session), session


def assert_no_secret_leaked(run: Run, capsys: pytest.CaptureFixture[str], caplog: object) -> None:
    captured = capsys.readouterr()
    records = getattr(caplog, "text", "")
    for secret in SECRETS:
        assert secret not in run.out.getvalue()
        assert secret not in captured.out
        assert secret not in captured.err
        assert secret not in records


def test_run_without_fixtures_reads_guard_through_the_connector(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    session = FakeSession()
    connector = Connector(session)
    run = Run(tmp_path)
    assert run.main(connector) == 0
    assert (run.state_dir / "state.json").exists()
    assert len(run.state().conditions) > 0
    assert connector.requested == [run.state_dir]
    assert (session.entered, session.exited) == (1, 1)
    cost_lines = [m for m in caplog.messages if m.startswith("poll cost: ")]
    assert len(cost_lines) == 1
    assert "list_projects 1" in cost_lines[0]


def test_dry_run_without_fixtures_prints_the_cost_after_the_summary(tmp_path: Path) -> None:
    session = FakeSession()
    run = Run(tmp_path)
    assert run.main(Connector(session), extra=["--dry-run"]) == 0
    lines = run.out.getvalue().splitlines()
    assert lines[-1].startswith("poll cost: ")
    assert lines[-2].startswith("summary: ")
    assert not (run.state_dir / "state.json").exists()
    assert (session.entered, session.exited) == (1, 1)


def test_fixtures_still_replay_without_a_connector(tmp_path: Path) -> None:
    out = io.StringIO()
    code = cli.main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(FIXTURES),
            "--state-dir",
            str(tmp_path / "state"),
            "--env-file",
            str(tmp_path / "none.env"),
            "--now",
            NOW,
        ],
        environ=ENVIRON,
        stdout=out,
        guard_connector=Connector(None),
    )
    assert code == 0
    assert out.getvalue().splitlines()[-1].startswith("poll cost: ")


def test_dry_run_persists_the_rotated_refresh_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    run = Run(tmp_path)
    oauth = FakeOAuthTransport([token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)])
    connector, session = token_connector(run.state_dir, oauth, timedelta(hours=-1))
    assert run.main(connector, extra=["--dry-run"]) == 0
    store = CredentialStore(run.state_dir)
    assert store.load().refresh_token.get_secret_value() == ROTATED_REFRESH_TOKEN
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not (run.state_dir / "state.json").exists()
    assert not (run.state_dir / "state.json.bak").exists()
    assert len(oauth.requests) == 1
    assert session.exited == 1
    assert_no_secret_leaked(run, capsys, caplog)


def test_lock_held_never_connects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    run = Run(tmp_path)
    connector = Connector(None)
    with FileStateStore(run.state_dir).lock() as acquired:
        assert acquired
        assert run.main(connector) == 0
    assert connector.requested == []
    assert_no_secret_leaked(run, capsys, caplog)


def test_corrupt_state_never_connects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    run = Run(tmp_path)
    run.state_dir.mkdir()
    (run.state_dir / "state.json").write_text("{not json", encoding="utf-8")
    connector = Connector(None)
    assert run.main(connector) == 2
    assert connector.requested == []
    assert_no_secret_leaked(run, capsys, caplog)


def test_connect_failure_auth_is_a_failed_poll_that_reaches_the_ops_chat(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    run = Run(tmp_path)
    error = GuardAuthError(
        AuthFailureKind.AUTH_EXPIRED, AUTH_FAILURE_TEXT[AuthFailureKind.AUTH_EXPIRED]
    )
    session = FakeSession(enter_error=error)
    transport = FakeTransport([ok_response()])
    code = run.main(
        Connector(session), environ=OPS_ENVIRON, transport=transport, config=ops_config(tmp_path)
    )
    assert code == 1
    health = run.state().health
    assert health.consecutive_failures == 1
    assert health.last_failure_cause == "auth"
    (request,) = transport.requests
    assert request[1]["chat_id"] == int(OPS)
    assert "Guard login expired or revoked: run `enji-guard-radar auth`" in str(
        request[1]["text"]
    ).replace("\\", "")
    assert session.exited == 0
    assert_no_secret_leaked(run, capsys, caplog)


def test_connect_failure_unavailable_twice_counts_two_failed_polls(tmp_path: Path) -> None:
    run = Run(tmp_path)
    for expected in (1, 2):
        session = FakeSession(enter_error=GuardCallError("Guard is unreachable"))
        assert run.main(Connector(session)) == 1
        assert run.state().health.consecutive_failures == expected
        assert session.entered == 1


def test_tokens_follow_the_system_clock_not_now(tmp_path: Path) -> None:
    run = Run(tmp_path)
    oauth = FakeOAuthTransport([])
    connector, _ = token_connector(run.state_dir, oauth, timedelta(hours=1))
    assert run.main(connector, now="2030-01-01T00:00:00Z") == 0
    assert oauth.requests == []


def test_a_call_failure_after_connecting_still_closes_the_session(tmp_path: Path) -> None:
    def explode() -> None:
        message = "Guard went away"
        raise GuardCallError(message)

    session = FakeSession(before_call=explode)
    run = Run(tmp_path)
    assert run.main(Connector(session)) == 1
    assert (session.entered, session.exited) == (1, 1)
