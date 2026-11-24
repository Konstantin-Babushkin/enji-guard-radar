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

from enji_guard_radar import cli_auth, cli_doctor, cli_probe, cli_serve
from enji_guard_radar.config import ConfigError, RadarConfig, parse_config
from enji_guard_radar.env_file import EnvFileError, read_env_file
from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.guard import GuardDataError, parse_guard_time
from enji_guard_radar.guard_credentials import LoginTransport, UrllibOAuthTransport
from enji_guard_radar.ledger import StateStore
from enji_guard_radar.mcp_guard import open_guard
from enji_guard_radar.messages import render_alert
from enji_guard_radar.model import Audience, OutboundMessage
from enji_guard_radar.pipeline import Deps, DigestStatus, ProjectStatus, RunReport, run_once
from enji_guard_radar.poll_cost import CountingGuardTools, format_poll_cost
from enji_guard_radar.ports import (
    Clock,
    GuardCallError,
    GuardConnector,
    GuardSession,
    GuardTools,
    Notifier,
    Printed,
    Triager,
)
from enji_guard_radar.secret_guard import SecretGatedNotifier, configure_logging, redact
from enji_guard_radar.settings import (
    ENV_CHAT_ID,
    ENV_OPS_CHAT_ID,
    SettingsError,
    TelegramSettings,
    known_secrets,
    load_claude_token,
    load_settings,
    require_live,
)
from enji_guard_radar.stale import StaleVerdict
from enji_guard_radar.state_store import DryRunStateStore, FileStateStore
from enji_guard_radar.telegram import HttpTransport, TelegramNotifier, UrllibTransport
from enji_guard_radar.texts import DEFAULT_LANGUAGE, Language
from enji_guard_radar.triage_cost import TriageCostStore
from enji_guard_radar.triage_cost_store import FileTriageCostStore, ReadOnlyTriageCostStore

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


def _follow_ups_line(report: RunReport) -> str | None:
    reminders = len(report.reminder_messages)
    if not reminders and report.digest is DigestStatus.NOT_DUE:
        return None
    return f"follow-ups: {reminders} reminder message(s), digest {report.digest.value}"


def format_dry_run_report(
    report: RunReport, printed: Sequence[OutboundMessage], *, language: Language = DEFAULT_LANGUAGE
) -> str:
    now = report.now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    lines = [f"DRY RUN at {now}: nothing is sent and no state is written."]
    for message in printed:
        lines += [f"--- would send to {message.audience.value} ---", message.text, ""]
    lines.append(f"--- baseline, not sent ({len(report.baselined)}) ---")
    for alert in report.baselined:
        lines += [render_alert(alert, language=language), ""]
    unchecked = [r for r in report.results if r.status is not ProjectStatus.EVALUATED]
    lines.append(f"--- not checked ({len(unchecked)}) ---")
    lines += [f"{r.project_name}: {r.note}" for r in unchecked]
    evaluated, stale, never_ran, not_checked = _counts(report)
    would_send = sum(1 for m in printed if m.audience is Audience.GROUP)
    follow_ups = _follow_ups_line(report)
    if follow_ups is not None:
        lines.append(follow_ups)
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
    run.add_argument(
        "--fixtures",
        type=Path,
        metavar="DIR",
        help="replay recorded Guard answers from DIR (default: read real Guard)",
    )
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
    cli_doctor.add_parser(commands)
    cli_probe.add_parser(commands)
    cli_serve.add_parser(commands)
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


class _LazyGuard:
    """Real Guard that connects on the first call, so a run that never gets that far never connects.

    A failed connection is remembered: every later call raises the same error and nothing
    reconnects.
    """

    def __init__(self, connector: GuardConnector, state_dir: Path) -> None:
        self._connector = connector
        self._state_dir = state_dir
        self._session: GuardSession | None = None
        self._entered = False
        self._error: GuardCallError | None = None

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        if self._error is not None:
            raise self._error
        if self._session is None:
            try:
                session = self._connector(self._state_dir)
                self._session = session
                session.__enter__()
                self._entered = True
            except GuardCallError as error:
                logger.error("Guard connection failed: %s", error)  # noqa: TRY400
                self._error = error
                raise
            logger.info("connected to Guard")
        return self._session.call(tool, args)

    def close(self) -> None:
        if self._session is not None and self._entered:
            self._entered = False
            self._session.__exit__(None, None, None)


def _triage_wiring(
    config: RadarConfig,
    settings: TelegramSettings,
    environ: Mapping[str, str],
    env_file: Path,
    state_dir: Path,
    *,
    dry_run: bool,
) -> tuple[Triager, TriageCostStore, tuple[str, ...]]:
    """The Claude triager, its cost store and the secrets to redact. Only called when enabled."""
    # Lazy: with triage off the adapter module is never imported (D-03).
    from enji_guard_radar.claude_cli_triager import ClaudeCliTriager

    token = load_claude_token(environ, env_file)
    triager = ClaudeCliTriager(
        token=token.get_secret_value() if token is not None else None,
        model=config.triage.model,
        timeout_seconds=config.triage.timeout_seconds,
        max_cost_usd=config.triage.max_cost_usd_per_call,
    )
    file_store = FileTriageCostStore(state_dir)
    cost_store: TriageCostStore = ReadOnlyTriageCostStore(file_store) if dry_run else file_store
    return triager, cost_store, known_secrets(settings, token)


