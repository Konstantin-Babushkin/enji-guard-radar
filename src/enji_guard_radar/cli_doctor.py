"""The `doctor` command: gathers facts about this host and prints the verdicts of `doctor.py`.

Read-only by construction: it never sends a Telegram message, never writes state.json and prints
no secret. It may refresh the Guard login (through the credential store and its lock), exactly as
a run would.
"""

import argparse
import logging
import shutil
import stat
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from pydantic import SecretStr

from enji_guard_radar import doctor
from enji_guard_radar.config import ConfigError, RadarConfig, parse_config
from enji_guard_radar.doctor import (
    ApiAnswer,
    ApiUnreachable,
    CheckResult,
    CheckStatus,
    CredentialFacts,
    DoctorReport,
    GuardFacts,
    SheetFacts,
    TelegramFact,
    TriageFacts,
)
from enji_guard_radar.guard import GUARD_READ_TOOLS, GuardDataError, parse_projects_page
from enji_guard_radar.guard_credentials import CredentialStore
from enji_guard_radar.ledger import Loaded, StateLoad
from enji_guard_radar.ports import (
    Clock,
    GuardAuthError,
    GuardCallError,
    GuardConnector,
    SheetReader,
    SheetUnavailable,
)
from enji_guard_radar.secret_guard import configure_logging, redact
from enji_guard_radar.settings import (
    SettingsError,
    known_secrets,
    load_claude_token,
    load_settings,
    load_sheet_key,
)
from enji_guard_radar.sheet_client import SheetsHttp, key_secrets, make_sheet_reader
from enji_guard_radar.sheet_settings import SheetInvalid, apply_sheet
from enji_guard_radar.sheet_store import FileSheetStore
from enji_guard_radar.state_store import FileStateStore
from enji_guard_radar.telegram import (
    HttpTransport,
    TransportFailure,
    call_method,
    chat_payload_id,
)
from enji_guard_radar.triage_cost_store import FileTriageCostStore

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "state"
DEFAULT_ENV_FILE = ".env"


def add_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "doctor", help="check Guard, Telegram and local setup; changes nothing"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(DEFAULT_STATE_DIR),
        metavar="DIR",
        help=f"directory with state.json and guard-auth.json (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"dotenv file with Telegram settings (default: {DEFAULT_ENV_FILE}; may be missing)",
    )
    parser.add_argument(
        "--config", type=Path, metavar="PATH", help="radar config file (paused/excluded projects)"
    )


def _read_config(path: Path) -> RadarConfig:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        message = f"config file not found: {path}"
        raise ConfigError(message) from None
    except (OSError, UnicodeDecodeError):
        message = f"cannot read config file {path}"
        raise ConfigError(message) from None
    return parse_config(text, source=str(path))


def _ask(
    token: SecretStr, method: str, payload: Mapping[str, object], transport: HttpTransport
) -> TelegramFact:
    try:
        result = call_method(token, method, payload, transport)
    except TransportFailure as failure:
        return ApiUnreachable(failure.kind)
    return ApiAnswer(
        ok=result.ok,
        status=result.status,
        error_code=result.error_code,
        migrate_to_chat_id=result.migrate_to_chat_id,
        description=result.description,
        result=result.result,
    )


def _credential_facts(store: CredentialStore) -> CredentialFacts:
    try:
        mode = stat.S_IMODE(store.path.stat().st_mode)
    except FileNotFoundError:
        return CredentialFacts(exists=False)
    except OSError:
        return CredentialFacts(
            exists=True, load_error="credential file cannot be read (permission or I/O error)"
        )
    try:
        return CredentialFacts(exists=True, mode=mode, credentials=store.load())
    except GuardAuthError as error:
        return CredentialFacts(exists=True, mode=mode, load_error=str(error))


def _guard_facts(connector: GuardConnector, state_dir: Path) -> GuardFacts:
    try:
        with connector(state_dir) as session:
            names = session.tool_names()
            if not names >= GUARD_READ_TOOLS:
                return GuardFacts(tool_names=names)
            try:
                page = parse_projects_page(session.call("list_projects", {}))
            except GuardDataError:
                return GuardFacts(tool_names=names, bad_data=True)
            return GuardFacts(tool_names=names, project_count=len(page.items))
    except GuardCallError as error:
        return GuardFacts(failure=error)


