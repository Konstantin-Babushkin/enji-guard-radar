"""`doctor` verdicts: what the shell found out in, one PASS/FAIL/WARN/SKIP line per check out.

Core module. Every function is a pure map from plain facts to a CheckResult. Details are made of
status words, ids, variable names and fixed texts only: never a token, a URL or a Telegram
description, so the report is safe to paste into a ticket.
"""

import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from enji_guard_radar.config import RadarConfig
from enji_guard_radar.guard import GUARD_READ_TOOLS
from enji_guard_radar.guard_auth import AUTH_COMMAND, AUTH_FAILURE_TEXT, GuardCredentials
from enji_guard_radar.ledger import Corrupt, Loaded, Missing, StateLoad
from enji_guard_radar.ports import GuardAuthError, GuardCallError, GuardFailure

ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_OPS_CHAT_ID = "TELEGRAM_OPS_CHAT_ID"

NAME_SETTINGS = "settings"
NAME_OPS_ID = "ops chat id"
NAME_CONFIG = "config"
NAME_STATE = "state"
NAME_UNEVALUATED = "unevaluated projects"
NAME_CREDENTIALS = "guard login"
NAME_TOKEN = "telegram token"
NAME_GROUP = "group chat"
NAME_GROUP_PERMISSIONS = "group permissions"
NAME_OPS = "ops chat"
NAME_GUARD = "guard"
NAME_TIMER = "timer"

LABEL_GROUP = "the group chat"
LABEL_OPS = "the ops chat"

SKIP_NEEDS_TOKEN = "needs a valid bot token"
SKIP_NEEDS_MEMBERSHIP = "needs bot membership in the group chat"
CREDENTIAL_MODE = 0o600
MAX_LISTED_IDS = 10

HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404

MEMBER_OK = frozenset({"creator", "administrator", "member"})
ADMIN_STATUSES = frozenset({"creator", "administrator"})
GONE_STATUSES = frozenset({"left", "kicked"})
KNOWN_STATUSES = MEMBER_OK | GONE_STATUSES | {"restricted"}


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"
    SKIP = "SKIP"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class DoctorReport:
    results: tuple[CheckResult, ...] = field(default_factory=tuple)

    def count(self, status: CheckStatus) -> int:
        return sum(1 for result in self.results if result.status is status)

    @property
    def exit_code(self) -> int:
        return 1 if self.count(CheckStatus.FAIL) else 0


def _result(name: str, status: CheckStatus, detail: str) -> CheckResult:
    return CheckResult(name=name, status=status, detail=detail)


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


# --- facts the shell gathers -------------------------------------------------------------------


@dataclass(frozen=True)
class ApiAnswer:
    """A Telegram answer. `description` is only matched against, never printed."""

    ok: bool
    status: int
    error_code: int | None = None
    migrate_to_chat_id: int | None = None
    description: str | None = None
    result: Mapping[str, object] | None = None

    @property
    def code(self) -> int:
        return self.error_code if self.error_code is not None else self.status


@dataclass(frozen=True)
class ApiUnreachable:
    kind: Literal["timeout", "network"]


TelegramFact = ApiAnswer | ApiUnreachable


@dataclass(frozen=True)
class CredentialFacts:
    exists: bool
    mode: int | None = None
    credentials: GuardCredentials | None = None
    load_error: str | None = None


@dataclass(frozen=True)
class GuardFacts:
    failure: GuardCallError | None = None
    tool_names: frozenset[str] = frozenset()
    project_count: int | None = None
    bad_data: bool = False


# --- settings, config, state, credentials ------------------------------------------------------


def verdict_settings(
    *, has_token: bool, chat_id: str | None, ops_chat_id: str | None
) -> tuple[CheckResult, CheckResult]:
    missing = [
        name
        for name, present in ((ENV_BOT_TOKEN, has_token), (ENV_CHAT_ID, chat_id is not None))
        if not present
    ]
    if missing:
        settings = _result(NAME_SETTINGS, CheckStatus.FAIL, f"{', '.join(missing)} not set")
    else:
        settings = _result(NAME_SETTINGS, CheckStatus.PASS, "bot token and group chat id are set")
    if ops_chat_id is None:
        ops = _result(
            NAME_OPS_ID,
            CheckStatus.FAIL,
            f"{ENV_OPS_CHAT_ID} is not set: outage messages cannot be delivered (OPS-02)",
        )
    else:
        ops = _result(NAME_OPS_ID, CheckStatus.PASS, "ops chat id is set")
    return settings, ops


def verdict_config(*, error: str | None, override_count: int) -> CheckResult:
    if error is not None:
        return _result(NAME_CONFIG, CheckStatus.FAIL, f"config invalid: {error}")
    return _result(
        NAME_CONFIG, CheckStatus.PASS, f"config loads ({override_count} project entries)"
    )


