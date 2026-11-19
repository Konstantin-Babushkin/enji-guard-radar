"""03-10: reminders for open High series, through the real pipeline over in-memory worlds."""

import logging
from datetime import timedelta
from pathlib import Path

import pytest
from guard_fakes import InMemoryGuardTools, world
from pydantic import SecretStr
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, error_response, ok_response
from test_high_pipeline import (
    BASE,
    CALM_CONFIG,
    CHAT_ID,
    PID,
    PROJECT,
    REPO,
    REPO2,
    RID,
    RID2,
    Accepting,
    Sim,
    assert_quiet,
    texts,
)

from enji_guard_radar.ledger import Loaded, ReminderClock
from enji_guard_radar.model import OutboundMessage
from enji_guard_radar.pipeline import Deps, RunOutcome, RunReport, run_once
from enji_guard_radar.ports import (
    Delivered,
    DeliveryError,
    NotDelivered,
    Notifier,
    Printed,
    SendOutcome,
)
from enji_guard_radar.secret_guard import SecretGatedNotifier
from enji_guard_radar.state_store import DryRunStateStore, FileStateStore
from enji_guard_radar.telegram import TelegramNotifier

REMINDER_TITLE = "Reminder: High findings are still open"


def test_reminder_comes_at_the_interval_and_not_before(tmp_path: Path) -> None:
    sim = Sim(tmp_path, reminders_on=True)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    alert = sim.poll(1)
    assert len(alert.messages) == 1
    assert alert.reminder_messages == ()

    early = sim.poll(7)  # 6 days after the alert
    assert early.reminder_messages == ()
    assert early.reminders_delivered == 0

    first = sim.poll(8)  # exactly 7 days
    assert first.messages == ()
    assert first.reminders_delivered == 1
    (message,) = first.reminder_messages
    assert message.text.startswith(f"<b>{REMINDER_TITLE}")
    assert "2 High findings in audit.security (acme/svc)." in message.text
    assert "Seen by the radar since" in message.text

    assert sim.poll(8).reminder_messages == ()
    assert sim.poll(14).reminder_messages == ()
    assert len(sim.poll(15).reminder_messages) == 1

    sim.add_run(high=4, day=16)
    rise = sim.poll(16)
    assert len(rise.messages) == 1
    assert rise.reminder_messages == ()
    assert sim.poll(22).reminder_messages == ()
    assert len(sim.poll(23).reminder_messages) == 1


# ---- edge table ------------------------------------------------------------------------------

EVERY_DAY = CALM_CONFIG + "reminders:\n  every_days: 1\n"


def telegram(transport: FakeTransport) -> Notifier:
    return SecretGatedNotifier(
        TelegramNotifier(SecretStr(FAKE_TOKEN), CHAT_ID, transport, sleep=FakeTime().sleep),
        (FAKE_TOKEN,),
    )


def alerted(tmp_path: Path, config_text: str = CALM_CONFIG, *, high: int = 2) -> Sim:
    """A project whose High series was alerted on day 1; the first reminder is due on day 8."""
    sim = Sim(tmp_path, config_text, reminders_on=True)
    sim.poll(0)
    sim.add_run(high=high, day=1)
    assert len(sim.poll(1).messages) == 1
    return sim


def poll_time(day: int) -> object:
    return BASE + timedelta(days=day, hours=2)


class Scripted:
    """A notifier answering from a list and then with Delivered; it remembers what it was sent."""

    def __init__(self, *outcomes: SendOutcome) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> SendOutcome:
        self.sent.append(message)
        return self.outcomes.pop(0) if self.outcomes else Delivered(message_id=1)


def is_reminder(message: OutboundMessage) -> bool:
    return REMINDER_TITLE in message.text


def test_failed_send_is_not_marked_and_retried_next_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sim = alerted(tmp_path)
    failing = FakeTransport([error_response(500, 500, "Internal Server Error")])
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.pipeline"):
        report = sim.poll(8, notifier=telegram(failing))
    assert report.outcome is RunOutcome.ATTENTION
    assert report.exit_code == 1
    assert report.follow_up_not_delivered == 1
    assert report.not_delivered == 0
    assert report.reminders_delivered == 0
    assert len(report.reminder_messages) == 1
    assert sim.state().reminders == ()
    assert "retried next run" in caplog.text

    retry = FakeTransport([ok_response(7)])
    report = sim.poll(9, notifier=telegram(retry))
    assert report.outcome is RunOutcome.COMPLETED
    assert report.reminders_delivered == 1
    assert REMINDER_TITLE in str(retry.requests[0][1]["text"])
    (clock,) = sim.state().reminders
    assert clock.last_reminded_at == poll_time(9)


