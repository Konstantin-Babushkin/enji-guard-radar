"""The shell: argument parsing, env file, config file, output. Everything with I/O lives here."""

import argparse
import logging
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TextIO

from enji_guard_radar.config import ConfigError, RadarConfig, parse_config
from enji_guard_radar.env_file import EnvFileError, read_env_file
from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard import GuardDataError, parse_guard_time
from enji_guard_radar.model import OutboundMessage
from enji_guard_radar.pipeline import Deps, ProjectStatus, RunReport, run_once
from enji_guard_radar.ports import Clock, GuardCallError, Printed
from enji_guard_radar.stale import StaleVerdict

logger = logging.getLogger(__name__)


def system_clock() -> datetime:
    """The only system clock in the package."""
    return datetime.now(UTC)


def read_config(path: Path | None) -> RadarConfig:
    if path is None:
        return RadarConfig()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        message = f"config file not found: {path}"
        raise ConfigError(message) from None
    except (OSError, UnicodeDecodeError):
        message = f"cannot read config file {path}"
        raise ConfigError(message) from None
    return parse_config(text, source=str(path))


class PrintNotifier:
    """Dry-run notifier: remembers what would be sent and sends nothing."""

    def __init__(self, messages: list[OutboundMessage]) -> None:
        self.messages = messages

    def send(self, message: OutboundMessage) -> Printed:
        self.messages.append(message)
        return Printed()


def format_dry_run_report(report: RunReport, printed: Sequence[OutboundMessage]) -> str:
    now = report.now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    lines = [f"DRY RUN at {now}: nothing is sent and no state is written."]
    for message in printed:
        lines += [f"--- would send to {message.audience.value} ---", message.text, ""]
    unchecked = [r for r in report.results if r.status is not ProjectStatus.EVALUATED]
    lines.append(f"--- not checked ({len(unchecked)}) ---")
    lines += [f"{r.project_name}: {r.note}" for r in unchecked]
    verdicts = [r.stale.verdict for r in report.results if r.stale is not None]
    evaluated = len(report.results) - len(unchecked)
    stale = verdicts.count(StaleVerdict.STALE)
    never_ran = verdicts.count(StaleVerdict.NEVER_RAN)
    lines.append(
        f"summary: {evaluated} evaluated, {stale} stale, {never_ran} never ran, "
        f"{len(unchecked)} not checked, {len(printed)} message(s) would be sent"
    )
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enji-guard-radar")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('enji-guard-radar')}",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="dotenv file (KEY=VALUE per line) with credentials; fails if missing or unreadable",
    )
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="run the radar once")
    run.add_argument("--fixtures", type=Path, required=True, metavar="DIR")
    # Required for now: plan 01-07 makes it optional when the live notifier is wired.
    run.add_argument("--dry-run", action="store_true", required=True)
    run.add_argument("--config", type=Path, metavar="PATH")
    run.add_argument(
        "--now", metavar="ISO8601", help="fixed clock, for example 2026-11-16T12:00:00Z"
    )
    return parser


def _fixed_clock(moment: datetime) -> Clock:
    return lambda: moment


def _run(args: argparse.Namespace, out: TextIO) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    clock: Clock = system_clock
    if args.now is not None:
        try:
            clock = _fixed_clock(parse_guard_time(args.now))
        except GuardDataError:
            logger.error("run rejected: invalid --now value")  # noqa: TRY400
            print(
                "error: invalid --now value, expected ISO 8601 like 2026-11-16T12:00:00Z",
                file=sys.stderr,
            )
            return 2
    try:
        config = read_config(args.config)
        guard = FixtureGuardTools(args.fixtures)
    except (ConfigError, GuardCallError) as error:
        logger.error("run rejected: %s", error)  # noqa: TRY400
        print(f"error: {error}", file=sys.stderr)
        return 2
    notifier = PrintNotifier([])
    report = run_once(Deps(guard=guard, notifier=notifier, config=config, clock=clock))
    out.write(format_dry_run_report(report, notifier.messages))
    return report.exit_code


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout if stdout is not None else sys.stdout
    args = _build_parser().parse_args(argv)

    if args.env_file is not None:
        try:
            env_values = read_env_file(args.env_file)
        except EnvFileError as error:
            logger.error("env file rejected: %s", error)  # noqa: TRY400
            print(f"error: {error}", file=sys.stderr)
            return 2
        logger.info("loaded %d variables from %s", len(env_values), args.env_file)

    if args.command == "run":
        return _run(args, out)

    print("enji-guard-radar: not implemented yet", file=out)
    return 0