def verdict_state(load: StateLoad) -> CheckResult:
    if isinstance(load, Corrupt):
        return _result(
            NAME_STATE,
            CheckStatus.FAIL,
            f"state.json is corrupt: {load.reason}; the radar halts until it is fixed",
        )
    if isinstance(load, Missing):
        return _result(NAME_STATE, CheckStatus.PASS, "no state yet; the first run baselines")
    last = load.state.health.last_success_at
    polled = _utc(last) if last is not None else "never"
    return _result(
        NAME_STATE, CheckStatus.PASS, f"state.json loads (last successful Guard poll: {polled})"
    )


def verdict_unevaluated_projects(load: StateLoad, config: RadarConfig | None) -> CheckResult:
    """Active projects the last successful poll did not evaluate. Never FAIL (Open Question 9)."""
    name = NAME_UNEVALUATED
    if not isinstance(load, Loaded):
        return _result(name, CheckStatus.SKIP, "needs a readable state.json")
    if config is None:
        return _result(name, CheckStatus.SKIP, "needs a readable config")
    last_success = load.state.health.last_success_at
    if last_success is None:
        return _result(name, CheckStatus.SKIP, "no successful Guard poll yet")
    missed = sorted(
        record.project_id
        for record in load.state.projects
        if config.mode_for(record.project_id) == "active"
        and record.last_evaluated_at < last_success
    )
    if not missed:
        return _result(
            name, CheckStatus.PASS, "every active project was evaluated in the last successful poll"
        )
    listed = ", ".join(missed[:MAX_LISTED_IDS])
    if len(missed) > MAX_LISTED_IDS:
        listed += f", and {len(missed) - MAX_LISTED_IDS} more"
    return _result(
        name,
        CheckStatus.WARN,
        f"{len(missed)} active project(s) not evaluated in the last successful poll "
        f"(Guard failed for them, or they were removed): {listed}",
    )


def verdict_credentials(facts: CredentialFacts, *, path: Path, now: datetime) -> CheckResult:
    name = NAME_CREDENTIALS
    if not facts.exists:
        return _result(name, CheckStatus.FAIL, f"no Guard login stored: run `{AUTH_COMMAND}`")
    if facts.mode is not None and stat.S_IMODE(facts.mode) != CREDENTIAL_MODE:
        mode = f"{stat.S_IMODE(facts.mode):04o}"
        return _result(
            name, CheckStatus.FAIL, f"credential file is mode {mode}; run chmod 600 {path}"
        )
    if facts.credentials is None:
        reason = facts.load_error or "credential file cannot be read"
        return _result(name, CheckStatus.FAIL, reason)
    credentials = facts.credentials
    if credentials.expires_at <= now:
        access = "access token expired, refreshed on first use"
    else:
        access = f"access token valid until {_utc(credentials.expires_at)}"
    return _result(name, CheckStatus.PASS, f"login stored (scope {credentials.scope}, {access})")


# --- Telegram ----------------------------------------------------------------------------------


def _unreachable(name: str, fact: ApiUnreachable) -> CheckResult:
    return _result(name, CheckStatus.FAIL, f"Telegram unreachable ({fact.kind})")


def bot_id(fact: TelegramFact) -> int | None:
    """The bot's own user id when getMe answered with a bot, else None."""
    if not isinstance(fact, ApiAnswer) or not fact.ok or fact.result is None:
        return None
    identity = fact.result.get("id")
    if fact.result.get("is_bot") is not True:
        return None
    return identity if isinstance(identity, int) and not isinstance(identity, bool) else None


def verdict_get_me(fact: TelegramFact) -> CheckResult:
    name = NAME_TOKEN
    if isinstance(fact, ApiUnreachable):
        return _unreachable(name, fact)
    if fact.ok:
        if bot_id(fact) is None:
            return _result(
                name,
                CheckStatus.FAIL,
                f"Telegram getMe did not describe a bot; check {ENV_BOT_TOKEN}",
            )
        username = fact.result.get("username") if fact.result is not None else None
        label = f"@{username}" if isinstance(username, str) else "(no username)"
        return _result(name, CheckStatus.PASS, f"bot {label}")
    if fact.code in (HTTP_UNAUTHORIZED, HTTP_NOT_FOUND):
        return _result(
            name, CheckStatus.FAIL, f"Telegram rejected the bot token; check {ENV_BOT_TOKEN}"
        )
    return _result(name, CheckStatus.FAIL, f"Telegram getMe failed (HTTP {fact.code})")


def member_status(fact: TelegramFact) -> str | None:
    if not isinstance(fact, ApiAnswer) or not fact.ok or fact.result is None:
        return None
    status = fact.result.get("status")
    return status if isinstance(status, str) else None


