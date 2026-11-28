"""The `serve` command: the same poll as `run`, repeated on the slots of `schedule.every_minutes`.

Shell module (signals, threads, state file stat). The slot math is pure and lives in `schedule`.
One thread, so a run never overlaps the next; the state lock keeps a hand-started `run` safe.
A stop request only sets a flag: the run in progress is finished (state writes are atomic),
no new run starts, and the exit code is 0.
"""

import argparse
import contextlib
import logging
import signal
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from enji_guard_radar.ports import Clock
from enji_guard_radar.schedule import (
    catch_up_due,
    format_slot,
    next_slot,
    skipped_slots,
)
from enji_guard_radar.state_store import last_poll_time

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "state"
DEFAULT_ENV_FILE = ".env"
# Never sleep longer than this at a time: the clock is read again after each chunk, so a host
# suspend or a clock jump does not make a slot late by more than this.
WAIT_CHUNK_SECONDS = 60.0
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


def add_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "serve", help="run the radar on the schedule from the config until stopped"
    )
    parser.add_argument("--config", type=Path, metavar="PATH")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(DEFAULT_STATE_DIR),
        metavar="DIR",
        help=f"directory for state.json and the lock (default: {DEFAULT_STATE_DIR})",
    )
    # SUPPRESS: a value given before the subcommand is not overwritten by this option's default.
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"dotenv file with Telegram settings (default: {DEFAULT_ENV_FILE}; may be missing)",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        metavar="DIR",
        help="replay recorded Guard answers from DIR on every poll (default: read real Guard)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print instead of sending on every poll; write nothing to the state directory",
    )
    parser.add_argument(
        "--ready-file",
        type=Path,
        metavar="PATH",
        help="write this file after every poll that exits 0 (a healthcheck probes it)",
    )


class StopRequest:
    """Set by a signal handler; read by the loop."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.name: str | None = None

    def request(self, name: str) -> None:
        if self.name is None:
            self.name = name
        self.event.set()


@contextlib.contextmanager
def install_signal_handlers(stop: StopRequest) -> Iterator[None]:
    """SIGTERM and SIGINT request a stop; the previous handlers come back on exit.

    The handler only sets a flag: no logging or I/O inside a signal handler.
    """

    def handler(signum: int, _frame: object) -> None:
        stop.request(signal.Signals(signum).name)

    previous = {sig: signal.signal(sig, handler) for sig in STOP_SIGNALS}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


def serve_loop(
    *,
    every_minutes: int,
    state_dir: Path,
    poll: Callable[[], int],
    clock: Clock,
    stop: StopRequest,
    wait: Callable[[float], object],
    current_every_minutes: Callable[[], int] | None = None,
) -> None:
    """Run `poll` on the slot grid until `stop` is set. `poll` returns the run's exit code.

    `current_every_minutes` is asked after each poll: a changed value (the sheet or the YAML
    changed `schedule.every_minutes`) sets the grid for the next slot, no restart needed.
    """

    def wait_until(slot_time: datetime) -> bool:
        """False when a stop was requested before the slot came."""
        while True:
            if stop.event.is_set():
                return False
            remaining = (slot_time - clock()).total_seconds()
            if remaining <= 0:
                return True
            wait(min(remaining, WAIT_CHUNK_SECONDS))

    begin = clock()
    catch_up = catch_up_due(begin, last_poll_time(state_dir), every_minutes)
    slot = begin if catch_up else next_slot(begin, every_minutes)
    reason = "catch-up" if catch_up else "scheduled"
    logger.info(
        "serve: started every_minutes=%d first_run=%s reason=%s",
        every_minutes,
        format_slot(slot),
        reason,
    )
    while wait_until(slot):
        started = clock()
        logger.info(
            "serve: run start slot=%s reason=%s next_slot=%s",
            format_slot(slot),
            reason,
            format_slot(next_slot(started, every_minutes)),
        )
        try:
            code = poll()
        except Exception:
            logger.exception("serve: run crashed; the loop continues")
            code = 2
        ended = clock()
        missed = skipped_slots(started, ended, every_minutes)
        if current_every_minutes is not None:
            refreshed = current_every_minutes()
            if refreshed != every_minutes:
                logger.info(
                    "serve: schedule changed every_minutes=%d->%d, applies from the next slot",
                    every_minutes,
                    refreshed,
                )
                every_minutes = refreshed
        slot = next_slot(ended, every_minutes)
        reason = "scheduled"
        logger.info(
            "serve: run finish exit=%d duration=%.1fs next_slot=%s",
            code,
            (ended - started).total_seconds(),
            format_slot(slot),
        )
        if missed:
            logger.warning(
                "serve: run overran, skipped slot(s): %s", ", ".join(format_slot(m) for m in missed)
            )
    if stop.event.is_set():
        logger.info("serve: stop requested (%s)", stop.name)
    logger.info("serve: stopped")


def mark_ready(path: Path) -> None:
    """Record that a poll finished with exit 0. A failure to write is logged, never raised."""
    try:
        path.touch()
    except OSError as error:
        logger.warning("serve: cannot write ready file %s (%s)", path, type(error).__name__)
        return
    logger.info("serve: ready file written path=%s", path)


def run_serve(
    *,
    every_minutes: int,
    state_dir: Path,
    poll: Callable[[], int],
    clock: Clock,
    wait: Callable[[float], object] | None = None,
    current_every_minutes: Callable[[], int] | None = None,
) -> int:
    """Install the stop handlers and loop. Returns 0 once stopped by a signal."""
    stop = StopRequest()
    pause = wait if wait is not None else stop.event.wait
    with install_signal_handlers(stop):
        serve_loop(
            every_minutes=every_minutes,
            state_dir=state_dir,
            poll=poll,
            clock=clock,
            stop=stop,
            wait=pause,
            current_every_minutes=current_every_minutes,
        )
    return 0
