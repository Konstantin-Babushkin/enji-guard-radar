"""The weekly digest in the run: tracer through cli.main on the fixtures, then the edge table.

No network: Telegram is a fake transport, Guard is the synthetic fixtures or an in-memory world.
"""

import hashlib
import io
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from guard_fakes import InMemoryGuardTools, call_key, report_page, run_item, world
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, ok_response
from test_collect_pipeline import AI_READINESS, DEAD_CODE, SCOPED_TO_SECURITY, add_scored_run
from test_high_pipeline import (
    BASE,
    CALM_CONFIG,
    PID,
    PROJECT,
    REPO,
    RID,
    SECURITY,
    Sim,
    stamp,
)
from test_reminders_pipeline import Scripted

from enji_guard_radar import cli, pipeline
from enji_guard_radar.config import parse_config
from enji_guard_radar.guard import ARG_PROJECT_ID, TOOL_LIST_PROJECTS, TOOL_LIST_REPOSITORIES
from enji_guard_radar.ledger import DigestRecord, Loaded, State, deserialize_state
from enji_guard_radar.model import Audience, OutboundMessage
from enji_guard_radar.pipeline import Deps, DigestStatus, RunOutcome, RunReport, run_once
from enji_guard_radar.ports import (
    Delivered,
    DeliveryError,
    GuardCallError,
    NotDelivered,
    Printed,
    SendOutcome,
)
from enji_guard_radar.secret_guard import SecretGatedNotifier
from enji_guard_radar.state_store import DryRunStateStore, FileStateStore
from enji_guard_radar.telegram import HttpTransport

DIGEST_MARK = "<b>Radar weekly"  # starts both the titled digest and the all-clear line
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
ENVIRON = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "-1001234567890"}


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def run_at(
    tmp_path: Path, now: str, transport: HttpTransport, *, dry_run: bool = False
) -> tuple[int, str]:
    args = [
        "run",
        "--fixtures",
        str(FIXTURES),
        "--config",
        str(FIXTURES / "radar.yaml"),
        "--state-dir",
        str(tmp_path / "state"),
        "--env-file",
        str(tmp_path / "none.env"),
        "--now",
        now,
    ]
    if dry_run:
        args.append("--dry-run")
    out = io.StringIO()
    code = cli.main(args, environ=ENVIRON, transport=transport, sleep=FakeTime().sleep, stdout=out)
    return code, out.getvalue()


def load_state(tmp_path: Path) -> State:
    return deserialize_state((tmp_path / "state" / "state.json").read_bytes())


def digest_texts(transport: FakeTransport) -> list[str]:
    texts = [str(payload["text"]) for _, payload in transport.requests]
    return [text for text in texts if text.startswith("<b>Radar weekly")]


def state_digests(tmp_path: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((tmp_path / "state").iterdir())
        if p.is_file()
    }


def test_weekly_digest_is_sent_once_on_the_first_run_at_or_after_the_planned_moment(
    tmp_path: Path,
) -> None:
    transport = FakeTransport([ok_response(1)])
    run_at(tmp_path, "2026-11-18T06:00:00Z", transport)

    assert len(transport.requests) == 1
    text = str(transport.requests[0][1]["text"])
    assert text.startswith("<b>Radar weekly · ")
    assert "Last poll: 2026-11-18 06:00 UTC" in text
    assert " open</b>" in text.split("\n")[0]
    assert "· security High 1 · 8d" in text
    digest = load_state(tmp_path).digest
    assert digest.outcome == "sent"
    assert digest.planned_for == datetime(2026, 11, 18, 5, tzinfo=UTC)
    assert digest.sent_at == datetime(2026, 11, 18, 6, tzinfo=UTC)

    again = FakeTransport([])
    run_at(tmp_path, "2026-11-18T06:00:00Z", again)
    assert again.requests == []
    later = FakeTransport([])
    run_at(tmp_path, "2026-11-18T09:00:00Z", later)
    assert later.requests == []

    # A week later the fixtures also produce new alerts (older runs go stale): count the digests.
    next_week = FakeTransport([ok_response(i) for i in range(2, 12)])
    run_at(tmp_path, "2026-11-25T06:00:00Z", next_week)
    assert len(digest_texts(next_week)) == 1
    assert load_state(tmp_path).digest.planned_for == datetime(2026, 11, 25, 5, tzinfo=UTC)

    before = state_digests(tmp_path)
    dry = FakeTransport([])
    code, out = run_at(tmp_path, "2026-12-02T06:00:00Z", dry, dry_run=True)
    assert code == 0
    assert dry.requests == []
    lines = out.splitlines()
    assert lines.count("--- would send to group ---") == 1
    assert "<b>Radar weekly · " in out
    assert "follow-ups: 0 reminder message(s), digest printed" in out
    assert state_digests(tmp_path) == before


