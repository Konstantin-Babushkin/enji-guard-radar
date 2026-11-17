"""The shell: argument parsing, env file, config file, output. Everything with I/O lives here."""

import argparse
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TextIO

from pydantic import SecretStr

from enji_guard_radar import cli_auth
from enji_guard_radar.config import ConfigError, RadarConfig, parse_config
from enji_guard_radar.env_file import EnvFileError, read_env_file
from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard import GuardDataError, parse_guard_time
from enji_guard_radar.guard_credentials import LoginTransport, UrllibOAuthTransport
from enji_guard_radar.ledger import StateStore
from enji_guard_radar.messages import render_alert
from enji_guard_radar.model import Audience, OutboundMessage
from enji_guard_radar.pipeline import Deps, ProjectStatus, RunReport, run_once
from enji_guard_radar.ports import Clock, GuardCallError, Notifier, Printed
from enji_guard_radar.secret_guard import SecretGatedNotifier, configure_logging, redact
from enji_guard_radar.settings import (
    ENV_CHAT_ID,
    ENV_OPS_CHAT_ID,
    SettingsError,
    TelegramSettings,
    known_secrets,
    load_settings,
    require_live,
)
from enji_guard_radar.stale import StaleVerdict
from enji_guard_radar.state_store import DryRunStateStore, FileStateStore
from enji_guard_radar.telegram import HttpTransport, TelegramNotifier, UrllibTransport

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "state"
DEFAULT_ENV_FILE = ".env"


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

    def __init__(self, messages: list[OutboundMessage] | None = None) -> None:
        self.messages = messages if messages is not None else []

    def send(self, message: OutboundMessage) -> Printed:
        self.messages.append(message)
        return Printed()


def _counts(report: RunReport) -> tuple[int, int, int, int]:
    """evaluated, stale, never ran, not checked."""
    unchecked = sum(1 for r in report.results if r.status is not ProjectStatus.EVALUATED)
    verdicts = [r.stale.verdict for r in report.results if r.stale is not None]
    return (
        len(report.results) - unchecked,
        verdicts.count(StaleVerdict.STALE),
        verdicts.count(StaleVerdict.NEVER_RAN),
        unchecked,
    )


def format_dry_run_report(report: RunReport, printed: Sequence[OutboundMessage]) -> str:
    now = report.now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    lines = [f"DRY RUN at {now}: nothing is sent and no state is written."]
    for message in printed:
        lines += [f"--- would send to {message.audience.value} ---", message.text, ""]
    lines.append(f"--- baseline, not sent ({len(report.baselined)}) ---")
    for alert in report.baselined:
        lines += [render_alert(alert), ""]
    unchecked = [r for r in report.results if r.status is not ProjectStatus.EVALUATED]
    lines.append(f"--- not checked ({len(unchecked)}) ---")
    lines += [f"{r.project_name}: {r.note}" for r in unchecked]
    evaluated, stale, never_ran, not_checked = _counts(report)
    would_send = sum(1 for m in printed if m.audience is Audience.GROUP)
    lines.append(
        f"summary: {evaluated} evaluated, {stale} stale, {never_ran} never ran, "
        f"{not_checked} not checked, {would_send} message(s) would be sent, "
        f"{len(report.baselined)} baselined"
    )
    return "\n".join(lines) + "\n"


def format_live_summary(report: RunReport) -> str:
    evaluated, stale, never_ran, not_checked = _counts(report)
    return (
        f"summary: {evaluated} evaluated, {stale} stale, {never_ran} never ran, "
        f"{not_checked} not checked, {report.delivered} message(s) sent, "
        f"{len(report.baselined)} baselined, {report.not_delivered} not delivered\n"
    )


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
    run.add_argument(
        "--dry-run", action="store_true", help="print instead of sending; write nothing"
    )
    run.add_argument("--config", type=Path, metavar="PATH")
    run.add_argument(
        "--state-dir",
        type=Path,
        default=Path(DEFAULT_STATE_DIR),
        metavar="DIR",
        help=f"directory for state.json and the lock (default: {DEFAULT_STATE_DIR})",
    )
    # SUPPRESS: a value given before the subcommand is not overwritten by this option's default.
    run.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"dotenv file with Telegram settings (default: {DEFAULT_ENV_FILE}; may be missing)",
    )
    run.add_argument(
        "--now", metavar="ISO8601", help="fixed clock, for example 2026-11-16T12:00:00Z"
    )
    cli_auth.add_parser(commands)
    return parser


