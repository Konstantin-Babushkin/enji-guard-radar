"""The serve loop with a fake clock, a fake wait and a fake poll: no sleeping, no network."""

import logging
import os
import signal
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from enji_guard_radar.cli_serve import (
    WAIT_CHUNK_SECONDS,
    StopRequest,
    install_signal_handlers,
    serve_loop,
)

EVERY = 180
UTC_DAY = datetime(2026, 11, 23, tzinfo=UTC)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return UTC_DAY.replace(hour=hour, minute=minute, second=second)


class World:
    """A mutable fake clock plus the recorded waits, polls and a script for each poll."""

    def __init__(self, start: datetime, state_dir: Path) -> None:
        self.now = start
        self.state_dir = state_dir
        self.stop = StopRequest()
        self.waits: list[float] = []
        self.poll_times: list[datetime] = []
        self.durations: list[timedelta] = []
        self.codes: list[int | Exception] = []
        self.stop_after_polls: int | None = None
        self.stop_during_poll: int | None = None
        self.stop_after_waits: int | None = None

    def clock(self) -> datetime:
        return self.now

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        assert len(self.waits) < 10_000, "the loop never stopped"
        self.now += timedelta(seconds=seconds)
        if self.stop_after_waits is not None and len(self.waits) >= self.stop_after_waits:
            self.stop.request("SIGTERM")

    def poll(self) -> int:
        self.poll_times.append(self.now)
        number = len(self.poll_times)
        if self.stop_during_poll == number:
            self.stop.request("SIGTERM")
        step = self.durations[number - 1] if number <= len(self.durations) else timedelta(0)
        self.now += step
        result = self.codes[number - 1] if number <= len(self.codes) else 0
        if self.stop_after_polls == number:
            self.stop.request("SIGTERM")
        if isinstance(result, Exception):
            raise result
        return result

    def run(self) -> None:
        serve_loop(
            every_minutes=EVERY,
            state_dir=self.state_dir,
            poll=self.poll,
            clock=self.clock,
            stop=self.stop,
            wait=self.wait,
        )


@pytest.fixture(autouse=True)
def info_logs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)


def world(start: datetime, tmp_path: Path, *, last_poll: datetime | None = None) -> World:
    if last_poll is not None:
        state = tmp_path / "state.json"
        state.write_text("{}", encoding="utf-8")
        os.utime(state, (last_poll.timestamp(), last_poll.timestamp()))
    return World(start, tmp_path)


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.endswith("cli_serve")]


def test_catch_up_runs_at_once_when_there_is_no_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = world(at(4, 10), tmp_path)
    w.stop_after_polls = 1
    w.run()
    assert w.poll_times == [at(4, 10)]
    assert w.waits == []
    lines = messages(caplog)
    assert (
        lines[0]
        == "serve: started every_minutes=180 first_run=2026-11-23T04:10:00Z reason=catch-up"
    )
    assert lines[1] == (
        "serve: run start slot=2026-11-23T04:10:00Z reason=catch-up next_slot=2026-11-23T06:00:00Z"
    )
    assert lines[2] == ("serve: run finish exit=0 duration=0.0s next_slot=2026-11-23T06:00:00Z")
    assert lines[-2:] == ["serve: stop requested (SIGTERM)", "serve: stopped"]


def test_catch_up_when_the_last_poll_is_before_the_last_slot(tmp_path: Path) -> None:
    w = world(at(4, 10), tmp_path, last_poll=at(2, 50))
    w.stop_after_polls = 1
    w.run()
    assert w.poll_times == [at(4, 10)]


def test_no_catch_up_when_the_last_poll_is_after_the_last_slot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = world(at(4, 10), tmp_path, last_poll=at(3, 5))
    w.stop_after_polls = 1
    w.run()
    assert w.poll_times == [at(6)]
    assert "reason=scheduled" in messages(caplog)[0]
    assert "reason=scheduled" in messages(caplog)[1]


def test_two_scheduled_slots_run_exactly_on_the_grid(tmp_path: Path) -> None:
    w = world(at(4, 10), tmp_path, last_poll=at(3, 5))
    w.stop_after_polls = 2
    w.run()
    assert w.poll_times == [at(6), at(9)]