# ---- edge table: in-memory worlds through run_once --------------------------------------------

# Only the overview audit counts for tooling gaps, so a world with nothing open has nothing open.
QUIET_TOOLING = "tooling_gaps:\n  audit_types:\n    - audit.recon\n"
TRANSIENT = NotDelivered(error=DeliveryError.TRANSIENT, detail="down", stop_sending=False)
WED = 2  # Sim days: day 0 is a Monday, polls run at 12:00 UTC, so 2, 9 and 16 are Wednesdays.


def digest_sim(tmp_path: Path, extra: str = "", *, base: str = CALM_CONFIG) -> Sim:
    return Sim(tmp_path, base + QUIET_TOOLING + extra, digest_on=True)


def with_open_series(tmp_path: Path, extra: str = "") -> Sim:
    """A project with an alerted High series (opened on day 1, Tuesday); polls on days 0 and 1."""
    sim = digest_sim(tmp_path, extra)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    return sim


def add_repo(sim: Sim, rid: str, name: str, *, high: int, day: int = 0) -> None:
    sim.repos.append((rid, name))
    sim.runs[rid] = []
    sim.add_run("audit.recon", day=0, report=False, rid=rid)
    if high:
        sim.add_run(SECURITY, high=high, day=day, rid=rid)


def only_digest(report: RunReport) -> str:
    (message,) = report.digest_messages
    assert DIGEST_MARK in message.text
    return message.text


def digests_sent(notifier: Scripted) -> list[OutboundMessage]:
    return [m for m in notifier.sent if DIGEST_MARK in m.text]


def test_window_failed_send_is_retried_inside_the_window_and_then_marked(tmp_path: Path) -> None:
    sim = with_open_series(tmp_path, "digest:\n  grace_hours: 48\n")
    failed = sim.poll(WED, notifier=Scripted(TRANSIENT))
    assert failed.digest is DigestStatus.NOT_DELIVERED
    assert failed.outcome is RunOutcome.ATTENTION
    assert sim.state().digest == DigestRecord()

    retry = Scripted()
    sent = sim.poll(WED + 1, notifier=retry)  # Thursday 12:00, 31 hours after the planned moment
    assert sent.digest is DigestStatus.SENT
    assert len(digests_sent(retry)) == 1
    record = sim.state().digest
    assert record.outcome == "sent"
    assert record.planned_for == datetime(2026, 11, 4, 5, tzinfo=UTC)
    assert record.sent_at == BASE + timedelta(days=3, hours=2)

    quiet = Scripted()
    friday = sim.poll(WED + 2, notifier=quiet)
    assert friday.digest is DigestStatus.NOT_DUE
    assert quiet.sent == []