def test_ack_before_mark_the_clock_is_written_after_the_acknowledgement(tmp_path: Path) -> None:
    sim = alerted(tmp_path)
    seen: list[tuple[ReminderClock, ...]] = []

    class Peeking:
        def send(self, message: OutboundMessage) -> SendOutcome:
            load = FileStateStore(sim.state_dir).load()
            assert isinstance(load, Loaded)
            if is_reminder(message):
                seen.append(load.state.reminders)
            return Delivered(message_id=1)

    report = sim.poll(8, notifier=Peeking())
    assert report.reminders_delivered == 1
    assert seen == [()]
    (clock,) = sim.state().reminders
    assert clock.last_reminded_at == poll_time(8)
    assert (clock.project_id, clock.repo_id, clock.action_key) == (PID, RID, "audit.security")


def test_baselined_series_is_not_reminded_until_a_rise_alerts_it(tmp_path: Path) -> None:
    sim = Sim(tmp_path, reminders_on=True)
    sim.add_run(high=2, day=0)
    first = sim.poll(0)
    assert first.baselined
    assert first.messages == ()
    quiet = sim.poll(30)
    assert quiet.reminder_messages == ()
    assert_quiet(quiet)

    sim.add_run(high=4, day=31)
    (rise,) = texts(sim.poll(31))
    assert rise == "🔴 <b>Proj</b> · security High 2→4 · 31d"
    assert sim.poll(37).reminder_messages == ()
    assert len(sim.poll(38).reminder_messages) == 1


def test_exclusions_stale_project_is_never_reminded(tmp_path: Path) -> None:
    sim = Sim(tmp_path, "defaults:\n  stale_after_days: 14\n", reminders_on=True)
    sim.poll(0)
    stale = sim.poll(20)
    assert any(text.startswith("🟡 <b>Proj</b> · no scan") for text in texts(stale))
    for day in (30, 50):
        report = sim.poll(day)
        assert report.reminder_messages == ()
        assert report.reminders_delivered == 0
    assert sim.state().reminders == ()


def test_exclusions_series_with_latest_count_zero_is_not_reminded(tmp_path: Path) -> None:
    sim = alerted(tmp_path)
    sim.add_run(high=0, day=7)
    report = sim.poll(8)
    assert sim.high_record().count == 0  # still inside the clean-run debounce
    assert report.reminder_messages == ()
    assert sim.state().reminders == ()


def test_exclusions_closed_series_is_not_reminded(tmp_path: Path) -> None:
    sim = alerted(tmp_path)
    sim.add_run(high=0, day=2)
    sim.poll(2)
    sim.add_run(high=0, day=3)
    closed = sim.poll(3)
    assert [key.level for key in closed.resolved] == ["high"]
    assert sim.high_condition() is None
    assert sim.poll(8).reminder_messages == ()
    assert sim.poll(30).reminder_messages == ()


def test_exclusions_series_alerted_in_this_run_is_not_reminded(tmp_path: Path) -> None:
    sim = alerted(tmp_path, EVERY_DAY)
    assert len(sim.poll(2).reminder_messages) == 1
    sim.add_run(high=5, day=3)
    rise = sim.poll(3)  # its clock is a day old, yet the rise alert goes instead
    assert len(rise.messages) == 1
    assert rise.reminder_messages == ()
    assert sim.poll(3).reminder_messages == ()
    assert len(sim.poll(4).reminder_messages) == 1


def test_exclusions_series_with_an_alert_still_planned_is_not_reminded(tmp_path: Path) -> None:
    sim = alerted(tmp_path)
    sim.add_run(high=5, day=7)
    transient = NotDelivered(error=DeliveryError.TRANSIENT, detail="try later", stop_sending=False)
    notifier = Scripted(transient)
    report = sim.poll(8, notifier=notifier)  # the clock is 7 days old, but the rise is planned
    assert [is_reminder(m) for m in notifier.sent] == [False]
    assert report.not_delivered == 1
    assert report.reminder_messages == ()
    assert sim.state().reminders == ()