def _sheet_facts(
    config: RadarConfig, reader: SheetReader, state_dir: Path, now: datetime
) -> SheetFacts:
    """Facts only: the sheet is read, the stored copy is read, nothing is written."""
    record = FileSheetStore(state_dir).load()
    age = None if record.last_good is None else now - record.last_good.fetched_at
    try:
        data = reader.read()
    except SheetUnavailable as error:
        return SheetFacts(unreachable=error.cause.value, copy_age=age)
    try:
        apply_sheet(config, data)
    except SheetInvalid as error:
        return SheetFacts(invalid=str(error), copy_age=age)
    return SheetFacts(copy_age=age)


def _triage_facts(
    config: RadarConfig,
    *,
    token: SecretStr | None,
    state_dir: Path,
    now: datetime,
) -> TriageFacts:
    """Facts only: no Claude call; the cost file is read, never written."""
    today_calls = today_failures = 0
    if config.triage.enabled:
        today = now.astimezone(UTC).date()
        for day in FileTriageCostStore(state_dir).load():
            if day.day == today:
                today_calls, today_failures = day.calls, day.failures
    return TriageFacts(
        enabled=config.triage.enabled,
        backend=config.triage.backend,
        binary_found=shutil.which(doctor.CLAUDE_BINARY) is not None,
        token_set=token is not None,
        today_calls=today_calls,
        today_failures=today_failures,
    )


def _telegram_checks(
    *,
    token: SecretStr | None,
    chat_id: str | None,
    ops_chat_id: str | None,
    transport: HttpTransport,
) -> list[CheckResult]:
    if token is None:
        return [
            doctor.skip(doctor.NAME_TOKEN, f"{doctor.ENV_BOT_TOKEN} is not set"),
            doctor.skip(doctor.NAME_GROUP, doctor.SKIP_NEEDS_TOKEN),
            doctor.skip(doctor.NAME_GROUP_PERMISSIONS, doctor.SKIP_NEEDS_TOKEN),
            doctor.skip(
                doctor.NAME_OPS,
                doctor.SKIP_NEEDS_TOKEN if ops_chat_id is not None else "no ops chat id",
            ),
        ]
    get_me = _ask(token, "getMe", {}, transport)
    token_check = doctor.verdict_get_me(get_me)
    bot = doctor.bot_id(get_me)
    if token_check.status is not CheckStatus.PASS or bot is None:
        return [
            token_check,
            doctor.skip(doctor.NAME_GROUP, doctor.SKIP_NEEDS_TOKEN),
            doctor.skip(doctor.NAME_GROUP_PERMISSIONS, doctor.SKIP_NEEDS_TOKEN),
            doctor.skip(doctor.NAME_OPS, doctor.SKIP_NEEDS_TOKEN),
        ]
    checks = [token_check]
    if chat_id is None:
        missing = f"{doctor.ENV_CHAT_ID} is not set"
        checks += [
            doctor.skip(doctor.NAME_GROUP, missing),
            doctor.skip(doctor.NAME_GROUP_PERMISSIONS, missing),
        ]
    else:
        target = chat_payload_id(chat_id)
        member = _ask(token, "getChatMember", {"chat_id": target, "user_id": bot}, transport)
        membership = doctor.verdict_chat_member(
            member, name=doctor.NAME_GROUP, label=doctor.LABEL_GROUP, variable=doctor.ENV_CHAT_ID
        )
        checks.append(membership)
        if membership.status is CheckStatus.PASS:
            chat = _ask(token, "getChat", {"chat_id": target}, transport)
            checks.append(
                doctor.verdict_chat_permissions(
                    chat, bot_status=doctor.member_status(member), label=doctor.LABEL_GROUP
                )
            )
        else:
            checks.append(doctor.skip(doctor.NAME_GROUP_PERMISSIONS, doctor.SKIP_NEEDS_MEMBERSHIP))
    if ops_chat_id is None:
        checks.append(doctor.skip(doctor.NAME_OPS, "no ops chat id"))
    else:
        member = _ask(
            token,
            "getChatMember",
            {"chat_id": chat_payload_id(ops_chat_id), "user_id": bot},
            transport,
        )
        checks.append(
            doctor.verdict_chat_member(
                member,
                name=doctor.NAME_OPS,
                label=doctor.LABEL_OPS,
                variable=doctor.ENV_OPS_CHAT_ID,
            )
        )
    return checks


