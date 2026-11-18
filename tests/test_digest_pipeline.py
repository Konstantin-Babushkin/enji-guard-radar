"""03-11: the weekly digest through the whole run.

The tracer goes through cli.main on the fixtures with a fake Telegram transport; the edge table
runs pipeline.run_once over in-memory Guard worlds. Everything is offline.
"""

import io
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from guard_fakes import InMemoryGuardTools, call_key, run_item, world
from telegram_fakes import FakeTime, FakeTransport, ok_response
from test_high_pipeline import (
    BASE,
    CALM_CONFIG,
    PID,
    PROJECT,
    REPO,
    REPO2,
    RID,
    RID2,
    Sim,
    stamp,
)
from test_live_run_e2e import ENVIRON, FIXTURES, load_state
from test_reminders_pipeline import Scripted

from enji_guard_radar import cli
from enji_guard_radar.config import parse_config
from enji_guard_radar.guard import TOOL_LIST_PROJECTS, TOOL_LIST_REPOSITORIES
from enji_guard_radar.ledger import Loaded, State
from enji_guard_radar.model import OutboundMessage
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

DIGEST_TITLE = "<b>Guard radar weekly digest"  # the bold title, without its closing tag
TRANSIENT = NotDelivered(error=DeliveryError.TRANSIENT, detail="try later", stop_sending=False)
FATAL = NotDelivered(
    error=DeliveryError.UNAUTHORIZED,
    detail="bad token",
    stop_sending=True,
    operator_action="check the bot token",
)
# The default Sim repository has only an overview run, so its tooling check is limited to that
# type: nothing else is a gap, and a test sees only the items it adds.
RECON_ONLY = "tooling_gaps:\n  audit_types:\n    - audit.recon\n"
GRACE_48 = "digest:\n  grace_hours: 48\n"


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def digest_sim(tmp_path: Path, extra: str = "", *, tooling: str = RECON_ONLY) -> Sim:
    return Sim(tmp_path, CALM_CONFIG + tooling + extra, digest_on=True)


def is_digest(message: OutboundMessage) -> bool:
    return message.text.startswith(DIGEST_TITLE)


def digest_texts(report: RunReport) -> list[str]:
    return [m.text for m in report.digest_messages]


def planned(day: int) -> datetime:
    """05:00 UTC of the Wednesday of that Sim day."""
    return (BASE + timedelta(days=day)).replace(hour=5)


# ---- tracer: cli.main on the fixtures ----------------------------------------------------------