def test_exclusions_two_due_series_are_one_message_and_two_clocks(tmp_path: Path) -> None:
    sim = Sim(tmp_path, reminders_on=True)
    sim.poll(0)
    sim.repos.append((RID2, REPO2))
    sim.add_run(high=2, day=1)
    sim.add_run(high=3, day=1, rid=RID2)
    sim.poll(1)
    report = sim.poll(8)
    (message,) = report.reminder_messages
    assert REPO in message.text
    assert REPO2 in message.text
    assert report.reminders_delivered == 1
    clocks = sim.state().reminders
    assert {clock.repo_id for clock in clocks} == {RID, RID2}
    assert {clock.last_reminded_at for clock in clocks} == {poll_time(8)}


def test_interval_setting_one_day_reminds_after_a_day_and_not_before(tmp_path: Path) -> None:
    sim = alerted(tmp_path, EVERY_DAY)
    assert sim.poll(1).reminder_messages == ()
    assert len(sim.poll(2).reminder_messages) == 1
    assert sim.poll(2).reminder_messages == ()
    assert len(sim.poll(3).reminder_messages) == 1


def test_interval_setting_quiet_default_keeps_long_spans_silent(tmp_path: Path) -> None:
    sim = Sim(tmp_path)  # reminders_on is False: every_days is 90
    sim.poll(0)
    sim.add_run(high=2, day=1)
    sim.poll(1)
    assert sim.poll(60).reminder_messages == ()
    assert sim.poll(90).reminder_messages == ()  # 89 days after the alert
    assert len(sim.poll(91).reminder_messages) == 1


def _with_a_due_series_and_a_new_alert(tmp_path: Path) -> Sim:
    """On day 8 the first series is due a reminder while a second series needs an alert."""
    sim = alerted(tmp_path)
    sim.repos.append((RID2, REPO2))
    sim.add_run(high=3, day=8, rid=RID2)
    return sim


def test_fatal_alert_send_error_stops_the_run_before_any_reminder(tmp_path: Path) -> None:
    sim = _with_a_due_series_and_a_new_alert(tmp_path)
    fatal = NotDelivered(
        error=DeliveryError.UNAUTHORIZED,
        detail="bad token",
        stop_sending=True,
        operator_action="check the bot token",
    )
    notifier = Scripted(fatal)
    report = sim.poll(8, notifier=notifier)
    assert [is_reminder(m) for m in notifier.sent] == [False]
    assert report.not_delivered == 1
    assert report.reminder_messages == ()
    assert report.reminders_delivered == 0
    assert sim.state().reminders == ()

    recovered = sim.poll(9)
    assert len(recovered.reminder_messages) == 1
    assert recovered.reminders_delivered == 1


def test_not_fatal_alert_failure_still_runs_the_reminder_step(tmp_path: Path) -> None:
    sim = _with_a_due_series_and_a_new_alert(tmp_path)
    transient = NotDelivered(error=DeliveryError.TRANSIENT, detail="try later", stop_sending=False)
    notifier = Scripted(transient)
    report = sim.poll(8, notifier=notifier)
    assert [is_reminder(m) for m in notifier.sent] == [False, True]
    assert report.not_delivered == 1
    assert report.reminders_delivered == 1
    assert report.follow_up_not_delivered == 0
    assert len(sim.state().reminders) == 1


def test_dry_run_prints_the_reminder_and_writes_nothing(tmp_path: Path) -> None:
    sim = alerted(tmp_path)
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
    report: RunReport = run_once(
        Deps(
            guard=guard,
            notifier=printing,
            config=sim.config,
            clock=lambda: BASE + timedelta(days=8, hours=2),
            store=DryRunStateStore(store),
        )
    )
    (message,) = report.reminder_messages
    assert [m.text for m in printing.sent] == [message.text]
    assert REMINDER_TITLE in message.text
    assert report.reminders_delivered == 0
    assert report.follow_up_not_delivered == 0
    assert (store.state_path.read_bytes(), store.backup_path.read_bytes()) == before


def test_logs_a_delivered_reminder(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    sim = alerted(tmp_path)
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.pipeline"):
        sim.poll(8, notifier=Accepting())
    assert "reminders: 1 due, 1 delivered" in caplog.text