def verdict_chat_member(fact: TelegramFact, *, name: str, label: str, variable: str) -> CheckResult:
    if isinstance(fact, ApiUnreachable):
        return _unreachable(name, fact)
    if not fact.ok:
        return _result(name, CheckStatus.FAIL, _member_error(fact, label, variable))
    status = member_status(fact)
    if status is None or status not in KNOWN_STATUSES:
        shown = status if status is not None else "missing"
        return _result(name, CheckStatus.FAIL, f"unknown bot status {shown!r} in {label}")
    if status in MEMBER_OK:
        return _result(name, CheckStatus.PASS, f"bot is {status} in {label}")
    if status == "restricted":
        details = fact.result or {}
        if details.get("is_member") is True and details.get("can_send_messages") is True:
            return _result(name, CheckStatus.PASS, f"bot is restricted in {label} but can post")
        return _result(name, CheckStatus.FAIL, f"bot is restricted and cannot post in {label}")
    return _result(name, CheckStatus.FAIL, f"bot is not in {label} (status {status}); add it")


def _member_error(fact: ApiAnswer, label: str, variable: str) -> str:
    if fact.migrate_to_chat_id is not None:
        return f"{label} was upgraded to a supergroup; set {variable}={fact.migrate_to_chat_id}"
    if fact.code == HTTP_BAD_REQUEST:
        if "chat not found" in (fact.description or "").lower():
            return f"chat not found; check {variable}"
        return f"Telegram rejected the request for {label} (HTTP 400); check {variable}"
    if fact.code == HTTP_FORBIDDEN:
        return f"Telegram refused access to {label}"
    if fact.code in (HTTP_UNAUTHORIZED, HTTP_NOT_FOUND):
        return f"Telegram rejected the bot token; check {ENV_BOT_TOKEN}"
    return f"Telegram getChatMember failed for {label} (HTTP {fact.code})"


def verdict_chat_permissions(
    fact: TelegramFact, *, bot_status: str | None, label: str
) -> CheckResult:
    name = NAME_GROUP_PERMISSIONS
    if isinstance(fact, ApiUnreachable) or not fact.ok or fact.result is None:
        return _result(name, CheckStatus.WARN, f"could not read the default permissions of {label}")
    permissions = fact.result.get("permissions")
    allowed = permissions.get("can_send_messages", True) if isinstance(permissions, dict) else True
    if allowed is False and bot_status not in ADMIN_STATUSES:
        return _result(
            name,
            CheckStatus.WARN,
            f"default member permissions do not allow posting in {label}; "
            "make the bot an administrator or allow posting",
        )
    return _result(name, CheckStatus.PASS, f"the bot can post in {label}")


# --- Guard -------------------------------------------------------------------------------------


def verdict_guard(facts: GuardFacts) -> CheckResult:
    name = NAME_GUARD
    failure = facts.failure
    if failure is not None:
        if isinstance(failure, GuardAuthError):
            return _result(name, CheckStatus.FAIL, AUTH_FAILURE_TEXT[failure.kind])
        if failure.cause is GuardFailure.DATA:
            return _bad_data()
        return _result(name, CheckStatus.FAIL, f"Guard unreachable: {failure}")
    missing = sorted(GUARD_READ_TOOLS - facts.tool_names)
    if missing:
        return _result(
            name, CheckStatus.FAIL, f"Guard does not list the read tools: {', '.join(missing)}"
        )
    if facts.bad_data or facts.project_count is None:
        return _bad_data()
    return _result(
        name,
        CheckStatus.PASS,
        "connected with the radar's own login; read tools present; "
        f"{facts.project_count} project(s) on the first page",
    )


def _bad_data() -> CheckResult:
    return _result(
        NAME_GUARD, CheckStatus.FAIL, "Guard answered list_projects with unexpected data"
    )


# --- timer and output --------------------------------------------------------------------------


def timer_hint() -> CheckResult:
    return _result(
        NAME_TIMER,
        CheckStatus.SKIP,
        "a stopped timer cannot be seen from inside the radar; check "
        "`systemctl list-timers enji-guard-radar.timer` or the cron log",
    )


def skip(name: str, detail: str) -> CheckResult:
    return _result(name, CheckStatus.SKIP, detail)


def format_doctor(report: DoctorReport) -> str:
    lines = [f"{r.status.value:<4}  {r.name}: {r.detail}" for r in report.results]
    lines.append(
        f"doctor: {report.count(CheckStatus.PASS)} pass, {report.count(CheckStatus.FAIL)} fail, "
        f"{report.count(CheckStatus.WARN)} warn, {report.count(CheckStatus.SKIP)} skip"
    )
    return "\n".join(lines) + "\n"