def run_doctor(
    args: argparse.Namespace,
    *,
    out: TextIO,
    environ: Mapping[str, str],
    transport: HttpTransport,
    guard_connector: GuardConnector,
    clock: Clock,
    sheet_reader: SheetReader | None = None,
    sheet_http: SheetsHttp | None = None,
) -> int:
    env_file: Path = args.env_file or Path(DEFAULT_ENV_FILE)
    try:
        settings = load_settings(environ, env_file)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    secrets = list(known_secrets(settings))
    configure_logging(level=logging.INFO, known_secrets=secrets, stream=sys.stderr)
    results: list[CheckResult] = list(
        doctor.verdict_settings(
            has_token=settings.bot_token is not None,
            chat_id=settings.chat_id,
            ops_chat_id=settings.ops_chat_id,
        )
    )

    config: RadarConfig | None = RadarConfig()
    if args.config is not None:
        try:
            config = _read_config(args.config)
        except ConfigError as error:
            config = None
            results.append(doctor.verdict_config(error=str(error), override_count=0))
        else:
            results.append(doctor.verdict_config(error=None, override_count=len(config.projects)))
            results.append(doctor.verdict_message_language(config))

    state_load: StateLoad = FileStateStore(args.state_dir).load()
    results.append(doctor.verdict_state(state_load))
    results.append(doctor.verdict_unevaluated_projects(state_load, config))
    results.append(doctor.verdict_digest(config, state_load))
    results.append(doctor.verdict_schedule(config, clock()))
    results.append(doctor.verdict_score_rules(config))
    results.append(doctor.verdict_high_priority(config))
    sheet_secrets: tuple[str, ...] = ()
    sheet_facts: SheetFacts | None = None
    if config is not None and config.sheet.enabled:
        sheet_key = load_sheet_key(environ, env_file)
        sheet_secrets = key_secrets(sheet_key)
        secrets.extend(sheet_secrets)
        reader = make_sheet_reader(
            config.sheet, sheet_key, injected=sheet_reader, http=sheet_http, clock=clock
        )
        sheet_facts = _sheet_facts(config, reader, args.state_dir, clock())
    results.append(doctor.verdict_sheet(config, sheet_facts))
    claude_token = load_claude_token(environ, env_file)
    if claude_token is not None:
        secrets.append(claude_token.get_secret_value())
    if config is None:
        results.append(doctor.skip(doctor.NAME_TRIAGE, doctor.SKIP_NEEDS_CONFIG))
    else:
        triage_facts = _triage_facts(
            config, token=claude_token, state_dir=args.state_dir, now=clock()
        )
        results.append(doctor.verdict_triage(triage_facts))
    if isinstance(state_load, Loaded):
        logger.info("doctor read state: %d project(s)", len(state_load.state.projects))

    store = CredentialStore(args.state_dir)
    facts = _credential_facts(store)
    if facts.credentials is not None:
        secrets += [
            facts.credentials.access_token.get_secret_value(),
            facts.credentials.refresh_token.get_secret_value(),
        ]
    results.append(doctor.verdict_credentials(facts, path=store.path, now=clock()))

    results += _telegram_checks(
        token=settings.bot_token,
        chat_id=settings.chat_id,
        ops_chat_id=settings.ops_chat_id,
        transport=transport,
    )

    if facts.exists:
        results.append(doctor.verdict_guard(_guard_facts(guard_connector, args.state_dir)))
    else:
        results.append(doctor.skip(doctor.NAME_GUARD, "no Guard login"))
    results.append(doctor.timer_hint())

    report = DoctorReport(tuple(results))
    for failed in (r for r in report.results if r.status is CheckStatus.FAIL):
        logger.warning("doctor check failed: %s", failed.name)
    logger.info(
        "doctor finished: %d pass, %d fail, %d warn, %d skip",
        report.count(CheckStatus.PASS),
        report.count(CheckStatus.FAIL),
        report.count(CheckStatus.WARN),
        report.count(CheckStatus.SKIP),
    )
    out.write(redact(doctor.format_doctor(report), secrets))
    return report.exit_code