def test_a_run_longer_than_a_slot_skips_the_missed_slots(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = world(at(2, 0), tmp_path, last_poll=at(0, 30))
    w.durations = [timedelta(hours=7, minutes=30)]
    w.stop_after_polls = 2
    w.run()
    # first run at 03:00 lasts to 10:30: slots 06:00 and 09:00 are skipped, next run is 12:00
    assert w.poll_times == [at(3), at(12)]
    assert [m for m in messages(caplog) if "overran" in m] == [
        "serve: run overran, skipped slot(s): 2026-11-23T06:00:00Z, 2026-11-23T09:00:00Z"
    ]
    assert any(
        m == "serve: run finish exit=0 duration=27000.0s next_slot=2026-11-23T12:00:00Z"
        for m in messages(caplog)
    )


def test_a_short_run_logs_no_overrun(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    w = world(at(4, 10), tmp_path, last_poll=at(3, 5))
    w.durations = [timedelta(minutes=5)]
    w.stop_after_polls = 1
    w.run()
    assert not [m for m in messages(caplog) if "overran" in m]


@pytest.mark.parametrize("failure", [1, 2, RuntimeError("boom")])
def test_a_failed_or_raising_run_does_not_end_the_loop(
    tmp_path: Path, failure: int | Exception, caplog: pytest.LogCaptureFixture
) -> None:
    w = world(at(4, 10), tmp_path, last_poll=at(3, 5))
    w.codes = [failure, 0]
    w.stop_after_polls = 2
    w.run()
    assert w.poll_times == [at(6), at(9)]
    code = 2 if isinstance(failure, Exception) else failure
    assert any(f"serve: run finish exit={code} " in m for m in messages(caplog))
    if isinstance(failure, Exception):
        assert any(r.exc_info for r in caplog.records)


def test_stop_during_the_wait_runs_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = world(at(4, 10), tmp_path, last_poll=at(3, 5))
    w.stop_after_waits = 3
    w.run()
    assert w.poll_times == []
    assert messages(caplog)[-2:] == ["serve: stop requested (SIGTERM)", "serve: stopped"]


def test_stop_before_the_first_run_runs_nothing(tmp_path: Path) -> None:
    w = world(at(4, 10), tmp_path)  # catch-up would run at once
    w.stop.request("SIGINT")
    w.run()
    assert w.poll_times == []
    assert w.waits == []


def test_stop_during_a_run_finishes_it_and_starts_no_other(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = world(at(4, 10), tmp_path, last_poll=at(3, 5))
    w.durations = [timedelta(minutes=2)]
    w.stop_during_poll = 1
    w.run()
    assert w.poll_times == [at(6)]
    lines = messages(caplog)
    assert any(m.startswith("serve: run finish exit=0 duration=120.0s") for m in lines)
    assert lines[-2:] == ["serve: stop requested (SIGTERM)", "serve: stopped"]


def test_waits_are_short_chunks_and_the_clock_is_reread(tmp_path: Path) -> None:
    w = world(at(0, 5), tmp_path, last_poll=at(0, 1))
    w.stop_after_polls = 1
    w.run()
    assert w.poll_times == [at(3)]
    assert max(w.waits) <= WAIT_CHUNK_SECONDS
    assert sum(w.waits) == pytest.approx((at(3) - at(0, 5)).total_seconds())


def test_a_run_never_starts_before_its_slot(tmp_path: Path) -> None:
    w = world(at(2, 59, 59), tmp_path, last_poll=at(1))
    w.stop_after_polls = 1
    w.run()
    assert w.poll_times == [at(3)]
    assert w.waits == [1.0]


@pytest.fixture
def restore_signals() -> Iterator[None]:
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


@pytest.mark.parametrize(("sig", "name"), [(signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")])
@pytest.mark.usefixtures("restore_signals")
def test_signal_handlers_set_the_flag_and_are_restored(sig: signal.Signals, name: str) -> None:
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    stop = StopRequest()
    with install_signal_handlers(stop):
        assert not stop.event.is_set()
        signal.raise_signal(sig)
        assert stop.event.is_set()
        assert stop.name == name
        signal.raise_signal(signal.SIGTERM if sig is signal.SIGINT else signal.SIGINT)
        assert stop.name == name  # the first signal's name wins
    assert {s: signal.getsignal(s) for s in before} == before