def test_skip_after_the_window_is_logged_once_and_not_made_up_later(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sim = with_open_series(tmp_path)
    assert sim.poll(WED).digest is DigestStatus.SENT
    first_sent_at = sim.state().digest.sent_at
    assert first_sent_at == BASE + timedelta(days=2, hours=2)

    assert sim.poll(9, notifier=Scripted(TRANSIENT)).digest is DigestStatus.NOT_DELIVERED
    late = Scripted()
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.pipeline"):
        skipped = sim.poll(10, notifier=late)  # Thursday 12:00: 31 hours, outside 24
    assert skipped.digest is DigestStatus.SKIPPED
    assert skipped.outcome is RunOutcome.COMPLETED
    assert late.sent == []
    warnings = [r for r in caplog.records if "digest for the week of" in r.getMessage()]
    assert [r.levelno for r in warnings] == [logging.WARNING]
    assert "digest for the week of 2026-11-11 skipped: not acknowledged within 24 hours" in (
        warnings[0].getMessage()
    )
    record = sim.state().digest
    assert (record.outcome, record.planned_for) == (
        "skipped",
        datetime(2026, 11, 11, 5, tzinfo=UTC),
    )
    assert record.sent_at == first_sent_at

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.pipeline"):
        friday = sim.poll(11, notifier=Scripted())
    assert friday.digest is DigestStatus.NOT_DUE
    assert "digest" not in caplog.text
    assert sim.poll(16).digest is DigestStatus.SENT


def test_fresh_install_outside_a_window_is_not_a_skip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sim = digest_sim(tmp_path)
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.pipeline"):
        report = sim.poll(3)  # Thursday afternoon, no digest history
    assert report.digest is DigestStatus.NOT_DUE
    assert "skipped" not in caplog.text
    assert sim.state().digest == DigestRecord()


def test_deferral_while_the_listing_failed_then_the_first_good_poll_sends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sim = with_open_series(tmp_path, "digest:\n  grace_hours: 48\n")
    sim.failures = {call_key(TOOL_LIST_PROJECTS): GuardCallError("outage")}
    notifier = Scripted()
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.pipeline"):
        down = sim.poll(WED, notifier=notifier)
    assert down.outcome is RunOutcome.GUARD_UNAVAILABLE
    assert down.digest is DigestStatus.DEFERRED
    assert notifier.sent == []
    assert "digest deferred: Guard poll failed; planned 2026-11-04 05:00:00+00:00" in caplog.text
    assert sim.state().digest == DigestRecord()

    sim.failures = {}
    sent = sim.poll(WED + 1, notifier=Scripted())
    assert sent.digest is DigestStatus.SENT


def test_failed_poll_with_every_project_unknown_never_builds_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = with_open_series(tmp_path)
    assert sim.poll(WED).digest is DigestStatus.SENT
    built: list[object] = []

    def spy(inp: object) -> object:
        built.append(inp)
        message = "build_digest must not run on a failed poll"
        raise AssertionError(message)

    monkeypatch.setattr(pipeline, "build_digest", spy)
    sim.failures = {
        call_key(TOOL_LIST_REPOSITORIES, **{ARG_PROJECT_ID: PID}): GuardCallError("outage")
    }
    notifier = Scripted()
    failed = sim.poll(9, notifier=notifier)
    assert failed.poll_failed
    assert failed.digest is DigestStatus.DEFERRED
    assert notifier.sent == []
    assert built == []

    # After the window the first successful poll records the week as skipped, not as sent.
    sim.failures = {}
    monkeypatch.undo()
    later = sim.poll(10, notifier=Scripted())
    assert later.digest is DigestStatus.SKIPPED
    assert sim.state().digest.outcome == "skipped"


def test_partial_poll_counts_the_unreadable_project_in_the_text(tmp_path: Path) -> None:
    other, other_repo = "p-2", "repo_r-3"
    recon = run_item("t-r1", "audit.recon", stamp(0))
    security = run_item("t-s1", SECURITY, stamp(1))
    responses = world(
        [(PID, PROJECT), (other, "Other")],
        {PID: [(RID, REPO)], other: [(other_repo, "acme/third")]},
        {RID: [security, recon], other_repo: [run_item("t-r2", "audit.recon", stamp(0))]},
        {"t-s1": report_page("t-s1", high=1)},
        {
            call_key(TOOL_LIST_REPOSITORIES, **{ARG_PROJECT_ID: other}): GuardCallError("outage"),
        },
    )
    notifier = Scripted()
    now = BASE + timedelta(days=WED, hours=2)
    report = run_once(
        Deps(
            guard=InMemoryGuardTools(responses),
            notifier=notifier,
            config=parse_config(CALM_CONFIG + QUIET_TOOLING, source="test"),
            clock=lambda: now,
            store=FileStateStore(tmp_path / "state"),
        )
    )
    assert report.digest is DigestStatus.SENT
    assert not report.poll_failed
    text = only_digest(report)
    assert "1 project could not be read in that poll." in text
    assert "<b>Proj</b>" in text
    assert "Other" not in text


def test_ack_before_mark_the_record_appears_only_after_the_acknowledgement(
    tmp_path: Path,
) -> None:
    sim = with_open_series(tmp_path)
    sim.add_run(high=0, day=3)
    sim.poll(3)
    sim.add_run(high=0, day=4)
    sim.poll(4)
    assert len(sim.state().resolved) == 1
    seen: list[tuple[DigestRecord, int]] = []

    class Peeking:
        def send(self, message: OutboundMessage) -> SendOutcome:
            load = FileStateStore(sim.state_dir).load()
            assert isinstance(load, Loaded)
            seen.append((load.state.digest, len(load.state.resolved)))
            return Delivered(message_id=1)

    report = sim.poll(9, notifier=Peeking())
    assert report.digest is DigestStatus.SENT
    assert seen == [(DigestRecord(), 1)]
    state = sim.state()
    assert state.digest.outcome == "sent"
    assert state.resolved == ()


def test_resolved_items_are_listed_once_in_the_next_digest(tmp_path: Path) -> None:
    sim = with_open_series(tmp_path)
    assert sim.poll(WED).digest is DigestStatus.SENT
    sim.add_run(high=0, day=3)
    sim.poll(3)
    sim.add_run(high=0, day=4)
    sim.poll(4)

    week2 = only_digest(sim.poll(9))
    assert "Resolved · 1" in week2.split("\n")
    assert "<b>Proj</b> · security closed" in week2.split("\n")
    assert week2.startswith("<b>Radar weekly</b> · nothing open · last poll ")

    week3 = only_digest(sim.poll(16))
    assert "Resolved" not in week3
    assert "closed" not in week3


def test_resolved_stale_project_is_listed_once(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path, base="defaults:\n  stale_after_days: 4\n")
    sim.poll(0)
    sim.poll(5)  # stale alert on Saturday
    sim.add_run("audit.recon", day=6, report=False)
    assert sim.poll(6).resolved != ()

    week2 = only_digest(sim.poll(9))
    assert "Resolved · 1" in week2.split("\n")
    assert "<b>Proj</b> · no scan closed" in week2.split("\n")
    week3 = only_digest(sim.poll(16))
    assert "no scan closed" not in week3


def test_resolved_items_of_a_skipped_week_stay_pending(tmp_path: Path) -> None:
    sim = with_open_series(tmp_path)
    assert sim.poll(WED).digest is DigestStatus.SENT
    sim.add_run(high=0, day=3)
    sim.poll(3)
    sim.add_run(high=0, day=4)
    sim.poll(4)
    assert sim.poll(9, notifier=Scripted(TRANSIENT)).digest is DigestStatus.NOT_DELIVERED
    assert sim.poll(10).digest is DigestStatus.SKIPPED
    assert len(sim.state().resolved) == 1

    text = only_digest(sim.poll(16))
    assert "Resolved · 1" in text.split("\n")
    assert sim.state().resolved == ()


def test_open_items_show_alerted_and_baselined_series_with_the_recurring_marker(
    tmp_path: Path,
) -> None:
    sim = digest_sim(tmp_path, "recurring:\n  after_runs: 2\n")
    add_repo(sim, "repo_r-2", "acme/other", high=1)  # present at the first poll: baselined
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)  # alerted
    sim.add_run(high=2, day=2)  # the second run in a row with High findings
    text = only_digest(sim.poll(WED))

    assert text.split("\n")[0] == "<b>Radar weekly · 2 open</b>"
    assert "security (acme/svc) High 2" in text
    assert "security (acme/other) High 1" in text
    marked = [line for line in text.splitlines() if "recurring" in line]
    assert len(marked) == 1
    assert "(acme/svc)" in marked[0]
    assert marked[0].endswith("recurring 2 runs")


