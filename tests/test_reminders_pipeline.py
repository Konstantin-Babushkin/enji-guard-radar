"""03-10: reminders for open High series, through the real pipeline over in-memory worlds."""

from pathlib import Path

from test_high_pipeline import Sim

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