def _fixed_clock(moment: datetime) -> Clock:
    return lambda: moment


def _telegram_notifier(
    token: SecretStr,
    chat_id: str,
    env_name: str,
    transport: HttpTransport,
    sleep: Callable[[float], None],
    secrets: tuple[str, ...],
) -> Notifier:
    inner = TelegramNotifier(token, chat_id, transport, chat_env_name=env_name, sleep=sleep)
    return SecretGatedNotifier(inner, secrets)


def _run(
    args: argparse.Namespace,
    out: TextIO,
    *,
    environ: Mapping[str, str],
    transport: HttpTransport | None,
    sleep: Callable[[float], None] | None,
) -> int:
    env_file: Path = args.env_file or Path(DEFAULT_ENV_FILE)
    try:
        settings = load_settings(environ, env_file)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    secrets = known_secrets(settings)
    configure_logging(level=logging.INFO, known_secrets=secrets, stream=sys.stderr)

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

    printer = PrintNotifier()
    notifier: Notifier
    ops_notifier: Notifier | None
    store: StateStore
    if args.dry_run:
        notifier = SecretGatedNotifier(printer, secrets)
        ops_notifier = SecretGatedNotifier(printer, secrets)
        store = DryRunStateStore(FileStateStore(args.state_dir))
    else:
        try:
            store, notifier, ops_notifier = _live_wiring(
                settings, args.state_dir, transport, sleep, secrets
            )
        except SettingsError as error:
            logger.error("run rejected: %s", error)  # noqa: TRY400
            print(f"error: {error}", file=sys.stderr)
            return 2
    deps = Deps(
        guard=guard,
        notifier=notifier,
        config=config,
        clock=clock,
        store=store,
        ops_notifier=ops_notifier,
    )
    try:
        report = run_once(deps)
    except Exception:
        logger.exception("unexpected error; the run stopped")
        return 2
    text = (
        format_dry_run_report(report, printer.messages)
        if args.dry_run
        else format_live_summary(report)
    )
    out.write(redact(text, secrets))
    return report.exit_code


def _live_wiring(
    settings: TelegramSettings,
    state_dir: Path,
    transport: HttpTransport | None,
    sleep: Callable[[float], None] | None,
    secrets: tuple[str, ...],
) -> tuple[StateStore, Notifier, Notifier | None]:
    """Raises SettingsError when the token or the chat id is missing."""
    live = require_live(settings)
    http = transport if transport is not None else UrllibTransport()
    pause = sleep if sleep is not None else time.sleep
    group = _telegram_notifier(live.bot_token, live.chat_id, ENV_CHAT_ID, http, pause, secrets)
    ops = (
        _telegram_notifier(live.bot_token, live.ops_chat_id, ENV_OPS_CHAT_ID, http, pause, secrets)
        if live.ops_chat_id is not None
        else None
    )
    return FileStateStore(state_dir), group, ops


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    transport: HttpTransport | None = None,
    sleep: Callable[[float], None] | None = None,
    stdout: TextIO | None = None,
    oauth_transport: LoginTransport | None = None,
    prompt: Callable[[str], str] | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    args = _build_parser().parse_args(argv)

    if args.command == "run":
        return _run(
            args,
            out,
            environ=environ if environ is not None else os.environ,
            transport=transport,
            sleep=sleep,
        )

    if args.command == "auth":
        return cli_auth.run_auth(
            args,
            out=out,
            transport=oauth_transport or UrllibOAuthTransport(),
            prompt=prompt,
            clock=system_clock,
            new_secret=None,
        )

    if args.env_file is not None:
        try:
            env_values = read_env_file(args.env_file)
        except EnvFileError as error:
            logger.error("env file rejected: %s", error)  # noqa: TRY400
            print(f"error: {error}", file=sys.stderr)
            return 2
        logger.info("loaded %d variables from %s", len(env_values), args.env_file)

    print("enji-guard-radar: not implemented yet", file=out)
    return 0