def test_open_items_show_a_stale_project(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path, base="defaults:\n  stale_after_days: 1\n")
    sim.poll(0)
    text = only_digest(sim.poll(WED))
    assert text.split("\n")[0] == "<b>Radar weekly · 1 open</b>"
    assert "🟡 <b>Proj</b> · no scan 2d" in text.split("\n")


def test_open_items_show_a_low_score_and_a_tooling_gap(tmp_path: Path) -> None:
    config = (
        CALM_CONFIG
        + SCOPED_TO_SECURITY
        + "scores:\n  ai_readiness_below: 50\n"
        + f"tooling_gaps:\n  audit_types:\n    - audit.recon\n    - {DEAD_CODE}\n"
    )
    sim = Sim(tmp_path, config, digest_on=True)
    sim.poll(0)
    add_scored_run(sim, AI_READINESS, 40, day=1)
    text = only_digest(sim.poll(WED))
    assert text.split("\n")[0] == "<b>Radar weekly · 2 open</b>"
    assert "🟡 <b>Proj</b> · ai-readiness 40/100 · " in text
    assert "· ai-readiness 40/100 · 1d" in text
    assert "🟡 <b>Proj</b> · acme/svc · no runs 60d: dead-code" in text.split("\n")
    assert text.index("ai-readiness") < text.index("no runs")  # worst first


def test_open_items_leave_out_a_paused_project(tmp_path: Path) -> None:
    sim = with_open_series(tmp_path)
    sim.paused = True
    text = only_digest(sim.poll(WED))
    assert "Proj" not in text
    assert "nothing open" in text


def test_cap_shows_max_items_and_an_exact_remainder(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path, "digest:\n  max_items: 3\n")
    sim.add_run(SECURITY, high=1, day=0)
    for number in range(2, 7):
        add_repo(sim, f"repo_r-{number}", f"acme/r{number}", high=number)
    text = only_digest(sim.poll(WED))
    assert text.split("\n")[0] == "<b>Radar weekly · 6 open</b>"
    assert len([line for line in text.splitlines() if line.startswith("🔴")]) == 3
    assert "+3 more open items not shown." in text


