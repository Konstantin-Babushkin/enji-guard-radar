"""The settings sheet through cli.main: run, serve, doctor and effective-config. No network.

Guard is the recorded fixture world, the sheet is a scripted fake reader (or the real Google
reader over a fake HTTPS transport), Telegram is a fake transport.
"""

import io
import logging
import re
import signal
import stat
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sheet_fakes import (
    PROJECT_A,
    PROJECT_B,
    SPREADSHEET_ID,
    FakeSheetReader,
    globals_grid,
    projects_grid,
    sheet,
)
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, ok_response
from test_priority_e2e import DIGEST_AT, SECURITY_LINE
from test_serve_e2e import Harness
from test_sheet_client import KEY_JSON, FakeSheetsHttp

from enji_guard_radar import cli
from enji_guard_radar.ledger import deserialize_state
from enji_guard_radar.ports import SheetFailure
from enji_guard_radar.settings import ENV_SHEET_KEY
from enji_guard_radar.sheet_settings import SheetData
from enji_guard_radar.sheet_store import SHEET_FILE, FileSheetStore
from enji_guard_radar.sheet_sync import SheetCopy, SheetRecord

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
NOW = "2026-11-16T12:00:00Z"
OPS_CHAT = "-1009999999999"
ENVIRON = {
    "TELEGRAM_BOT_TOKEN": FAKE_TOKEN,
    "TELEGRAM_CHAT_ID": "-1001234567890",
    "TELEGRAM_OPS_CHAT_ID": OPS_CHAT,
}
SHEET_YAML = (
    f"sheet:\n  enabled: true\n  spreadsheet_id: {SPREADSHEET_ID}\n"
    "ops:\n  failures_before_alert: 2\n"
)
PAUSE_B = sheet(projects_grid({"project_id": PROJECT_B, "mode": "paused"}))
BAD_SHEET = sheet(projects_grid({"project_id": PROJECT_B, "stale_after_days": "0"}))


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def write_config(tmp_path: Path, text: str = SHEET_YAML) -> Path:
    path = tmp_path / "radar.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def run_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "run",
        "--fixtures",
        str(FIXTURES),
        "--config",
        str(tmp_path / "radar.yaml"),
        "--state-dir",
        str(tmp_path / "state"),
        "--env-file",
        str(tmp_path / "none.env"),
        "--now",
        NOW,
        *extra,
    ]


def run_live(tmp_path: Path, reader: FakeSheetReader, transport: FakeTransport) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(
        run_args(tmp_path),
        environ=ENVIRON,
        transport=transport,
        sleep=FakeTime().sleep,
        stdout=out,
        sheet_reader=reader,
    )
    return code, out.getvalue()


def not_checked(out: str) -> int:
    match = re.search(r"(\d+) not checked", out)
    assert match is not None, out
    return int(match.group(1))


def baseline_not_checked(tmp_path: Path) -> int:
    """How many projects a run leaves unchecked when the sheet changes nothing."""
    plain = tmp_path / "plain"
    plain.mkdir()
    write_config(plain)
    _, out = run_live(plain, FakeSheetReader(sheet()), FakeTransport([]))
    return not_checked(out)


def test_a_disabled_sheet_is_never_read(tmp_path: Path) -> None:
    write_config(tmp_path, "defaults: {}\n")
    reader = FakeSheetReader(PAUSE_B)
    code, _ = run_live(tmp_path, reader, FakeTransport([]))
    assert code == 0
    assert reader.reads == 0
    assert not (tmp_path / "state" / SHEET_FILE).exists()


def test_the_sheet_changes_what_a_run_checks_and_is_stored_privately(tmp_path: Path) -> None:
    write_config(tmp_path)
    code, out = run_live(tmp_path, FakeSheetReader(PAUSE_B), FakeTransport([]))
    assert code == 0
    assert not_checked(out) == baseline_not_checked(tmp_path) + 1
    stored = tmp_path / "state" / SHEET_FILE
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600


def test_dry_run_reads_the_sheet_but_writes_nothing(tmp_path: Path) -> None:
    write_config(tmp_path)
    reader = FakeSheetReader(PAUSE_B)
    out = io.StringIO()
    code = cli.main(run_args(tmp_path, "--dry-run"), environ={}, stdout=out, sheet_reader=reader)
    assert code == 0
    assert reader.reads == 1
    assert "Project B: paused in config" in out.getvalue()
    assert not (tmp_path / "state").exists()


