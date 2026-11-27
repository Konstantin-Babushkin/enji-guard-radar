"""`doctor`: the tracer through cli.main, the verdict tables and the broken-setup rows.

Everything is fake: FakeTransport for Telegram, a fake GuardSession for Guard, fake OAuth for the
refresh. No network, no real token.
"""

import io
import json
import logging
import re
import stat
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

import pytest
from fake_claude import install_fake_claude, with_path
from fake_oauth import (
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    ROTATED_ACCESS_TOKEN,
    ROTATED_REFRESH_TOKEN,
    FakeOAuthTransport,
    make_credentials,
    token_body,
)
from telegram_fakes import FAKE_TOKEN, FakeTransport, error_response

from enji_guard_radar import cli, doctor
from enji_guard_radar.config import ProjectSettings, RadarConfig, parse_config
from enji_guard_radar.doctor import (
    ApiAnswer,
    ApiUnreachable,
    CheckResult,
    CheckStatus,
    CredentialFacts,
    DoctorReport,
    GuardFacts,
    TriageFacts,
    format_doctor,
    verdict_chat_member,
    verdict_chat_permissions,
    verdict_config,
    verdict_credentials,
    verdict_digest,
    verdict_get_me,
    verdict_guard,
    verdict_high_priority,
    verdict_schedule,
    verdict_score_rules,
    verdict_settings,
    verdict_state,
    verdict_triage,
    verdict_unevaluated_projects,
)
from enji_guard_radar.guard import GUARD_READ_TOOLS
from enji_guard_radar.guard_auth import AUTH_FAILURE_TEXT
from enji_guard_radar.guard_credentials import CredentialStore, RefreshingTokenSource
from enji_guard_radar.ledger import (
    Corrupt,
    DigestRecord,
    Loaded,
    Missing,
    PollHealth,
    ProjectRecord,
    State,
)
from enji_guard_radar.ports import (
    AuthFailureKind,
    GuardAuthError,
    GuardCallError,
    GuardFailure,
    GuardSession,
)
from enji_guard_radar.settings import ENV_CLAUDE_TOKEN
from enji_guard_radar.state_store import FileStateStore
from enji_guard_radar.telegram import HttpResponse, TransportFailure
from enji_guard_radar.triage_cost import CostDay, serialize_days

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
GROUP = "-1001234567890"
OPS = "-1009990001"
FIXTURE_PROJECTS = Path(__file__).resolve().parents[1] / "fixtures" / "guard" / "list_projects.json"
PATH = Path("state/guard-auth.json")


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """`doctor` installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


# --- fakes -------------------------------------------------------------------------------------


def telegram_ok(result: object) -> HttpResponse:
    return HttpResponse(status=200, body=json.dumps({"ok": True, "result": result}).encode())


def get_me_ok() -> HttpResponse:
    return telegram_ok({"id": 777, "is_bot": True, "username": "radar_bot"})


def member(status: str, **extra: object) -> HttpResponse:
    return telegram_ok({"status": status, "user": {"id": 777}, **extra})


def chat(can_send: bool | None = True) -> HttpResponse:
    permissions = {} if can_send is None else {"can_send_messages": can_send}
    return telegram_ok({"id": -1001234567890, "type": "supergroup", "permissions": permissions})


class FakeGuardSession:
    """Implements the GuardSession protocol with no network."""

    def __init__(
        self,
        *,
        tools: frozenset[str] = GUARD_READ_TOOLS,
        projects: Mapping[str, object] | None = None,
        enter: Callable[[], object] | None = None,
        call_error: Exception | None = None,
    ) -> None:
        self.tools = tools
        self.projects = (
            projects
            if projects is not None
            else json.loads(FIXTURE_PROJECTS.read_text(encoding="utf-8"))
        )
        self.enter = enter
        self.call_error = call_error
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.closed = False

    def __enter__(self) -> Self:
        if self.enter is not None:
            self.enter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def tool_names(self) -> frozenset[str]:
        return self.tools

    def tool_arguments(self) -> Mapping[str, tuple[str, ...]]:
        return dict.fromkeys(self.tools, ())

    def result_shapes(self) -> Mapping[str, int]:
        return {"structured": len(self.calls)}

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append((tool, dict(args)))
        if self.call_error is not None:
            raise self.call_error
        return self.projects


def connector_for(session: GuardSession) -> Callable[[Path], GuardSession]:
    return lambda _directory: session


def never_connect(_directory: Path) -> GuardSession:
    message = "the Guard connector must not be called"
    raise AssertionError(message)


def environment(**overrides: str | None) -> dict[str, str]:
    base: dict[str, str | None] = {
        "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
        "TELEGRAM_CHAT_ID": GROUP,
        "TELEGRAM_OPS_CHAT_ID": OPS,
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def save_credentials(directory: Path, *, expires_in: timedelta = timedelta(hours=1)) -> Path:
    store = CredentialStore(directory)
    store.save(make_credentials(expires_at=NOW + expires_in, obtained_at=NOW))
    return store.path


def save_state(directory: Path, state: State | None = None) -> Path:
    store = FileStateStore(directory)
    store.save(state if state is not None else State.empty())
    return store.state_path


def run_doctor(
    directory: Path,
    *,
    environ: dict[str, str] | None = None,
    transport: FakeTransport | None = None,
    session: GuardSession | None = None,
    extra: list[str] | None = None,
) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(
        [
            "doctor",
            "--state-dir",
            str(directory),
            "--env-file",
            str(directory / "none.env"),
            *(extra or []),
        ],
        environ=environ if environ is not None else environment(),
        transport=transport if transport is not None else FakeTransport([]),
        guard_connector=connector_for(session) if session is not None else never_connect,
        stdout=out,
    )
    return code, out.getvalue()


def line(output: str, name: str) -> str:
    matches = [text for text in output.splitlines() if text[6:].startswith(f"{name}:")]
    assert len(matches) == 1, output
    return matches[0]


def healthy_transport() -> FakeTransport:
    return FakeTransport([get_me_ok(), member("member"), chat(True), member("administrator")])


# --- tracer ------------------------------------------------------------------------------------


def test_doctor_passes_on_a_healthy_setup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    state_path = save_state(tmp_path)
    save_credentials(tmp_path)
    before = state_path.read_bytes()
    transport = healthy_transport()
    session = FakeGuardSession()

    code, out = run_doctor(tmp_path, transport=transport, session=session)

    assert code == 0, out
    for name in (
        "settings",
        "ops chat id",
        "state",
        "guard login",
        "telegram token",
        "group chat",
        "group permissions",
        "ops chat",
        "guard",
    ):
        assert line(out, name).startswith("PASS"), out
    assert line(out, "unevaluated projects").startswith("SKIP")
    assert line(out, "weekly digest").startswith("PASS"), out
    assert "Wednesday 05:00 UTC" in line(out, "weekly digest")
    assert "24" in line(out, "weekly digest")
    assert re.fullmatch(
        r"PASS  schedule: every 180 min from 00:00 UTC; next slot "
        r"\d{4}-\d{2}-\d{2} (00|03|06|09|12|15|18|21):00 UTC",
        line(out, "schedule"),
    ), out
    assert line(out, "score rules").startswith("PASS"), out
    assert "off" in line(out, "score rules")
    assert line(out, "high priority types").startswith("PASS"), out
    assert "all audit types are high priority" in line(out, "high priority types")
    assert line(out, "timer").startswith("SKIP")
    assert line(out, "triage") == "SKIP  triage: off"
    assert line(out, "settings sheet").startswith("SKIP"), out
    assert out.rstrip().endswith("doctor: 13 pass, 0 fail, 0 warn, 4 skip")
    urls = [url.rsplit("/", 1)[1] for url, _ in transport.requests]
    assert urls == ["getMe", "getChatMember", "getChat", "getChatMember"]
    payloads = [payload for _, payload in transport.requests]
    assert payloads[1] == {"chat_id": int(GROUP), "user_id": 777}
    assert payloads[3] == {"chat_id": int(OPS), "user_id": 777}
    assert session.calls == [("list_projects", {})]
    assert session.closed
    assert state_path.read_bytes() == before
    captured = capsys.readouterr()
    for text in (out, captured.out, captured.err, caplog.text):
        assert FAKE_TOKEN not in text
        assert FAKE_ACCESS_TOKEN not in text
        assert "api.telegram.org" not in text


# --- verdict tables ----------------------------------------------------------------------------


def answer(
    *,
    ok: bool = True,
    status: int = 200,
    error_code: int | None = None,
    migrate: int | None = None,
    description: str | None = None,
    result: Mapping[str, object] | None = None,
) -> ApiAnswer:
    return ApiAnswer(
        ok=ok,
        status=status,
        error_code=error_code,
        migrate_to_chat_id=migrate,
        description=description,
        result=result,
    )


GET_ME_ROWS = [
    (answer(result={"id": 7, "is_bot": True, "username": "radar_bot"}), "PASS", "bot @radar_bot"),
    (
        answer(ok=False, status=401, error_code=401),
        "FAIL",
        "Telegram rejected the bot token; check TELEGRAM_BOT_TOKEN",
    ),
    (
        answer(ok=False, status=404, error_code=404),
        "FAIL",
        "Telegram rejected the bot token; check TELEGRAM_BOT_TOKEN",
    ),
    (answer(ok=False, status=500, error_code=500), "FAIL", "Telegram getMe failed (HTTP 500)"),
    (ApiUnreachable("timeout"), "FAIL", "Telegram unreachable (timeout)"),
    (ApiUnreachable("network"), "FAIL", "Telegram unreachable (network)"),
    (answer(result={"id": 7, "is_bot": False}), "FAIL", "did not describe a bot"),
    (answer(result=None), "FAIL", "did not describe a bot"),
]


@pytest.mark.parametrize(("fact", "status", "text"), GET_ME_ROWS)
def test_get_me_verdicts(fact: ApiAnswer | ApiUnreachable, status: str, text: str) -> None:
    result = verdict_get_me(fact)
    assert result.status.value == status
    assert text in result.detail


def member_fact(status: str, **extra: object) -> ApiAnswer:
    return answer(result={"status": status, **extra})


MEMBER_ROWS = [
    (member_fact("creator"), "PASS", "bot is creator in the group chat"),
    (member_fact("administrator"), "PASS", "bot is administrator in the group chat"),
    (member_fact("member"), "PASS", "bot is member in the group chat"),
    (member_fact("restricted", is_member=True, can_send_messages=True), "PASS", "restricted"),
    (
        member_fact("restricted", is_member=True, can_send_messages=False),
        "FAIL",
        "bot is restricted and cannot post in the group chat",
    ),
    (
        member_fact("restricted", is_member=False, can_send_messages=True),
        "FAIL",
        "bot is restricted and cannot post in the group chat",
    ),
    (member_fact("left"), "FAIL", "bot is not in the group chat (status left); add it"),
    (member_fact("kicked"), "FAIL", "bot is not in the group chat (status kicked); add it"),
    (member_fact("wizard"), "FAIL", "wizard"),
    (
        answer(ok=False, status=400, error_code=400, migrate=-1009876543210),
        "FAIL",
        "TELEGRAM_CHAT_ID=-1009876543210",
    ),
    (
        answer(ok=False, status=400, error_code=400, description="Bad Request: chat not found"),
        "FAIL",
        "chat not found; check TELEGRAM_CHAT_ID",
    ),
    (
        answer(ok=False, status=400, error_code=400, description="Bad Request: other"),
        "FAIL",
        "(HTTP 400); check TELEGRAM_CHAT_ID",
    ),
    (
        answer(ok=False, status=403, error_code=403),
        "FAIL",
        "Telegram refused access to the group chat",
    ),
    (ApiUnreachable("network"), "FAIL", "Telegram unreachable (network)"),
]


@pytest.mark.parametrize(("fact", "status", "text"), MEMBER_ROWS)
def test_chat_member_verdicts(fact: ApiAnswer | ApiUnreachable, status: str, text: str) -> None:
    result = verdict_chat_member(
        fact, name="group chat", label="the group chat", variable="TELEGRAM_CHAT_ID"
    )
    assert result.status.value == status
    assert text in result.detail


def test_migrated_ops_chat_names_the_ops_variable() -> None:
    fact = answer(ok=False, status=400, error_code=400, migrate=-100555)
    result = verdict_chat_member(
        fact, name="ops chat", label="the ops chat", variable="TELEGRAM_OPS_CHAT_ID"
    )
    assert result.status is CheckStatus.FAIL
    assert "TELEGRAM_OPS_CHAT_ID=-100555" in result.detail


@pytest.mark.parametrize(
    ("can_send", "bot_status", "expected"),
    [
        (False, "member", "WARN"),
        (False, "administrator", "PASS"),
        (False, "creator", "PASS"),
        (True, "member", "PASS"),
        (None, "member", "PASS"),
    ],
)
def test_chat_permissions_verdicts(can_send: bool | None, bot_status: str, expected: str) -> None:
    permissions = {} if can_send is None else {"can_send_messages": can_send}
    fact = answer(result={"permissions": permissions})
    result = verdict_chat_permissions(fact, bot_status=bot_status, label="the group chat")
    assert result.status.value == expected


def test_chat_permissions_warn_text_and_unreadable_answers() -> None:
    fact = answer(result={"permissions": {"can_send_messages": False}})
    warn = verdict_chat_permissions(fact, bot_status="member", label="the group chat")
    assert "default member permissions do not allow posting in the group chat" in warn.detail
    assert "make the bot an administrator or allow posting" in warn.detail
    for unreadable in (ApiUnreachable("timeout"), answer(ok=False, status=500)):
        got = verdict_chat_permissions(unreadable, bot_status="member", label="the group chat")
        assert got.status is CheckStatus.WARN


@pytest.mark.parametrize("kind", list(AuthFailureKind))
def test_guard_auth_failures_use_the_auth_texts(kind: AuthFailureKind) -> None:
    result = verdict_guard(GuardFacts(failure=GuardAuthError(kind, "internal text")))
    assert result.status is CheckStatus.FAIL
    assert result.detail == AUTH_FAILURE_TEXT[kind]
    assert "enji-guard-radar auth" in result.detail


GUARD_ROWS = [
    (
        GuardFacts(failure=GuardCallError("Guard timed out", cause=GuardFailure.UNAVAILABLE)),
        "FAIL",
        "Guard unreachable: Guard timed out",
    ),
    (
        GuardFacts(failure=GuardCallError("tool error", cause=GuardFailure.DATA)),
        "FAIL",
        "Guard answered list_projects with unexpected data",
    ),
    (
        GuardFacts(tool_names=GUARD_READ_TOOLS - {"get_audit_report"}),
        "FAIL",
        "Guard does not list the read tools: get_audit_report",
    ),
    (
        GuardFacts(tool_names=GUARD_READ_TOOLS, bad_data=True),
        "FAIL",
        "Guard answered list_projects with unexpected data",
    ),
    (
        GuardFacts(tool_names=GUARD_READ_TOOLS, project_count=3),
        "PASS",
        "connected with the radar's own login; read tools present; 3 project(s) on the first page",
    ),
]


@pytest.mark.parametrize(("facts", "status", "text"), GUARD_ROWS)
def test_guard_verdicts(facts: GuardFacts, status: str, text: str) -> None:
    result = verdict_guard(facts)
    assert result.status.value == status
    assert text in result.detail


def test_credentials_verdicts(tmp_path: Path) -> None:
    valid = make_credentials(expires_at=NOW + timedelta(hours=1), obtained_at=NOW)
    expired = make_credentials(expires_at=NOW - timedelta(hours=1), obtained_at=NOW)
    path = tmp_path / "guard-auth.json"

    missing = verdict_credentials(CredentialFacts(exists=False), path=path, now=NOW)
    assert missing.status is CheckStatus.FAIL
    assert missing.detail == "no Guard login stored: run `enji-guard-radar auth`"

    loose = verdict_credentials(
        CredentialFacts(exists=True, mode=0o644, credentials=valid), path=path, now=NOW
    )
    assert loose.status is CheckStatus.FAIL
    assert loose.detail == f"credential file is mode 0644; run chmod 600 {path}"

    broken = verdict_credentials(
        CredentialFacts(exists=True, mode=0o600, load_error="file invalid: bad json"),
        path=path,
        now=NOW,
    )
    assert broken.status is CheckStatus.FAIL
    assert broken.detail == "file invalid: bad json"

    good = verdict_credentials(
        CredentialFacts(exists=True, mode=0o600, credentials=valid), path=path, now=NOW
    )
    assert good.status is CheckStatus.PASS
    assert "scope projects:read audits:read" in good.detail
    assert "valid until 2026-11-18 13:00 UTC" in good.detail
    assert FAKE_ACCESS_TOKEN not in good.detail

    refreshed = verdict_credentials(
        CredentialFacts(exists=True, mode=0o600, credentials=expired), path=path, now=NOW
    )
    assert refreshed.status is CheckStatus.PASS
    assert "refreshed on first use" in refreshed.detail


def test_state_verdicts() -> None:
    assert verdict_state(Missing()).detail == "no state yet; the first run baselines"
    corrupt = verdict_state(Corrupt("bad schema", backup_valid=False))
    assert corrupt.status is CheckStatus.FAIL
    assert corrupt.detail == "state.json is corrupt: bad schema; the radar halts until it is fixed"
    never = verdict_state(Loaded(State.empty()))
    assert never.status is CheckStatus.PASS
    assert "last successful Guard poll: never" in never.detail
    polled = verdict_state(Loaded(State(health=PollHealth(last_success_at=NOW))))
    assert "2026-11-18 12:00 UTC" in polled.detail


def test_settings_verdicts() -> None:
    settings, ops = verdict_settings(has_token=True, chat_id=GROUP, ops_chat_id=OPS)
    assert (settings.status, ops.status) == (CheckStatus.PASS, CheckStatus.PASS)
    settings, ops = verdict_settings(has_token=True, chat_id=GROUP, ops_chat_id=None)
    assert settings.status is CheckStatus.PASS
    assert ops.status is CheckStatus.FAIL
    assert ops.detail == (
        "TELEGRAM_OPS_CHAT_ID is not set: outage messages cannot be delivered (OPS-02)"
    )
    settings, _ = verdict_settings(has_token=False, chat_id=None, ops_chat_id=OPS)
    assert settings.status is CheckStatus.FAIL
    assert "TELEGRAM_BOT_TOKEN" in settings.detail
    assert "TELEGRAM_CHAT_ID" in settings.detail


def test_config_verdicts() -> None:
    assert verdict_config(error=None, override_count=2).status is CheckStatus.PASS
    broken = verdict_config(error="config file not found: x.yml", override_count=0)
    assert broken.status is CheckStatus.FAIL
    assert "config file not found" in broken.detail


@pytest.mark.parametrize(
    ("statuses", "code"),
    [
        ([CheckStatus.PASS, CheckStatus.WARN, CheckStatus.SKIP], 0),
        ([CheckStatus.PASS, CheckStatus.FAIL], 1),
        ([CheckStatus.FAIL, CheckStatus.FAIL, CheckStatus.WARN], 1),
        ([], 0),
    ],
)
def test_exit_code(statuses: list[CheckStatus], code: int) -> None:
    report = DoctorReport(tuple(CheckResult("x", status, "d") for status in statuses))
    assert report.exit_code == code


def test_format_doctor_lines_and_summary() -> None:
    report = DoctorReport(
        (
            CheckResult("one", CheckStatus.PASS, "fine"),
            CheckResult("two", CheckStatus.FAIL, "broken"),
            CheckResult("three", CheckStatus.SKIP, "later"),
        )
    )
    assert format_doctor(report) == (
        "PASS  one: fine\nFAIL  two: broken\nSKIP  three: later\n"
        "doctor: 1 pass, 1 fail, 0 warn, 1 skip\n"
    )


def record(project_id: str, evaluated: datetime) -> ProjectRecord:
    return ProjectRecord(
        project_id=project_id, first_seen_at=NOW - timedelta(days=9), last_evaluated_at=evaluated
    )


def polled_state(*records: ProjectRecord, last_success: datetime | None = NOW) -> Loaded:
    return Loaded(State(projects=tuple(records), health=PollHealth(last_success_at=last_success)))


def test_unevaluated_needs_a_readable_state() -> None:
    for load in (Missing(), Corrupt("bad", backup_valid=False)):
        result = verdict_unevaluated_projects(load, RadarConfig())
        assert result.status is CheckStatus.SKIP
        assert result.detail == "needs a readable state.json"


def test_unevaluated_without_a_successful_poll_is_skipped() -> None:
    result = verdict_unevaluated_projects(polled_state(last_success=None), RadarConfig())
    assert (result.status, result.detail) == (CheckStatus.SKIP, "no successful Guard poll yet")


def test_unevaluated_all_evaluated_passes() -> None:
    state = polled_state(record("p1", NOW), record("p2", NOW))
    result = verdict_unevaluated_projects(state, RadarConfig())
    assert result.status is CheckStatus.PASS
    assert result.detail == "every active project was evaluated in the last successful poll"


def test_unevaluated_one_project_warns_with_its_id() -> None:
    state = polled_state(record("p1", NOW), record("p2", NOW - timedelta(hours=3)))
    result = verdict_unevaluated_projects(state, RadarConfig())
    assert result.status is CheckStatus.WARN
    assert result.detail == (
        "1 active project(s) not evaluated in the last successful poll "
        "(Guard failed for them, or they were removed): p2"
    )


def test_unevaluated_ignores_paused_and_excluded_projects() -> None:
    config = RadarConfig(
        projects={"p1": ProjectSettings(mode="paused"), "p2": ProjectSettings(mode="excluded")}
    )
    old = NOW - timedelta(days=1)
    result = verdict_unevaluated_projects(
        polled_state(record("p1", old), record("p2", old)), config
    )
    assert result.status is CheckStatus.PASS


def test_unevaluated_lists_at_most_ten_sorted_ids() -> None:
    old = NOW - timedelta(days=1)
    state = polled_state(*(record(f"p{n:02d}", old) for n in reversed(range(13))))
    result = verdict_unevaluated_projects(state, RadarConfig())
    assert result.status is CheckStatus.WARN
    assert result.detail.startswith("13 active project(s) not evaluated")
    assert result.detail.endswith("p00, p01, p02, p03, p04, p05, p06, p07, p08, p09, and 3 more")


def test_unevaluated_needs_a_readable_config() -> None:
    result = verdict_unevaluated_projects(polled_state(record("p1", NOW)), None)
    assert (result.status, result.detail) == (CheckStatus.SKIP, "needs a readable config")


# --- broken setups through cli.main ------------------------------------------------------------


def statuses(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for text in output.splitlines():
        head, _, _rest = text.partition(": ")
        if text[:4] in {"PASS", "FAIL", "WARN", "SKIP"}:
            result[head[6:]] = text[:4]
    return result


def test_bad_token_skips_the_chat_checks_and_still_checks_guard(tmp_path: Path) -> None:
    save_credentials(tmp_path)
    transport = FakeTransport([error_response(401, 401, "Unauthorized")])
    session = FakeGuardSession()
    code, out = run_doctor(tmp_path, transport=transport, session=session)
    assert code == 1
    found = statuses(out)
    assert found["telegram token"] == "FAIL"
    for name in ("group chat", "group permissions", "ops chat"):
        assert found[name] == "SKIP"
        assert "needs a valid bot token" in line(out, name)
    assert found["guard"] == "PASS"
    assert len(transport.requests) == 1


def test_left_group_fails_only_the_group_line(tmp_path: Path) -> None:
    save_credentials(tmp_path)
    transport = FakeTransport([get_me_ok(), member("left"), member("member")])
    code, out = run_doctor(tmp_path, transport=transport, session=FakeGuardSession())
    assert code == 1
    found = statuses(out)
    assert [name for name, status in found.items() if status == "FAIL"] == ["group chat"]
    assert found["group permissions"] == "SKIP"
    assert found["ops chat"] == "PASS"
    assert [url.rsplit("/", 1)[1] for url, _ in transport.requests] == [
        "getMe",
        "getChatMember",
        "getChatMember",
    ]


def test_no_credentials_never_calls_guard(tmp_path: Path) -> None:
    code, out = run_doctor(tmp_path, transport=healthy_transport())
    assert code == 1
    assert line(out, "guard login") == (
        "FAIL  guard login: no Guard login stored: run `enji-guard-radar auth`"
    )
    assert line(out, "guard") == "SKIP  guard: no Guard login"


def test_loose_credential_mode_fails(tmp_path: Path) -> None:
    path = save_credentials(tmp_path)
    path.chmod(0o644)
    code, out = run_doctor(tmp_path, transport=healthy_transport(), session=FakeGuardSession())
    assert code == 1
    assert "credential file is mode 0644" in line(out, "guard login")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_unreadable_credential_file_fails_with_a_reason_only(tmp_path: Path) -> None:
    path = tmp_path / "guard-auth.json"
    path.write_text("{not json", encoding="utf-8")
    path.chmod(0o600)
    code, out = run_doctor(tmp_path, transport=healthy_transport(), session=FakeGuardSession())
    assert code == 1
    assert line(out, "guard login").startswith("FAIL")
    assert "{not json" not in out


def test_refresh_rotates_the_login_but_leaves_state_alone(tmp_path: Path) -> None:
    state_path = save_state(tmp_path)
    credential_path = save_credentials(tmp_path, expires_in=timedelta(minutes=-5))
    state_before = state_path.read_bytes()
    oauth = FakeOAuthTransport([token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)])
    source = RefreshingTokenSource(CredentialStore(tmp_path), oauth, lambda: NOW)
    session = FakeGuardSession(enter=lambda: source.access_token())

    code, out = run_doctor(tmp_path, transport=healthy_transport(), session=session)

    assert code == 0, out
    assert len(oauth.requests) == 1
    stored = credential_path.read_text(encoding="utf-8")
    assert ROTATED_ACCESS_TOKEN in stored
    assert ROTATED_REFRESH_TOKEN in stored
    assert FAKE_REFRESH_TOKEN not in stored
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
    assert state_path.read_bytes() == state_before
    assert ROTATED_ACCESS_TOKEN not in out
    assert ROTATED_REFRESH_TOKEN not in out


def test_guard_auth_failure_during_connect_names_the_auth_command(tmp_path: Path) -> None:
    save_credentials(tmp_path)

    def refuse() -> None:
        raise GuardAuthError(AuthFailureKind.AUTH_EXPIRED, "x")

    code, out = run_doctor(
        tmp_path, transport=healthy_transport(), session=FakeGuardSession(enter=refuse)
    )
    assert code == 1
    assert line(out, "guard") == f"FAIL  guard: {AUTH_FAILURE_TEXT[AuthFailureKind.AUTH_EXPIRED]}"


def test_unparseable_project_list_fails(tmp_path: Path) -> None:
    save_credentials(tmp_path)
    session = FakeGuardSession(projects={"unexpected": []})
    code, out = run_doctor(tmp_path, transport=healthy_transport(), session=session)
    assert code == 1
    assert "unexpected data" in line(out, "guard")


def test_missing_read_tool_skips_the_project_call(tmp_path: Path) -> None:
    save_credentials(tmp_path)
    session = FakeGuardSession(tools=GUARD_READ_TOOLS - {"get_audit_report"})
    code, out = run_doctor(tmp_path, transport=healthy_transport(), session=session)
    assert code == 1
    assert "get_audit_report" in line(out, "guard")
    assert session.calls == []


def test_corrupt_state_fails_and_is_left_alone(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_bytes(b"not json")
    save_credentials(tmp_path)
    code, out = run_doctor(tmp_path, transport=healthy_transport(), session=FakeGuardSession())
    assert code == 1
    assert "state.json is corrupt: " in line(out, "state")
    assert line(out, "unevaluated projects").startswith("SKIP")
    assert state_path.read_bytes() == b"not json"
    assert not (tmp_path / "state.json.bak").exists()


def test_missing_ops_chat_fails_and_skips_its_membership(tmp_path: Path) -> None:
    save_credentials(tmp_path)
    transport = FakeTransport([get_me_ok(), member("member"), chat(True)])
    code, out = run_doctor(
        tmp_path,
        environ=environment(TELEGRAM_OPS_CHAT_ID=None),
        transport=transport,
        session=FakeGuardSession(),
    )
    assert code == 1
    assert "TELEGRAM_OPS_CHAT_ID is not set: outage messages cannot be delivered" in line(
        out, "ops chat id"
    )
    assert line(out, "ops chat").startswith("SKIP")


def test_transport_failure_on_get_me_fails_without_extra_requests(tmp_path: Path) -> None:
    save_credentials(tmp_path)
    transport = FakeTransport([TransportFailure("timeout", "request timed out")])
    code, out = run_doctor(tmp_path, transport=transport, session=FakeGuardSession())
    assert code == 1
    assert line(out, "telegram token") == "FAIL  telegram token: Telegram unreachable (timeout)"
    assert len(transport.requests) == 1


def test_default_permissions_warning_keeps_exit_zero(tmp_path: Path) -> None:
    save_credentials(tmp_path)
    transport = FakeTransport([get_me_ok(), member("member"), chat(False), member("member")])
    code, out = run_doctor(tmp_path, transport=transport, session=FakeGuardSession())
    assert code == 0
    assert line(out, "group permissions").startswith("WARN")


def test_unevaluated_projects_warns_through_the_cli_with_a_config_file(tmp_path: Path) -> None:
    old = NOW - timedelta(hours=6)
    save_state(
        tmp_path,
        State(
            projects=(record("p-active", old), record("p-paused", old)),
            health=PollHealth(last_success_at=NOW),
        ),
    )
    save_credentials(tmp_path)
    config = tmp_path / "radar.yml"
    config.write_text("projects:\n  p-paused:\n    mode: paused\n", encoding="utf-8")
    code, out = run_doctor(
        tmp_path,
        transport=healthy_transport(),
        session=FakeGuardSession(),
        extra=["--config", str(config)],
    )
    assert code == 0
    assert line(out, "config").startswith("PASS")
    assert line(out, "unevaluated projects").endswith(": p-active")


def test_missing_config_file_fails(tmp_path: Path) -> None:
    save_state(tmp_path)
    save_credentials(tmp_path)
    code, out = run_doctor(
        tmp_path,
        transport=healthy_transport(),
        session=FakeGuardSession(),
        extra=["--config", str(tmp_path / "missing.yml")],
    )
    assert code == 1
    assert "config invalid: config file not found" in line(out, "config")
    assert (
        line(out, "unevaluated projects") == "SKIP  unevaluated projects: needs a readable config"
    )


def snapshot(directory: Path) -> dict[str, tuple[str, object, int]]:
    """Every entry under `directory`: kind, content (bytes or symlink target) and mode."""
    entries: dict[str, tuple[str, object, int]] = {}
    for path in sorted(directory.rglob("*")):
        mode = stat.S_IMODE(path.lstat().st_mode)
        key = str(path.relative_to(directory))
        if path.is_symlink():
            entries[key] = ("symlink", str(path.readlink()), mode)
        elif path.is_dir():
            entries[key] = ("dir", None, mode)
        else:
            entries[key] = ("file", path.read_bytes(), mode)
    return entries


def unreadable_config(tmp_path: Path, kind: str) -> Path:
    path = tmp_path / "radar.yml"
    if kind == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"projects: \xff\xfe\n")
    return path


@pytest.mark.parametrize("kind", ["directory", "not-utf8"])
def test_unreadable_config_file_fails_with_a_reason_only(
    tmp_path: Path, kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    save_state(tmp_path)
    save_credentials(tmp_path)
    path = unreadable_config(tmp_path, kind)
    before = snapshot(tmp_path)
    code, out = run_doctor(
        tmp_path,
        transport=healthy_transport(),
        session=FakeGuardSession(),
        extra=["--config", str(path)],
    )
    assert code == 1
    assert line(out, "config") == f"FAIL  config: config invalid: cannot read config file {path}"
    assert (
        line(out, "unevaluated projects") == "SKIP  unevaluated projects: needs a readable config"
    )
    assert line(out, "triage").startswith("SKIP")
    assert snapshot(tmp_path) == before
    assert before["guard-auth.json"][2] == 0o600
    assert "Traceback" not in out + capsys.readouterr().err
    assert "UnicodeDecodeError" not in out
    assert "Errno" not in out


def test_unstattable_credential_file_fails_with_a_reason_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "guard-auth.json").symlink_to("guard-auth.json")
    before = snapshot(tmp_path)
    assert [(name, kind, target) for name, (kind, target, _) in before.items()] == [
        ("guard-auth.json", "symlink", "guard-auth.json")
    ]
    transport = healthy_transport()
    code, out = run_doctor(tmp_path, transport=transport, session=FakeGuardSession())
    assert code == 1
    assert line(out, "guard login") == (
        "FAIL  guard login: credential file cannot be read (permission or I/O error)"
    )
    assert line(out, "guard").startswith("PASS")
    assert snapshot(tmp_path) == before
    assert "Traceback" not in out + capsys.readouterr().err
    assert "Errno" not in out
    assert "symbolic links" not in out


def test_missing_bot_token_skips_every_telegram_line_and_makes_no_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save_credentials(tmp_path)
    transport = FakeTransport([])
    code, out = run_doctor(
        tmp_path,
        environ=environment(TELEGRAM_BOT_TOKEN=None),
        transport=transport,
        session=FakeGuardSession(),
    )
    assert code == 1
    assert line(out, "settings") == "FAIL  settings: TELEGRAM_BOT_TOKEN not set"
    assert line(out, "telegram token") == "SKIP  telegram token: TELEGRAM_BOT_TOKEN is not set"
    assert line(out, "group chat") == "SKIP  group chat: needs a valid bot token"
    assert line(out, "group permissions").startswith("SKIP")
    assert line(out, "ops chat") == "SKIP  ops chat: needs a valid bot token"
    assert transport.requests == []
    assert "Traceback" not in out + capsys.readouterr().err


def test_missing_bot_token_without_an_ops_chat_id_skips_the_ops_line_for_that(
    tmp_path: Path,
) -> None:
    transport = FakeTransport([])
    code, out = run_doctor(
        tmp_path,
        environ=environment(TELEGRAM_BOT_TOKEN=None, TELEGRAM_OPS_CHAT_ID=None),
        transport=transport,
    )
    assert code == 1
    assert line(out, "ops chat") == "SKIP  ops chat: no ops chat id"
    assert transport.requests == []


def test_missing_group_chat_id_skips_the_group_lines_after_a_good_get_me(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save_credentials(tmp_path)
    transport = FakeTransport([get_me_ok(), member("administrator")])
    code, out = run_doctor(
        tmp_path,
        environ=environment(TELEGRAM_CHAT_ID=None),
        transport=transport,
        session=FakeGuardSession(),
    )
    assert code == 1
    assert line(out, "settings") == "FAIL  settings: TELEGRAM_CHAT_ID not set"
    assert line(out, "telegram token").startswith("PASS")
    assert line(out, "group chat") == "SKIP  group chat: TELEGRAM_CHAT_ID is not set"
    assert line(out, "group permissions") == "SKIP  group permissions: TELEGRAM_CHAT_ID is not set"
    assert line(out, "ops chat").startswith("PASS")
    assert [url.rsplit("/", 1)[-1] for url, _ in transport.requests] == ["getMe", "getChatMember"]
    assert "Traceback" not in out + capsys.readouterr().err


def test_secrecy_across_failures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    save_credentials(tmp_path)
    leak = f"Unauthorized: bot{FAKE_TOKEN} token {FAKE_ACCESS_TOKEN}"
    scripts: list[list[HttpResponse | TransportFailure]] = [
        [error_response(401, 401, leak)],
        [get_me_ok(), error_response(403, 403, leak), error_response(400, 400, leak)],
        [TransportFailure("network", f"network error for {FAKE_TOKEN}")],
    ]
    for script in scripts:
        run_doctor(tmp_path, transport=FakeTransport(script), session=FakeGuardSession())
    leaky = GuardCallError(f"Guard said {FAKE_ACCESS_TOKEN}")
    failing = FakeGuardSession(call_error=leaky)
    _, out = run_doctor(tmp_path, transport=healthy_transport(), session=failing)
    captured = capsys.readouterr()
    for text in (out, captured.out, captured.err, caplog.text):
        assert FAKE_TOKEN not in text
        assert "api.telegram.org" not in text
    assert FAKE_ACCESS_TOKEN not in out


def test_no_send_and_no_state_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        message = "doctor must not write state"
        raise AssertionError(message)

    monkeypatch.setattr(FileStateStore, "save", refuse)
    save_credentials(tmp_path)
    transport = healthy_transport()
    code, _ = run_doctor(tmp_path, transport=transport, session=FakeGuardSession())
    assert code == 0
    assert all(not url.endswith("/sendMessage") for url, _ in transport.requests)
    assert not (tmp_path / "state.json").exists()


def test_invalid_chat_id_is_a_settings_error(tmp_path: Path) -> None:
    code, out = run_doctor(tmp_path, environ=environment(TELEGRAM_CHAT_ID="not a chat"))
    assert code == 2
    assert out == ""


# --- digest and score-rule lines ---------------------------------------------------------------


def config_of(text: str = "") -> RadarConfig:
    return parse_config(text, source="test")


def test_digest_verdicts_default_and_window() -> None:
    result = verdict_digest(RadarConfig(), Loaded(State.empty()))
    assert result.status is CheckStatus.PASS
    for part in ("Wednesday", "05:00 UTC", "24", "10", "none yet"):
        assert part in result.detail
    missing = verdict_digest(RadarConfig(), Missing())
    assert missing.status is CheckStatus.PASS
    assert "last digest" not in missing.detail
    assert verdict_digest(config_of("digest:\n  grace_hours: 3\n"), Missing()).status is (
        CheckStatus.PASS
    )
    for hours in (2, 1):
        warned = verdict_digest(config_of(f"digest:\n  grace_hours: {hours}\n"), Missing())
        assert warned.status is CheckStatus.WARN
        assert "digest.grace_hours" in warned.detail
    sunday = verdict_digest(config_of("digest:\n  weekday: sunday\n  hour_utc: 23\n"), Missing())
    assert "Sunday" in sunday.detail
    assert "23:00 UTC" in sunday.detail


def test_digest_verdicts_last_outcome_from_state() -> None:
    planned = NOW - timedelta(days=1)
    sent = Loaded(State(digest=DigestRecord(planned_for=planned, outcome="sent", sent_at=NOW)))
    assert "last digest: sent 2026-11-18 12:00 UTC" in verdict_digest(RadarConfig(), sent).detail
    skipped = Loaded(State(digest=DigestRecord(planned_for=planned, outcome="skipped")))
    skipped_detail = verdict_digest(RadarConfig(), skipped).detail
    assert "skipped 2026-11-17 12:00 UTC" in skipped_detail
    assert "none yet" in verdict_digest(RadarConfig(), Loaded(State.empty())).detail
    corrupt = verdict_digest(RadarConfig(), Corrupt("bad", backup_valid=False))
    assert corrupt.status is CheckStatus.PASS
    assert "last digest" not in corrupt.detail


def test_digest_verdicts_need_a_readable_config() -> None:
    result = verdict_digest(None, Missing())
    assert (result.status, result.detail) == (CheckStatus.SKIP, "needs a readable config")


def test_score_rule_verdicts_table() -> None:
    off = verdict_score_rules(RadarConfig())
    assert off.status is CheckStatus.PASS
    for part in ("off", "scores.ai_readiness_below", "scores.tests_below"):
        assert part in off.detail

    one = verdict_score_rules(config_of("scores:\n  ai_readiness_below: 40\n"))
    assert one.status is CheckStatus.PASS
    assert "ai-readiness below 40" in one.detail
    assert "tests off" in one.detail

    both = verdict_score_rules(config_of("scores:\n  ai_readiness_below: 40\n  tests_below: 60\n"))
    assert "below 40" in both.detail
    assert "below 60" in both.detail
    assert "0 project override(s)" in both.detail

    override = verdict_score_rules(config_of("projects:\n  p1:\n    tests_below: 50\n  p2: {}\n"))
    assert override.status is CheckStatus.PASS
    assert "1 project override(s)" in override.detail
    assert "score rules are off" not in override.detail

    zero = verdict_score_rules(config_of("scores:\n  ai_readiness_below: 40\n  tests_below: 0\n"))
    assert "tests off" in zero.detail

    skipped = verdict_score_rules(None)
    assert (skipped.status, skipped.detail) == (CheckStatus.SKIP, "needs a readable config")


def test_high_priority_verdicts_table() -> None:
    default = verdict_high_priority(RadarConfig())
    assert default.name == "high priority types"
    assert default.status is CheckStatus.PASS
    assert default.detail == (
        "all audit types are high priority (high.high_priority_audit_types is not set)"
    )
    one = verdict_high_priority(config_of("high: {high_priority_audit_types: [audit.security]}"))
    assert one.detail == "1 audit type is high priority; every other audit type is digest-only"
    nine = ", ".join(f"audit.type-{n}" for n in range(9))
    many = verdict_high_priority(config_of(f"high: {{high_priority_audit_types: [{nine}]}}"))
    assert many.status is CheckStatus.PASS
    assert many.detail == "9 audit types are high priority; every other audit type is digest-only"
    legacy = verdict_high_priority(config_of("high: {audit_types: [audit.security, audit.tests]}"))
    assert legacy.detail.startswith("2 audit types are high priority")
    skipped = verdict_high_priority(None)
    assert (skipped.status, skipped.detail) == (CheckStatus.SKIP, "needs a readable config")


def run_with_config(tmp_path: Path, text: str) -> tuple[int, str, Path]:
    config_path = tmp_path / "radar.yaml"
    config_path.write_text(text, encoding="utf-8")
    save_state(tmp_path)
    save_credentials(tmp_path)
    code, out = run_doctor(
        tmp_path,
        transport=healthy_transport(),
        session=FakeGuardSession(),
        extra=["--config", str(config_path)],
    )
    return code, out, config_path


def test_digest_warn_exit_zero_through_the_cli(tmp_path: Path) -> None:
    code, out, _ = run_with_config(tmp_path, "digest:\n  grace_hours: 1\n")
    assert line(out, "weekly digest").startswith("WARN"), out
    assert "digest.grace_hours" in line(out, "weekly digest")
    assert code == 0, out


def test_invalid_config_skips_both_lines(tmp_path: Path) -> None:
    code, out, _ = run_with_config(tmp_path, "digest: [\n")
    for name in ("weekly digest", "score rules"):
        assert line(out, name).startswith("SKIP"), out
        assert "needs a readable config" in line(out, name)
    assert line(out, "config").startswith("FAIL")
    assert code == 1


def test_digest_and_score_lines_no_leak(tmp_path: Path) -> None:
    _, out, config_path = run_with_config(
        tmp_path, "scores:\n  tests_below: 50\nprojects:\n  secret-project:\n    tests_below: 10\n"
    )
    for name in ("weekly digest", "score rules"):
        text = line(out, name)
        assert FAKE_TOKEN not in text
        assert GROUP not in text
        assert OPS not in text
        assert str(config_path) not in text
        assert "secret-project" not in text


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ("messages:\n  language: ru\n", "Russian (messages.language: ru)"),
        ("messages:\n  language: en\n", "English (messages.language: en)"),
        ("defaults:\n  stale_after_days: 14\n", "English (messages.language: en)"),
    ],
)
def test_doctor_shows_the_message_language_when_a_config_is_given(
    tmp_path: Path, body: str, detail: str
) -> None:
    save_state(tmp_path)
    save_credentials(tmp_path)
    config = tmp_path / "radar.yml"
    config.write_text(body, encoding="utf-8")
    _, out = run_doctor(
        tmp_path,
        transport=healthy_transport(),
        session=FakeGuardSession(),
        extra=["--config", str(config)],
    )
    assert line(out, "message language") == f"PASS  message language: {detail}"


def test_doctor_shows_no_message_language_without_a_config(tmp_path: Path) -> None:
    save_state(tmp_path)
    save_credentials(tmp_path)
    _, out = run_doctor(tmp_path, transport=healthy_transport(), session=FakeGuardSession())
    assert "message language" not in out


@pytest.mark.parametrize("body", ["messages:\n  language: de\n", None])
def test_doctor_shows_no_message_language_for_an_invalid_config(
    tmp_path: Path, body: str | None
) -> None:
    save_state(tmp_path)
    save_credentials(tmp_path)
    config = tmp_path / "radar.yml"
    if body is not None:
        config.write_text(body, encoding="utf-8")
    _, out = run_doctor(
        tmp_path,
        transport=healthy_transport(),
        session=FakeGuardSession(),
        extra=["--config", str(config)],
    )
    assert line(out, "config").startswith("FAIL")
    assert "message language" not in out


# --- triage line (D-16) ------------------------------------------------------------------------

CLAUDE_TOKEN = "sk-fake-9f3a"
TRIAGE_ON = "triage:\n  enabled: true\n"


def triage_config(directory: Path, text: str = TRIAGE_ON) -> list[str]:
    path = directory / "radar.yaml"
    path.write_text(text, encoding="utf-8")
    return ["--config", str(path)]


def test_triage_off_by_default_is_a_skip_line(tmp_path: Path) -> None:
    _, out = run_doctor(tmp_path, transport=healthy_transport())

    assert line(out, "triage") == "SKIP  triage: off"


def test_triage_ready_setup_passes_without_calling_claude(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    bin_dir = install_fake_claude(tmp_path, envelope="{}")
    monkeypatch.setenv("PATH", with_path(bin_dir))

    _, out = run_doctor(
        tmp_path,
        environ=environment(CLAUDE_CODE_OAUTH_TOKEN=CLAUDE_TOKEN),
        transport=healthy_transport(),
        extra=triage_config(tmp_path),
    )

    assert line(out, "triage") == (
        "PASS  triage: on, backend claude_cli, binary found, token set; calls 0, failed 0"
    )
    assert not (bin_dir / "record.json").exists()  # the fake would write it if it ran
    assert not (tmp_path / "triage-cost.json").exists()
    captured = capsys.readouterr()
    for text in (out, captured.out, captured.err, caplog.text):
        assert CLAUDE_TOKEN not in text


def facts(**overrides: object) -> TriageFacts:
    base: dict[str, object] = {
        "enabled": True,
        "backend": "claude_cli",
        "binary_found": True,
        "token_set": True,
        "today_calls": 0,
        "today_failures": 0,
    }
    base.update(overrides)
    return TriageFacts(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("given", "status", "needles"),
    [
        (facts(enabled=False), "SKIP", ["off"]),
        (facts(), "PASS", ["on, backend claude_cli, binary found, token set", "calls 0, failed 0"]),
        (facts(binary_found=False), "FAIL", ["claude not found on PATH"]),
        (facts(token_set=False), "FAIL", ["CLAUDE_CODE_OAUTH_TOKEN is not set"]),
        (
            facts(binary_found=False, token_set=False),
            "FAIL",
            ["claude not found on PATH", "CLAUDE_CODE_OAUTH_TOKEN is not set"],
        ),
        (facts(today_calls=3, today_failures=3), "WARN", ["calls 3, failed 3"]),
        (facts(today_calls=3, today_failures=1), "PASS", ["calls 3, failed 1"]),
        (facts(today_calls=1, today_failures=1), "WARN", ["calls 1, failed 1"]),
    ],
)
def test_triage_verdict_table(given: TriageFacts, status: str, needles: list[str]) -> None:
    result = verdict_triage(given)

    assert result.name == "triage"
    assert result.status.value == status
    for needle in needles:
        assert needle in result.detail


def test_triage_token_variable_matches_settings() -> None:
    assert doctor.ENV_CLAUDE_TOKEN == ENV_CLAUDE_TOKEN


def test_triage_missing_binary_and_token_fail_through_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    code, out = run_doctor(tmp_path, transport=healthy_transport(), extra=triage_config(tmp_path))

    assert code == 1
    detail = line(out, "triage")
    assert detail.startswith("FAIL")
    assert "claude" in detail
    assert "CLAUDE_CODE_OAUTH_TOKEN" in detail


def cost_file(directory: Path, *, calls: int, failures: int, days_ago: int = 0) -> Path:
    day = datetime.now(UTC).date() - timedelta(days=days_ago)
    record = CostDay(
        day=day,
        calls=calls,
        failures=failures,
        seconds=1.0,
        cost_usd=0.0,
        unknown_cost_calls=calls,
        input_tokens=0,
        output_tokens=0,
    )
    path = directory / "triage-cost.json"
    path.write_bytes(serialize_days((record,)))
    return path


def doctor_with_fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    bin_dir = install_fake_claude(tmp_path, envelope="{}")
    monkeypatch.setenv("PATH", with_path(bin_dir))
    _, out = run_doctor(
        tmp_path,
        environ=environment(CLAUDE_CODE_OAUTH_TOKEN=CLAUDE_TOKEN),
        transport=healthy_transport(),
        extra=triage_config(tmp_path),
    )
    return line(out, "triage"), bin_dir


def test_triage_every_call_failed_today_warns_and_leaves_the_file_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = cost_file(tmp_path, calls=3, failures=3)
    before = path.read_bytes()

    triage_line, bin_dir = doctor_with_fake_claude(tmp_path, monkeypatch)

    assert triage_line.startswith("WARN")
    assert "calls 3, failed 3" in triage_line
    assert path.read_bytes() == before
    assert not (bin_dir / "record.json").exists()


def test_triage_some_failures_today_stay_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cost_file(tmp_path, calls=3, failures=1)

    triage_line, _ = doctor_with_fake_claude(tmp_path, monkeypatch)

    assert triage_line.startswith("PASS")
    assert "calls 3, failed 1" in triage_line


def test_triage_ignores_a_record_of_another_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cost_file(tmp_path, calls=3, failures=3, days_ago=1)

    triage_line, _ = doctor_with_fake_claude(tmp_path, monkeypatch)

    assert triage_line.startswith("PASS")
    assert "calls 0, failed 0" in triage_line


def test_triage_unreadable_config_is_skipped(tmp_path: Path) -> None:
    _, out = run_doctor(
        tmp_path, transport=healthy_transport(), extra=triage_config(tmp_path, "messages: [\n")
    )

    assert line(out, "config").startswith("FAIL")
    assert line(out, "triage") == "SKIP  triage: needs a readable config"


@pytest.mark.parametrize(
    ("every", "expected"),
    [
        (180, "every 180 min from 00:00 UTC; next slot 2026-11-18 15:00 UTC"),
        (60, "every 60 min from 00:00 UTC; next slot 2026-11-18 13:00 UTC"),
        (1440, "every 1440 min from 00:00 UTC; next slot 2026-11-19 00:00 UTC"),
        (1, "every 1 min from 00:00 UTC; next slot 2026-11-18 12:01 UTC"),
    ],
)
def test_schedule_line_names_the_interval_and_the_next_slot(every: int, expected: str) -> None:
    result = verdict_schedule(config_of(f"schedule: {{every_minutes: {every}}}"), NOW)
    assert (result.status, result.detail) == (CheckStatus.PASS, expected)


def test_schedule_line_moves_on_at_a_slot_and_skips_without_a_config() -> None:
    on_a_slot = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)
    assert "next slot 2026-11-18 18:00 UTC" in verdict_schedule(RadarConfig(), on_a_slot).detail
    assert verdict_schedule(None, NOW).status is CheckStatus.SKIP


def test_schedule_line_is_skipped_for_an_unreadable_config(tmp_path: Path) -> None:
    bad = tmp_path / "radar.yaml"
    bad.write_text("schedule: {every_minutes: 7}\n", encoding="utf-8")
    _, out = run_doctor(tmp_path, transport=healthy_transport(), extra=["--config", str(bad)])
    assert line(out, "schedule").startswith("SKIP"), out