def run_cli(
    tmp_path: Path, now: str, transport: HttpTransport, *, dry_run: bool = False
) -> tuple[int, str]:
    out = io.StringIO()
    argv = [
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
        argv.append("--dry-run")
    code = cli.main(argv, environ=ENVIRON, transport=transport, sleep=FakeTime().sleep, stdout=out)
    return code, out.getvalue()


def test_weekly_digest_is_sent_once_on_the_first_run_at_or_after_the_planned_moment(
    tmp_path: Path,
) -> None:
    first = FakeTransport([ok_response(1)])
    code, out = run_cli(tmp_path, "2026-11-18T06:00:00Z", first)
    assert code == 0
    (request,) = first.requests
    text = str(request[1]["text"])
    assert text.startswith(DIGEST_TITLE)
    assert "Last successful Guard poll: 2026-11-18 06:00 UTC." in text
    assert "Open items:" in text
    assert "1 High finding in audit.security (acme/service-a)." in text
    assert "follow-ups: 0 reminder message(s), digest sent" in out
    assert out.index("summary:") < out.index("follow-ups:")
    record = load_state(tmp_path).digest
    assert record.outcome == "sent"
    assert record.planned_for == datetime(2026, 11, 18, 5, tzinfo=UTC)
    assert record.sent_at == datetime(2026, 11, 18, 6, tzinfo=UTC)

    for now in ("2026-11-18T06:00:00Z", "2026-11-18T09:00:00Z"):
        silent = FakeTransport([])
        run_cli(tmp_path, now, silent)
        assert silent.requests == []

    # A week later the fixtures also age a High series into an alert, so two requests may go out.
    next_week = FakeTransport([ok_response(2), ok_response(3)])
    run_cli(tmp_path, "2026-11-25T06:00:00Z", next_week)
    digests = [r for r in next_week.requests if str(r[1]["text"]).startswith(DIGEST_TITLE)]
    assert len(digests) == 1
    assert load_state(tmp_path).digest.planned_for == datetime(2026, 11, 25, 5, tzinfo=UTC)

    store = FileStateStore(tmp_path / "state")
    before = (store.state_path.read_bytes(), store.backup_path.read_bytes())
    dry = FakeTransport([])
    _, printed = run_cli(tmp_path, "2026-12-02T06:00:00Z", dry, dry_run=True)
    assert dry.requests == []
    assert "--- would send to group ---" in printed
    assert DIGEST_TITLE in printed
    assert printed.index("follow-ups: 0 reminder message(s), digest printed") < printed.index(
        "summary:"
    )
    assert (store.state_path.read_bytes(), store.backup_path.read_bytes()) == before


# ---- edge table -------------------------------------------------------------------------------


def test_window_failed_send_is_retried_inside_the_window_and_then_marked(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path, GRACE_48)
    sim.poll(0)
    failed = sim.poll(2, notifier=Scripted(TRANSIENT))
    assert failed.digest is DigestStatus.NOT_DELIVERED
    assert failed.outcome is RunOutcome.ATTENTION
    assert failed.follow_up_not_delivered == 1
    assert sim.state().digest.planned_for is None

    retry = Scripted()
    sent = sim.poll(3, notifier=retry)
    assert sent.digest is DigestStatus.SENT
    assert sent.outcome is RunOutcome.COMPLETED
    assert [is_digest(m) for m in retry.sent] == [True]
    assert sim.state().digest.outcome == "sent"
    assert sim.state().digest.planned_for == planned(2)

    later = Scripted()
    assert sim.poll(4, notifier=later).digest is DigestStatus.NOT_DUE
    assert later.sent == []


def test_skip_after_the_window_is_recorded_once_and_the_next_week_sends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sim = digest_sim(tmp_path)
    assert sim.poll(2).digest is DigestStatus.SENT
    sent_at = sim.state().digest.sent_at
    assert sim.poll(9, notifier=Scripted(TRANSIENT)).digest is DigestStatus.NOT_DELIVERED

    with caplog.at_level(logging.INFO, logger="enji_guard_radar.pipeline"):
        skipped = sim.poll(10)
    assert skipped.digest is DigestStatus.SKIPPED
    assert skipped.digest_messages == ()
    warnings = [r for r in caplog.records if "digest for the week of" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert "2026-11-11 skipped: not acknowledged within 24 hours" in warnings[0].getMessage()
    record = sim.state().digest
    assert (record.outcome, record.planned_for) == ("skipped", planned(9))
    assert record.sent_at == sent_at

    caplog.clear()
    quiet = Scripted()
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.pipeline"):
        assert sim.poll(11, notifier=quiet).digest is DigestStatus.NOT_DUE
    assert quiet.sent == []
    assert "digest" not in caplog.text

    assert sim.poll(16).digest is DigestStatus.SENT


def test_fresh_install_outside_a_window_is_not_due_and_logs_no_skip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sim = digest_sim(tmp_path)
    notifier = Scripted()
    with caplog.at_level(logging.DEBUG, logger="enji_guard_radar.pipeline"):
        report = sim.poll(3, notifier=notifier)  # Thursday afternoon
    assert report.digest is DigestStatus.NOT_DUE
    assert notifier.sent == []
    assert "skipped" not in caplog.text
    assert sim.state().digest.planned_for is None


def test_deferral_failed_listing_defers_and_the_next_good_poll_sends(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sim = digest_sim(tmp_path)
    sim.failures[call_key(TOOL_LIST_PROJECTS)] = GuardCallError("down")
    notifier = Scripted()
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.pipeline"):
        down = sim.poll(2, notifier=notifier)
    assert down.outcome is RunOutcome.GUARD_UNAVAILABLE
    assert down.digest is DigestStatus.DEFERRED
    assert notifier.sent == []
    assert "digest deferred: Guard poll failed; planned" in caplog.text

    sim.failures.clear()
    recovered = sim.poll(2)
    assert recovered.digest is DigestStatus.SENT
    assert len(recovered.digest_messages) == 1


def test_deferral_every_project_unknown_defers_too(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    sim.failures[call_key(TOOL_LIST_REPOSITORIES, project_id=PID)] = GuardCallError("down")
    notifier = Scripted()
    report = sim.poll(2, notifier=notifier)
    assert report.poll_failed
    assert report.digest is DigestStatus.DEFERRED
    assert notifier.sent == []
    assert sim.state().digest.planned_for is None


def test_deferral_after_the_window_the_first_good_poll_records_a_skip(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    assert sim.poll(2).digest is DigestStatus.SENT
    sim.failures[call_key(TOOL_LIST_PROJECTS)] = GuardCallError("down")
    assert sim.poll(9).digest is DigestStatus.DEFERRED
    sim.failures.clear()
    assert sim.poll(10).digest is DigestStatus.SKIPPED
    assert sim.state().digest.outcome == "skipped"


def test_partial_poll_still_sends_and_counts_the_unread_project(tmp_path: Path) -> None:
    config = parse_config(CALM_CONFIG + RECON_ONLY + "digest:\n  grace_hours: 24\n", source="t")
    runs = {
        RID: [_recon_run()],
        RID2: [_recon_run()],
    }
    guard = InMemoryGuardTools(
        world(
            [(PID, PROJECT), ("p-2", "Broken")],
            {PID: [(RID, REPO)], "p-2": [(RID2, REPO2)]},
            runs,
            {},
            {call_key(TOOL_LIST_REPOSITORIES, project_id="p-2"): GuardCallError("down")},
        )
    )
    now = BASE + timedelta(days=2, hours=2)
    notifier = Scripted()
    report = run_once(
        Deps(
            guard=guard,
            notifier=notifier,
            config=config,
            clock=lambda: now,
            store=FileStateStore(tmp_path / "state"),
        )
    )
    assert report.digest is DigestStatus.SENT
    assert not report.poll_failed
    (text,) = digest_texts(report)
    assert "1 project could not be read in that poll." in text
    assert "Broken" not in text


def _recon_run() -> dict[str, object]:
    return run_item("t-recon", "audit.recon", stamp(0))


def test_ack_before_mark_state_is_marked_only_after_the_acknowledgement(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    assert sim.poll(2).digest is DigestStatus.SENT
    sim.add_run(high=0, day=3)
    sim.poll(3)
    sim.add_run(high=0, day=4)
    resolved_poll = sim.poll(4)
    assert len(resolved_poll.resolved) == 1
    assert len(sim.state().resolved) == 1

    seen: list[tuple[datetime | None, int]] = []

    class Peeking:
        def send(self, message: OutboundMessage) -> SendOutcome:
            state = _loaded(sim)
            seen.append((state.digest.planned_for, len(state.resolved)))
            return Delivered(message_id=1)

    assert sim.poll(9, notifier=Peeking()).digest is DigestStatus.SENT
    assert seen == [(planned(2), 1)]
    after = sim.state()
    assert after.digest.planned_for == planned(9)
    assert after.resolved == ()


def _loaded(sim: Sim) -> State:
    load = FileStateStore(sim.state_dir).load()
    assert isinstance(load, Loaded)
    return load.state


def test_resolved_items_are_listed_once_in_the_next_digest_only(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    first = sim.poll(2)
    assert "Resolved since the last digest" not in digest_texts(first)[0]
    sim.add_run(high=0, day=3)
    sim.poll(3)
    sim.add_run(high=0, day=4)
    sim.poll(4)

    second = sim.poll(9)
    (text,) = digest_texts(second)
    assert "Resolved since the last digest (1):" in text
    assert "High findings in audit.security are closed." in text
    assert "Open items" not in text
    third = sim.poll(16)
    assert "Resolved since" not in digest_texts(third)[0]


def test_resolved_stale_project_that_becomes_fresh_is_listed_once(tmp_path: Path) -> None:
    sim = Sim(
        tmp_path,
        "defaults:\n  stale_after_days: 14\n" + RECON_ONLY,
        digest_on=True,
    )
    first = sim.poll(16)  # Wednesday: the project has been silent for 16 days
    assert "Stale:" in digest_texts(first)[0]
    sim.add_run("audit.recon", day=21, report=False)
    sim.poll(21)

    (text,) = digest_texts(sim.poll(23))
    assert text.count("no longer stale") == 1
    assert "Stale:" not in text
    assert "no longer stale" not in digest_texts(sim.poll(30))[0]


def test_resolved_items_of_a_skipped_week_stay_pending(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    assert sim.poll(2).digest is DigestStatus.SENT
    sim.add_run(high=0, day=3)
    sim.poll(3)
    sim.add_run(high=0, day=4)
    sim.poll(4)
    assert sim.poll(9, notifier=Scripted(TRANSIENT)).digest is DigestStatus.NOT_DELIVERED
    assert sim.poll(10).digest is DigestStatus.SKIPPED
    assert len(sim.state().resolved) == 1

    (text,) = digest_texts(sim.poll(16))
    assert "Resolved since the last digest (1):" in text
    assert sim.state().resolved == ()


def test_open_items_alerted_baselined_stale_score_gap_and_recurring_all_appear(
    tmp_path: Path,
) -> None:
    config = "scores:\n  ai_readiness_below: 60\nrecurring:\n  after_runs: 3\n"
    sim = Sim(tmp_path, CALM_CONFIG + config, digest_on=True)
    sim.repos.append((RID2, REPO2))
    sim.runs[RID2] = []
    sim.add_run(high=1, day=0)  # the series that will recur
    sim.add_run("audit.ai-readiness", day=0, rid=RID2)  # a score of 50, below 60
    sim.poll(0)  # baseline: nothing is sent
    sim.add_run(high=1, day=1)
    sim.poll(1)
    sim.add_run(high=1, day=2)
    alert = sim.poll(2)
    assert alert.messages == ()  # the count never rose: the series was alerted nowhere
    (text,) = digest_texts(alert)
    assert "- High:" in text
    assert "Seen by the radar since" in text
    assert "Recurring: High findings in each of the last 3 runs." in text
    assert "score" in text.lower()
    assert "Tooling gap" in text


def test_open_items_an_alerted_series_is_still_listed(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    assert len(sim.poll(1).messages) == 1  # the group already saw it as an alert
    (text,) = digest_texts(sim.poll(2))
    assert "2 High findings in audit.security (acme/svc)." in text


def test_open_items_a_stale_project_is_listed(tmp_path: Path) -> None:
    sim = Sim(tmp_path, "defaults:\n  stale_after_days: 14\n" + RECON_ONLY, digest_on=True)
    (text,) = digest_texts(sim.poll(16))
    assert "- Stale:" in text


def test_open_items_paused_project_appears_nowhere(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    sim.add_run(high=3, day=0)
    sim.paused = True
    report = sim.poll(2)
    (text,) = digest_texts(report)
    assert "nothing open" in text
    assert PROJECT not in text


def test_cap_six_open_series_show_three_and_say_how_many_more(tmp_path: Path) -> None:
    sim = digest_sim(
        tmp_path,
        "digest:\n  max_items: 3\n",
        tooling="tooling_gaps:\n  audit_types:\n    - audit.security\n",
    )
    extra = [(f"repo_x{n}", f"acme/extra-{n}") for n in range(5)]
    sim.repos.extend(extra)
    sim.add_run(high=1, day=0)
    for rid, _ in extra:
        sim.runs[rid] = []
        sim.add_run(high=1, day=0, rid=rid)
    report = sim.poll(2)
    (text,) = digest_texts(report)
    assert "Open items: 6." in text
    assert text.count("- High:") == 3
    assert "+3 more open items not shown." in text


def test_all_clear_is_one_short_message_with_the_poll_time(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    (text,) = digest_texts(sim.poll(2))
    assert text == (
        f"{DIGEST_TITLE}</b>: nothing open. Last successful Guard poll: 2026-11-04 12:00 UTC."
    )


def test_all_clear_followed_by_the_resolved_block(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    sim.add_run(high=0, day=3)
    sim.poll(3)
    sim.add_run(high=0, day=4)
    sim.poll(4)
    (text,) = digest_texts(sim.poll(9))
    first, *rest = text.split("\n")
    assert "nothing open" in first
    assert rest[0] == "Resolved since the last digest (1):"


def test_secret_gate_withholds_the_digest_and_nothing_is_marked(tmp_path: Path) -> None:
    secret_repo = "ghp_" + "a" * 36
    sim = digest_sim(tmp_path, GRACE_48)
    sim.repos[0] = (RID, secret_repo)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    inner = Scripted()
    report = sim.poll(2, notifier=SecretGatedNotifier(inner, ()))
    assert report.digest is DigestStatus.NOT_DELIVERED
    assert report.outcome is RunOutcome.ATTENTION
    assert report.operator_actions
    assert inner.sent == []
    assert sim.state().digest.planned_for is None

    sim.repos[0] = (RID, REPO)
    assert sim.poll(3).digest is DigestStatus.SENT


def test_fatal_alert_error_means_no_digest_this_run_and_it_goes_next_run(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path, GRACE_48)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    sim.add_run(high=4, day=2)  # a rise: an alert is planned for the Wednesday run
    notifier = Scripted(FATAL)
    report = sim.poll(2, notifier=notifier)
    assert report.not_delivered == 1
    assert report.digest is DigestStatus.DEFERRED
    assert not any(is_digest(m) for m in notifier.sent)
    assert sim.state().digest.planned_for is None

    after = Scripted()
    assert sim.poll(3, notifier=after).digest is DigestStatus.SENT
    assert any(is_digest(m) for m in after.sent)


def test_multi_message_digest_is_marked_only_when_every_message_was_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def two_messages(_digest: object) -> list[OutboundMessage]:
        return [OutboundMessage(text=f"{DIGEST_TITLE} ({n}/2)</b>") for n in (1, 2)]

    monkeypatch.setattr("enji_guard_radar.pipeline.render_digest", two_messages)
    sim = digest_sim(tmp_path, GRACE_48)
    first = Scripted(Delivered(message_id=1), TRANSIENT)
    report = sim.poll(2, notifier=first)
    assert report.digest is DigestStatus.NOT_DELIVERED
    assert sim.state().digest.planned_for is None

    retry = Scripted()
    assert sim.poll(3, notifier=retry).digest is DigestStatus.SENT
    assert len(retry.sent) == 2
    assert sim.state().digest.planned_for == planned(2)


def test_dry_run_digest_is_printed_and_the_state_files_do_not_change(tmp_path: Path) -> None:
    sim = digest_sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    store = FileStateStore(sim.state_dir)
    before = (store.state_path.read_bytes(), store.backup_path.read_bytes())

    class Printing:
        def __init__(self) -> None:
            self.sent: list[OutboundMessage] = []

        def send(self, message: OutboundMessage) -> SendOutcome:
            self.sent.append(message)
            return Printed()

    printing = Printing()
    guard = InMemoryGuardTools(
        world([(PID, PROJECT)], {PID: sim.repos}, sim.runs, sim.reports, sim.failures)
    )
    now = BASE + timedelta(days=2, hours=2)
    report = run_once(
        Deps(
            guard=guard,
            notifier=printing,
            config=sim.config,
            clock=lambda: now,
            store=DryRunStateStore(store),
        )
    )
    assert report.digest is DigestStatus.PRINTED
    assert [is_digest(m) for m in printing.sent] == [True]
    assert report.digest_messages == tuple(printing.sent)
    assert (store.state_path.read_bytes(), store.backup_path.read_bytes()) == before


def test_dry_run_through_the_cli_prints_the_follow_ups_line_and_writes_nothing(
    tmp_path: Path,
) -> None:
    transport = FakeTransport([])
    code, out = run_cli(tmp_path, "2026-11-18T06:00:00Z", transport, dry_run=True)
    assert code == 0
    assert transport.requests == []
    assert "follow-ups: 0 reminder message(s), digest printed" in out
    assert not (tmp_path / "state").exists()
    quiet = run_cli(tmp_path, "2026-11-19T12:00:00Z", transport, dry_run=True)[1]
    assert "follow-ups:" not in quiet
    assert "would send to" not in quiet


def test_failed_poll_never_builds_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def spy(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        message = "build_digest must not run on a failed poll"
        raise AssertionError(message)

    monkeypatch.setattr("enji_guard_radar.pipeline.build_digest", spy)
    sim = digest_sim(tmp_path)
    sim.failures[call_key(TOOL_LIST_PROJECTS)] = GuardCallError("down")
    assert sim.poll(2).digest is DigestStatus.DEFERRED
    sim.failures.clear()
    sim.failures[call_key(TOOL_LIST_REPOSITORIES, project_id=PID)] = GuardCallError("down")
    assert sim.poll(2).digest is DigestStatus.DEFERRED
    assert calls == []