def test_a_failing_sheet_keeps_the_copy_and_ops_hears_once_then_once_on_recovery(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    write_config(tmp_path)
    reader = FakeSheetReader(PAUSE_B)
    transport = FakeTransport([ok_response(1), ok_response(2)])  # one outage, one recovery
    _, healthy = run_live(tmp_path, reader, transport)

    reader.set(SheetFailure.NETWORK)
    _, first = run_live(tmp_path, reader, transport)  # failure 1: below the threshold of 2
    assert transport.requests == []
    _, second = run_live(tmp_path, reader, transport)  # failure 2: the one outage message
    assert len(transport.requests) == 1
    _, payload = transport.requests[0]
    assert payload["chat_id"] == int(OPS_CHAT)
    assert "settings sheet" in str(payload["text"])
    assert "last good copy" in str(payload["text"])
    assert not_checked(first) == not_checked(second) == not_checked(healthy)
    _, third = run_live(tmp_path, reader, transport)  # still failing: no second message
    assert len(transport.requests) == 1
    assert not_checked(third) == not_checked(healthy)

    reader.set(PAUSE_B)
    run_live(tmp_path, reader, transport)
    assert len(transport.requests) == 2
    assert "works again" in str(transport.requests[1][1]["text"])
    run_live(tmp_path, reader, transport)
    assert len(transport.requests) == 2
    assert any("last good copy" in m and "in use" in m for m in caplog.messages)


def test_an_invalid_sheet_alerts_ops_with_tab_row_and_column_and_falls_back_to_yaml(
    tmp_path: Path,
) -> None:
    write_config(tmp_path)
    transport = FakeTransport([ok_response(1)])
    code, out = run_live(tmp_path, FakeSheetReader(BAD_SHEET), transport)
    assert code == 0
    assert not_checked(out) == baseline_not_checked(tmp_path)
    ((_, payload),) = transport.requests
    assert "projects row 2 column stale_after_days" in str(payload["text"])
    assert "radar.yaml" in str(payload["text"])


def test_the_real_reader_runs_through_main_and_the_key_never_leaks(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    write_config(tmp_path)
    out = io.StringIO()
    http = FakeSheetsHttp()
    code = cli.main(
        run_args(tmp_path),
        environ={**ENVIRON, ENV_SHEET_KEY: KEY_JSON},
        transport=FakeTransport([]),
        sleep=FakeTime().sleep,
        stdout=out,
        sheet_http=http,
    )
    assert code == 0
    assert len(http.requests) == 2
    private_key = KEY_JSON.split('"private_key": "')[1].split('"')[0]
    captured = capsys.readouterr()
    for text in (out.getvalue(), captured.out, captured.err, caplog.text):
        assert KEY_JSON not in text
        assert private_key not in text
        assert "BEGIN PRIVATE KEY" not in text


def test_a_missing_key_variable_is_a_sheet_failure_not_a_crash(tmp_path: Path) -> None:
    write_config(tmp_path)
    transport = FakeTransport([ok_response(1)])
    out = io.StringIO()
    code = cli.main(
        run_args(tmp_path),
        environ=ENVIRON,
        transport=transport,
        sleep=FakeTime().sleep,
        stdout=out,
        sheet_http=FakeSheetsHttp(),
    )
    assert code == 0
    ((_, payload),) = transport.requests
    assert "key" in str(payload["text"])


# --- effective-config ------------------------------------------------------------------------


def effective(
    tmp_path: Path, reader: FakeSheetReader | None, config: Path | None = None
) -> tuple[int, str]:
    out = io.StringIO()
    args = [
        "effective-config",
        "--state-dir",
        str(tmp_path / "state"),
        "--env-file",
        str(tmp_path / "none.env"),
    ]
    if config is not None:
        args += ["--config", str(config)]
    code = cli.main(args, environ={}, stdout=out, sheet_reader=reader)
    return code, out.getvalue()


def test_effective_config_prints_the_sheet_value_over_the_yaml_value(tmp_path: Path) -> None:
    config = write_config(tmp_path, SHEET_YAML + "schedule:\n  every_minutes: 180\n")
    reader = FakeSheetReader(
        sheet(
            projects_grid({"project_id": PROJECT_B, "mode": "paused"}),
            globals_grid(("schedule.every_minutes", "60")),
        )
    )
    code, out = effective(tmp_path, reader, config)
    assert code == 0
    assert out.splitlines()[0] == "# source: live sheet over YAML"
    assert "every_minutes: 60" in out
    assert "mode: paused" in out
    assert not (tmp_path / "state").exists()


def test_effective_config_without_a_sheet_says_yaml_only(tmp_path: Path) -> None:
    config = write_config(tmp_path, "defaults:\n  stale_after_days: 20\n")
    code, out = effective(tmp_path, None, config)
    assert code == 0
    assert out.splitlines()[0] == "# source: YAML only (sheet disabled)"
    assert "stale_after_days: 20" in out


def test_effective_config_uses_the_last_good_copy_when_the_sheet_fails(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    run_live(tmp_path, FakeSheetReader(PAUSE_B), FakeTransport([]))
    code, out = effective(tmp_path, FakeSheetReader(SheetFailure.DENIED), config)
    assert code == 0
    first = out.splitlines()[0]
    assert first.startswith("# source: last good sheet copy from ")
    assert "denied" in first
    assert "mode: paused" in out


def test_effective_config_rejects_a_broken_yaml_with_exit_2(tmp_path: Path) -> None:
    config = write_config(tmp_path, "sheet:\n  enabled: true\n")
    code, out = effective(tmp_path, FakeSheetReader(sheet()), config)
    assert code == 2
    assert out == ""


# --- doctor ----------------------------------------------------------------------------------


def doctor_line(tmp_path: Path, reader: FakeSheetReader | None, config: Path) -> str:
    out = io.StringIO()
    cli.main(
        [
            "doctor",
            "--config",
            str(config),
            "--state-dir",
            str(tmp_path / "state"),
            "--env-file",
            str(tmp_path / "none.env"),
        ],
        environ={},
        transport=FakeTransport([]),
        guard_connector=lambda _d: pytest.fail("no Guard login, so no connection"),
        stdout=out,
        sheet_reader=reader,
    )
    (line,) = [x for x in out.getvalue().splitlines() if "settings sheet:" in x]
    return line


def test_doctor_reports_the_sheet_in_every_state(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    assert doctor_line(tmp_path, None, write_config(tmp_path, "defaults: {}\n")).startswith("SKIP")
    config = write_config(tmp_path)
    no_copy = doctor_line(tmp_path, FakeSheetReader(SheetFailure.NETWORK), config)
    assert no_copy.startswith("FAIL")
    assert "no good copy stored yet" in no_copy
    ok = doctor_line(tmp_path, FakeSheetReader(PAUSE_B), config)
    assert ok.startswith("PASS")
    assert "configured, reachable, valid" in ok
    run_live(tmp_path, FakeSheetReader(PAUSE_B), FakeTransport([]))
    unreachable = doctor_line(tmp_path, FakeSheetReader(SheetFailure.DENIED), config)
    assert unreachable.startswith("WARN")
    assert "not reachable (denied:" in unreachable
    assert "last good copy" in unreachable
    invalid = doctor_line(tmp_path, FakeSheetReader(BAD_SHEET), config)
    assert invalid.startswith("WARN")
    assert "projects row 2 column stale_after_days" in invalid


def test_doctor_names_the_missing_key_variable(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    out = io.StringIO()
    cli.main(
        [
            "doctor",
            "--config",
            str(config),
            "--state-dir",
            str(tmp_path / "state"),
            "--env-file",
            str(tmp_path / "none.env"),
        ],
        environ={},
        transport=FakeTransport([]),
        guard_connector=lambda _d: pytest.fail("no Guard login, so no connection"),
        stdout=out,
        sheet_http=FakeSheetsHttp(),
    )
    (line,) = [x for x in out.getvalue().splitlines() if "settings sheet:" in x]
    assert line.startswith("FAIL")
    assert ENV_SHEET_KEY in line


def test_doctor_and_effective_config_agree_when_the_stored_copy_no_longer_fits(
    tmp_path: Path,
) -> None:
    config = write_config(tmp_path)
    copy = SheetCopy(fetched_at=datetime(2026, 11, 20, tzinfo=UTC), data=BAD_SHEET)
    FileSheetStore(tmp_path / "state").save(SheetRecord(last_good=copy))
    down = FakeSheetReader(SheetFailure.NETWORK)
    line = doctor_line(tmp_path, down, config)
    assert line.startswith("FAIL")
    assert "no longer validates" in line
    assert "YAML alone" in line
    _, out = effective(tmp_path, FakeSheetReader(SheetFailure.NETWORK), config)
    assert out.splitlines()[0].startswith("# source: YAML only, no usable sheet copy")


# --- serve -----------------------------------------------------------------------------------


def test_serve_applies_a_schedule_changed_in_the_sheet_without_a_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    harness = Harness(tmp_path)
    harness.config.write_text(
        SHEET_YAML + "schedule:\n  every_minutes: 180\n",
        encoding="utf-8",
    )
    reader = FakeSheetReader(sheet(globals_=globals_grid(("schedule.every_minutes", "60"))))

    from test_serve_e2e import ENVIRON as SERVE_ENVIRON

    code = cli.main(
        [
            "serve",
            "--config",
            str(harness.config),
            "--state-dir",
            str(harness.state_dir),
            "--env-file",
            str(tmp_path / "none.env"),
        ],
        environ=SERVE_ENVIRON,
        transport=FakeTransport([]),
        sleep=FakeTime().sleep,
        stdout=harness.out,
        guard_connector=harness.connector,
        serve_clock=harness.clock,
        serve_wait=harness.wait,
        sheet_reader=reader,
    )
    assert code == 0
    starts = [m.split()[3] for m in caplog.messages if m.startswith("serve: run start")]
    # Catch-up at 02:00 on the YAML grid (180); the sheet then sets 60, so 03:00 and 04:00 follow.
    assert starts == [
        "slot=2026-11-23T02:00:00Z",
        "slot=2026-11-23T03:00:00Z",
        "slot=2026-11-23T04:00:00Z",
    ]
    assert any("schedule changed every_minutes=180->60" in m for m in caplog.messages)
    assert datetime(2026, 11, 23, tzinfo=UTC) < harness.now < datetime(2026, 11, 24, tzinfo=UTC)
    assert harness.now - timedelta(hours=3) < datetime(2026, 11, 23, 5, tzinfo=UTC)
    assert signal.getsignal(signal.SIGTERM) is not None


# --- high priority audit types set in the sheet ---------------------------------------------------


def priority_run(tmp_path: Path, reader: FakeSheetReader) -> list[str]:
    """A live run on the fixture world at digest time; returns the lines of the one message sent."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "radar.yaml"
    config.write_text(
        (FIXTURES / "radar.yaml").read_text(encoding="utf-8")
        + f"sheet:\n  enabled: true\n  spreadsheet_id: {SPREADSHEET_ID}\n",
        encoding="utf-8",
    )
    transport = FakeTransport([ok_response(1)])
    code = cli.main(
        [*run_args(tmp_path)[:-2], "--now", DIGEST_AT],
        environ={"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "-1001234567890"},
        transport=transport,
        sleep=FakeTime().sleep,
        stdout=io.StringIO(),
        sheet_reader=reader,
    )
    assert code == 0
    ((_, payload),) = transport.requests
    return str(payload["text"]).split("\n")


def types_row(project_id: str, types: str) -> SheetData:
    return sheet(projects_grid({"project_id": project_id, "high_priority_audit_types": types}))


def test_without_the_sheet_the_fixture_security_series_alerts(tmp_path: Path) -> None:
    lines = priority_run(tmp_path, FakeSheetReader(sheet()))
    assert f"🔴 {SECURITY_LINE}" in lines
    assert "Low priority · 1" not in lines


def test_a_project_list_in_the_sheet_makes_its_security_series_low_priority(
    tmp_path: Path,
) -> None:
    lines = priority_run(tmp_path, FakeSheetReader(types_row(PROJECT_A, "audit.tech-health")))
    assert f"🟡 {SECURITY_LINE}" in lines
    assert "Low priority · 1" in lines
    assert not any(line.startswith("🔴") for line in lines)
    state = deserialize_state((tmp_path / "state" / "state.json").read_bytes())
    assert [c for c in state.conditions if c.key.action_key == "audit.security"] == []
    (history,) = [h for h in state.histories if h.action_key == "audit.security"]
    assert history.open is True


def test_a_list_that_names_the_type_or_belongs_to_another_project_keeps_it_high(
    tmp_path: Path,
) -> None:
    named = priority_run(
        tmp_path / "named", FakeSheetReader(types_row(PROJECT_A, "audit.security"))
    )
    other = priority_run(tmp_path / "other", FakeSheetReader(types_row(PROJECT_B, "audit.tests")))
    for lines in (named, other):
        assert f"🔴 {SECURITY_LINE}" in lines
        assert "Low priority · 1" not in lines