def _run(
    args: argparse.Namespace,
    out: TextIO,
    *,
    environ: Mapping[str, str],
    transport: HttpTransport | None,
    sleep: Callable[[float], None] | None,
    guard_connector: GuardConnector | None,
    clock: Clock | None = None,
) -> int:
    env_file: Path = args.env_file or Path(DEFAULT_ENV_FILE)
    try:
        settings = load_settings(environ, env_file)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    secrets = known_secrets(settings)
    configure_logging(level=logging.INFO, known_secrets=secrets, stream=sys.stderr)

    clock = clock if clock is not None else system_clock
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
    lazy: _LazyGuard | None = None
    source: GuardTools
    try:
        config = read_config(args.config)
        if args.fixtures is not None:
            source = FixtureGuardTools(args.fixtures)
        else:
            # Tokens are always judged by the system clock, never by --now.
            connector = guard_connector or (lambda d: open_guard(d, clock=system_clock))
            lazy = source = _LazyGuard(connector, args.state_dir)
    except (ConfigError, GuardCallError) as error:
        logger.error("run rejected: %s", error)  # noqa: TRY400
        print(f"error: {error}", file=sys.stderr)
        return 2

    triager: Triager | None = None
    cost_store: TriageCostStore | None = None
    triage_secrets: tuple[str, ...] = ()
    if config.triage.enabled:
        triager, cost_store, secrets = _triage_wiring(
            config, settings, environ, env_file, args.state_dir, dry_run=args.dry_run
        )
        triage_secrets = secrets
        # The Claude token must be redacted from logs and output too (D-15).
        configure_logging(level=logging.INFO, known_secrets=secrets, stream=sys.stderr)

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
    guard = CountingGuardTools(source, time.monotonic)
    deps = Deps(
        guard=guard,
        notifier=notifier,
        config=config,
        clock=clock,
        store=store,
        ops_notifier=ops_notifier,
        triager=triager,
        cost_store=cost_store,
        known_secrets=triage_secrets,
    )
    try:
        report = run_once(deps)
    except Exception:
        logger.exception("unexpected error; the run stopped")
        return 2
    finally:
        if lazy is not None:
            lazy.close()
    cost_line = format_poll_cost(
        guard.cost(),
        projects=len(report.results),
        repositories=sum(r.stale.repo_count for r in report.results if r.stale is not None),
    )
    logger.info("%s", cost_line)
    if args.dry_run:
        dry_run = format_dry_run_report(report, printer.messages, language=config.messages.language)
        text = dry_run + cost_line + "\n"
    else:
        text = format_live_summary(report)
        follow_ups = _follow_ups_line(report)
        if follow_ups is not None:
            text += follow_ups + "\n"
    out.write(redact(text, secrets))
    return report.exit_code


def _serve(
    args: argparse.Namespace,
    out: TextIO,
    *,
    environ: Mapping[str, str],
    transport: HttpTransport | None,
    sleep: Callable[[float], None] | None,
    guard_connector: GuardConnector | None,
    clock: Clock,
    wait: Callable[[float], object] | None,
) -> int:
    """Poll on the slots of `schedule.every_minutes` until SIGTERM or SIGINT; exit 0."""
    configure_logging(level=logging.INFO, known_secrets=(), stream=sys.stderr)
    try:
        config = read_config(args.config)
    except ConfigError as error:
        logger.error("serve rejected: %s", error)  # noqa: TRY400
        print(f"error: {error}", file=sys.stderr)
        return 2
    # What `run` takes: a live poll, real Guard, no fixed clock. Config and env are read per run.
    poll_args = argparse.Namespace(
        fixtures=None,
        dry_run=False,
        now=None,
        config=args.config,
        state_dir=args.state_dir,
        env_file=args.env_file,
    )

    def poll() -> int:
        return _run(
            poll_args,
            out,
            environ=environ,
            transport=transport,
            sleep=sleep,
            guard_connector=guard_connector,
            clock=clock,
        )

    return cli_serve.run_serve(
        every_minutes=config.schedule.every_minutes,
        state_dir=args.state_dir,
        poll=poll,
        clock=clock,
        wait=wait,
    )


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
    guard_connector: GuardConnector | None = None,
    serve_clock: Clock | None = None,
    serve_wait: Callable[[float], object] | None = None,
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
            guard_connector=guard_connector,
        )

    if args.command == "serve":
        return _serve(
            args,
            out,
            environ=environ if environ is not None else os.environ,
            transport=transport,
            sleep=sleep,
            guard_connector=guard_connector,
            clock=serve_clock if serve_clock is not None else system_clock,
            wait=serve_wait,
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

    if args.command == "doctor":
        return cli_doctor.run_doctor(
            args,
            out=out,
            environ=environ if environ is not None else os.environ,
            transport=transport if transport is not None else UrllibTransport(),
            guard_connector=guard_connector or (lambda d: open_guard(d, clock=system_clock)),
            clock=system_clock,
        )

    if args.command == "probe":
        return cli_probe.run_probe(
            args,
            out=out,
            guard_connector=guard_connector or (lambda d: open_guard(d, clock=system_clock)),
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