def test_all_clear_is_one_line_with_the_poll_time(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    notifier = Scripted()
    report = sim.poll(WED, notifier=notifier)
    assert report.digest is DigestStatus.SENT
    (message,) = digests_sent(notifier)
    assert message.audience is Audience.GROUP
    assert message.text == ("<b>Radar weekly</b> · nothing open · last poll 2026-11-04 12:00 UTC")


def test_secret_gate_withholds_the_digest_and_nothing_is_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = with_open_series(tmp_path, "digest:\n  grace_hours: 48\n")
    monkeypatch.setattr("test_high_pipeline.PROJECT", "ghp_" + "a" * 36)
    inner = Scripted()
    gated = SecretGatedNotifier(inner, ())
    report = sim.poll(WED, notifier=gated)
    assert report.digest is DigestStatus.NOT_DELIVERED
    assert report.outcome is RunOutcome.ATTENTION
    assert len(report.operator_actions) == 1
    assert inner.sent == []
    assert sim.state().digest == DigestRecord()

    again = sim.poll(WED + 1, notifier=gated)
    assert again.digest is DigestStatus.NOT_DELIVERED  # attempted again, still withheld

    monkeypatch.undo()
    assert sim.poll(WED + 1, notifier=Scripted()).digest is DigestStatus.SENT


def test_fatal_send_error_defers_the_digest_to_the_next_run(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=2, day=WED)  # a new alert on the digest day
    fatal = NotDelivered(
        error=DeliveryError.UNAUTHORIZED,
        detail="bad token",
        stop_sending=True,
        operator_action="check the bot token",
    )
    stopped = Scripted(fatal)
    report = sim.poll(WED, notifier=stopped)
    assert len(stopped.sent) == 1  # the alert only
    assert report.digest is DigestStatus.DEFERRED
    assert report.digest_messages == ()
    assert sim.state().digest == DigestRecord()

    retry = Scripted()
    assert sim.poll(WED, notifier=retry).digest is DigestStatus.SENT
    assert len(digests_sent(retry)) == 1


def test_multi_message_digest_is_marked_only_when_every_part_was_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sim = digest_sim(tmp_path, "digest:\n  grace_hours: 48\n")
    parts = [
        OutboundMessage(text=f"{DIGEST_MARK} part {number}", audience=Audience.GROUP)
        for number in (1, 2)
    ]
    monkeypatch.setattr(pipeline, "render_digest", lambda digest: parts)
    broken = Scripted(Delivered(message_id=1), TRANSIENT)
    report = sim.poll(WED, notifier=broken)
    assert report.digest is DigestStatus.NOT_DELIVERED
    assert len(broken.sent) == 2
    assert sim.state().digest == DigestRecord()

    retry = Scripted()
    assert sim.poll(WED + 1, notifier=retry).digest is DigestStatus.SENT
    assert [m.text for m in retry.sent] == [p.text for p in parts]


class Printing:
    def send(self, message: OutboundMessage) -> SendOutcome:
        return Printed()


def test_dry_run_builds_the_digest_and_writes_nothing(tmp_path: Path) -> None:
    sim = with_open_series(tmp_path)
    store = FileStateStore(sim.state_dir)
    before = (store.state_path.read_bytes(), store.backup_path.read_bytes())
    now = BASE + timedelta(days=WED, hours=2)
    report = run_once(
        Deps(
            guard=InMemoryGuardTools(
                world([(PID, PROJECT)], {PID: sim.repos}, sim.runs, sim.reports, sim.failures)
            ),
            notifier=Printing(),
            config=sim.config,
            clock=lambda: now,
            store=DryRunStateStore(store),
        )
    )
    assert report.digest is DigestStatus.PRINTED
    assert "· security High 2 · 1d" in only_digest(report)
    assert (store.state_path.read_bytes(), store.backup_path.read_bytes()) == before


def test_low_priority_series_is_in_the_digest_block_and_never_an_alert(tmp_path: Path) -> None:
    low = "high:\n  high_priority_audit_types:\n    - audit.tech-health\n"
    sim = digest_sim(tmp_path, low)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    notifier = Scripted()
    assert sim.poll(1, notifier=notifier).messages == ()
    assert notifier.sent == []

    wednesday = sim.poll(WED, notifier=notifier)
    assert wednesday.messages == ()
    text = only_digest(wednesday)
    assert text.split("\n")[2:5] == [
        "Low priority · 1",
        "🟡 <b>Proj</b> · security High 2 · 1d",
        "Ages count from the radar's first sight.",
    ]
    assert text.startswith("<b>Radar weekly · 1 open</b>")
    assert "🔴" not in text
