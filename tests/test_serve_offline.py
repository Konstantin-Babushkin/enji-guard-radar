"""`serve --fixtures --dry-run` and `--ready-file` through cli.main: no network, nothing written."""

import io
import logging
import signal
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport

from enji_guard_radar import cli
from enji_guard_radar.ports import GuardCallError, GuardSession

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
LIVE_ENVIRON = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "-1001234567890"}
START = datetime(2026, 11, 23, 2, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def forbidden_connector(_state_dir: Path) -> GuardSession:
    message = "the real Guard must not be used"
    raise AssertionError(message)


def serve(
    tmp_path: Path,
    flags: list[str],
    *,
    connector: object = forbidden_connector,
    environ: Mapping[str, str] | None = None,
    polls: int = 1,
) -> tuple[int, str]:
    """Run `serve` on a fake clock; SIGTERM arrives once `polls` slots have passed."""
    config = tmp_path / "radar.yaml"
    config.write_text("schedule:\n  every_minutes: 1\n", encoding="utf-8")
    now = START
    waits = 0
    out = io.StringIO()

    def wait(seconds: float) -> None:
        nonlocal now, waits
        now += timedelta(seconds=seconds)
        waits += 1
        if waits >= polls:
            signal.raise_signal(signal.SIGTERM)

    code = cli.main(
        [
            "serve",
            "--config",
            str(config),
            "--state-dir",
            str(tmp_path / "state"),
            "--env-file",
            str(tmp_path / "none.env"),
            *flags,
        ],
        environ=environ if environ is not None else {},
        transport=FakeTransport([]),
        sleep=FakeTime().sleep,
        stdout=out,
        guard_connector=connector,  # type: ignore[arg-type]
        serve_clock=lambda: now,
        serve_wait=wait,
    )
    return code, out.getvalue()


def test_serve_dry_run_on_fixtures_needs_no_secret_network_or_state(tmp_path: Path) -> None:
    code, out = serve(tmp_path, ["--fixtures", str(FIXTURES), "--dry-run"])
    assert code == 0
    assert "DRY RUN" in out
    assert "nothing is sent and no state is written" in out
    assert "summary: 5 evaluated" in out
    assert not (tmp_path / "state").exists()


def test_serve_dry_run_polls_again_on_every_slot_and_still_writes_nothing(tmp_path: Path) -> None:
    code, out = serve(tmp_path, ["--fixtures", str(FIXTURES), "--dry-run"], polls=3)
    assert code == 0
    assert out.count("DRY RUN") >= 2
    assert not (tmp_path / "state").exists()


def test_serve_with_fixtures_but_without_dry_run_behaves_like_run(tmp_path: Path) -> None:
    code, out = serve(tmp_path, ["--fixtures", str(FIXTURES)], environ=LIVE_ENVIRON)
    assert code == 0
    assert "DRY RUN" not in out
    assert (tmp_path / "state" / "state.json").exists()


def test_serve_without_the_offline_flags_still_uses_the_live_guard(tmp_path: Path) -> None:
    calls: list[Path] = []

    def connector(state_dir: Path) -> GuardSession:
        calls.append(state_dir)
        message = "no login in this test"
        raise GuardCallError(message)

    code, _ = serve(tmp_path, [], connector=connector, environ=LIVE_ENVIRON)
    assert code == 0
    assert calls


def test_ready_file_appears_after_a_poll_that_exits_zero(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    code, _ = serve(
        tmp_path, ["--fixtures", str(FIXTURES), "--dry-run", "--ready-file", str(ready)]
    )
    assert code == 0
    assert ready.exists()


def test_ready_file_is_not_written_when_the_poll_needs_attention(tmp_path: Path) -> None:
    ready = tmp_path / "ready"

    def failing(_state_dir: Path) -> GuardSession:
        message = "Guard login missing"
        raise GuardCallError(message)

    code, _ = serve(tmp_path, ["--ready-file", str(ready)], connector=failing, environ=LIVE_ENVIRON)
    assert code == 0
    assert not ready.exists()


def test_an_unwritable_ready_file_does_not_stop_serve(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    ready = tmp_path / "missing-dir" / "ready"
    code, _ = serve(
        tmp_path, ["--fixtures", str(FIXTURES), "--dry-run", "--ready-file", str(ready)]
    )
    assert code == 0
    assert not ready.exists()
    assert any("cannot write ready file" in m for m in caplog.messages)
    assert caplog.messages[-1] == "serve: stopped"
