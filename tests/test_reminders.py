import random
from datetime import UTC, datetime, timedelta

import pytest

from enji_guard_radar.ledger import ConditionRecord, ReminderClock, State
from enji_guard_radar.messages import (
    MAX_NAME_CHARS,
    TELEGRAM_MAX_CHARS,
    telegram_length,
)
from enji_guard_radar.model import Alert, AlertKey, AlertKind, OutboundMessage, Severity
from enji_guard_radar.reminders import (
    mark_reminded,
    reminders_due,
    render_reminders,
)
from enji_guard_radar.texts import Language

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)


def high(
    name: str = "Acme",
    *,
    pid: str = "p1",
    repo_id: str = "r1",
    action: str = "audit.security",
    count: int | None = 3,
    age: int | None = 12,
    repo: str | None = "acme/svc",
    seen_since: datetime | None = NOW - timedelta(days=12),
) -> Alert:
    return Alert(
        key=AlertKey(project_id=pid, repo_id=repo_id, action_key=action, level="high"),
        kind=AlertKind.HIGH_FINDINGS,
        project_name=name,
        severity=Severity.HIGH,
        age_days=age,
        last_run_at=None,
        repo_count=1,
        count=count,
        repo_name=repo,
        seen_since=seen_since,
    )


def series_of(alert: Alert) -> tuple[str, str, str]:
    return (alert.key.project_id, alert.key.repo_id, alert.key.action_key)


def due(
    alerts: list[Alert],
    *,
    alerted_ago: timedelta | None = timedelta(days=7),
    reminded_ago: timedelta | None = None,
    planned: list[AlertKey] | None = None,
    every_days: int = 7,
    now: datetime = NOW,
) -> tuple[Alert, ...]:
    alerted_at = {} if alerted_ago is None else {a.key: now - alerted_ago for a in alerts}
    reminded_at = {} if reminded_ago is None else {series_of(a): now - reminded_ago for a in alerts}
    return reminders_due(
        alerts,
        alerted_at=alerted_at,
        reminded_at=reminded_at,
        planned=planned or [],
        every_days=every_days,
        now=now,
    )


def keys_of(messages: list[OutboundMessage]) -> list[AlertKey]:
    return [key for message in messages for key in message.keys]


def test_one_reminder_for_a_series_whose_clock_is_a_week_old() -> None:
    alert = high()
    result = due([alert])
    assert result == (alert,)
    messages = render_reminders(result)
    assert len(messages) == 1
    text = messages[0].text
    assert text == "<b>Radar reminder · 1 open</b>\n🔴 <b>Acme</b> · security High 3 · 12d"
    assert messages[0].keys == (alert.key,)
    assert telegram_length(text) < TELEGRAM_MAX_CHARS


# --- due-ness ---


@pytest.mark.parametrize("every_days", [1, 7, 90])
def test_boundary_the_interval_itself_is_due_one_second_short_is_not(every_days: int) -> None:
    interval = timedelta(days=every_days)
    alert = high()
    assert due([alert], alerted_ago=interval, every_days=every_days) == (alert,)
    short = interval - timedelta(seconds=1)
    assert due([alert], alerted_ago=short, every_days=every_days) == ()


@pytest.mark.parametrize(
    ("alerted_days", "reminded_days", "is_due"),
    [
        (20, 3, False),  # a recent reminder resets the clock
        (20, 8, True),
        (5, 8, False),  # reminded earlier than alerted (re-armed): alerted_at is the clock
        (8, 20, True),
    ],
)
def test_clock_is_the_later_of_alerted_at_and_last_reminder(
    alerted_days: int, reminded_days: int, is_due: bool
) -> None:
    alert = high()
    result = reminders_due(
        [alert],
        alerted_at={alert.key: NOW - timedelta(days=alerted_days)},
        reminded_at={series_of(alert): NOW - timedelta(days=reminded_days)},
        planned=[],
        every_days=7,
        now=NOW,
    )
    assert (result == (alert,)) is is_due


def test_never_alerted_series_is_not_reminded_however_old() -> None:
    alert = high(age=400, seen_since=NOW - timedelta(days=400))
    assert due([alert], alerted_ago=None) == ()
    assert (
        reminders_due(
            [alert],
            alerted_at={},
            reminded_at={series_of(alert): NOW - timedelta(days=100)},
            planned=[],
            every_days=7,
            now=NOW,
        )
        == ()
    )


def test_excluded_series_being_alerted_in_this_run() -> None:
    alert = high()
    assert due([alert], planned=[alert.key]) == ()
    other = high(pid="p2")
    assert due([alert, other], planned=[alert.key]) == (other,)


@pytest.mark.parametrize("count", [0, None])
def test_excluded_when_the_latest_high_count_is_not_positive(count: int | None) -> None:
    assert due([high(count=count)]) == ()


def test_excluded_stale_and_never_ran_alerts() -> None:
    stale = Alert(
        key=AlertKey(project_id="p1", level="stale"),
        kind=AlertKind.STALE,
        project_name="Acme",
        severity=Severity.MEDIUM,
        age_days=30,
        last_run_at=NOW - timedelta(days=30),
        repo_count=1,
    )
    never = Alert(
        key=AlertKey(project_id="p2", level="never_ran"),
        kind=AlertKind.NEVER_RAN,
        project_name="Beta",
        severity=Severity.MEDIUM,
        age_days=None,
        last_run_at=None,
        repo_count=0,
    )
    result = reminders_due(
        [stale, never],
        alerted_at={stale.key: NOW - timedelta(days=90), never.key: NOW - timedelta(days=90)},
        reminded_at={},
        planned=[],
        every_days=7,
        now=NOW,
    )
    assert result == ()


def test_excluded_duplicate_series_is_returned_once() -> None:
    alert = high()
    assert due([alert, alert]) == (alert,)


def test_no_cap_the_same_series_is_due_again_every_interval() -> None:
    alert = high()
    alerted_at = {alert.key: NOW - timedelta(days=7)}
    reminded_at: dict[tuple[str, str, str], datetime] = {}
    for weeks in (1, 2, 3):
        now = NOW + timedelta(days=7 * (weeks - 1))
        assert reminders_due(
            [alert],
            alerted_at=alerted_at,
            reminded_at=reminded_at,
            planned=[],
            every_days=7,
            now=now,
        ) == (alert,)
        reminded_at[series_of(alert)] = now
        # the day after a reminder it is not due again
        assert (
            reminders_due(
                [alert],
                alerted_at=alerted_at,
                reminded_at=reminded_at,
                planned=[],
                every_days=7,
                now=now + timedelta(days=1),
            )
            == ()
        )


def test_order_is_count_then_age_then_name_then_key_whatever_the_input_order() -> None:
    expected = [
        high("Zed", pid="a", count=9, age=1),
        high("Zed", pid="b", count=5, age=30),
        high("alpha", pid="c", count=5, age=10),
        high("Beta", pid="d", count=5, age=10),
        high("Beta", pid="e", count=5, age=10),
        high("Beta", pid="f", count=1, age=None),
    ]
    shuffled = list(expected)
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        assert list(due(shuffled)) == expected


# --- text ---


def test_short_reminder_text() -> None:
    first = high("Project A", pid="a", action="audit.security", count=3, age=15)
    second = high("Project D", pid="d", action="audit.tests", count=1, age=9)
    (message,) = render_reminders([first, second])
    assert message.text == (
        "<b>Radar reminder · 2 open</b>\n"
        "🔴 <b>Project A</b> · security High 3 · 15d\n"
        "🔴 <b>Project D</b> · tests High 1 · 9d"
    )


def test_render_two_series_in_one_message_with_both_keys_in_order() -> None:
    first = high("Acme", pid="p1", count=1, repo="acme/one")
    second = high("Beta", pid="p2", count=4, action="audit.deps", repo="beta/two", age=3)
    messages = render_reminders([second, first])
    assert len(messages) == 1
    lines = messages[0].text.split("\n")
    assert lines == [
        "<b>Radar reminder · 2 open</b>",
        "🔴 <b>Beta</b> · deps High 4 · 3d",
        "🔴 <b>Acme</b> · security High 1 · 12d",
    ]
    assert messages[0].keys == (second.key, first.key)


def test_render_text_is_counts_only() -> None:
    text = render_reminders([high()])[0].text
    for forbidden in ("F-001", "/src/", "Placeholder", "Severity", "since", "P1"):
        assert forbidden not in text


def test_render_escapes_names_and_cuts_long_ones() -> None:
    text = render_reminders([high('<b>&"x')])[0].text
    assert '<b>&lt;b&gt;&amp;"x</b>' in text
    both = render_reminders(
        [high("Acme", repo_id="r1", repo="a<i>/b&c"), high("Acme", repo_id="r2", repo="other")]
    )[0].text
    assert "(a&lt;i&gt;/b&amp;c)" in both
    long_text = render_reminders([high("n" * 200)])[0].text
    assert "n" * (MAX_NAME_CHARS - 3) + "..." in long_text
    assert "n" * MAX_NAME_CHARS not in long_text


def test_render_empty_input_gives_no_message() -> None:
    assert render_reminders([]) == []


def test_split_into_numbered_messages_each_owning_its_keys() -> None:
    alerts = [high("N" * 80, pid=f"p{i:03d}", count=100 - i) for i in range(60)]
    messages = render_reminders(alerts, max_chars=1000)
    assert len(messages) > 1
    total = len(messages)
    for number, message in enumerate(messages, start=1):
        assert telegram_length(message.text) <= 1000
        assert message.text.startswith(f"<b>Radar reminder · 60 open ({number}/{total})</b>")
        assert len(message.text.split("\n")) - 1 == len(message.keys)
    assert keys_of(messages) == [a.key for a in alerts]


def test_split_not_needed_keeps_a_plain_title() -> None:
    messages = render_reminders([high(pid=f"p{i}") for i in range(3)])
    assert len(messages) == 1
    assert messages[0].text.split("\n")[0] == "<b>Radar reminder · 3 open</b>"


def test_reminder_russian_text() -> None:
    first = high("Project A", pid="a", action="audit.security", count=3, age=15)
    second = high("Project D", pid="d", action="audit.tests", count=1, age=9)
    (message,) = render_reminders([first, second], language="ru")
    assert message.text == (
        "<b>Радар · напоминание · открыто: 2</b>\n"
        "🔴 <b>Project A</b> · security высокие 3 · 15 д\n"
        "🔴 <b>Project D</b> · tests высокие 1 · 9 д"
    )


def test_reminder_shows_repo_only_when_ambiguous() -> None:
    alerts = [
        high("P", pid="p", repo_id="r1", repo="r1", count=3),
        high("P", pid="p", repo_id="r2", repo="r2", count=2),
        high("Q", pid="q", repo_id="r1", repo="r1", action="audit.tests", count=1),
        high("S", pid="s", repo_id="r1", repo="r1", action="audit.security", count=1),
        high("S", pid="s", repo_id="r2", repo="r2", action="audit.tests", count=1),
    ]
    lines = render_reminders(alerts)[0].text.split("\n")[1:]
    assert "security (r1) High 3" in lines[0]
    assert "security (r2) High 2" in lines[1]
    assert not any("(" in line for line in lines[2:])


def test_reminder_split_80_series_keeps_one_total_in_every_title() -> None:
    alerts = [high("N" * 80, pid=f"p{i:03d}", count=200 - i) for i in range(80)]
    messages = render_reminders(alerts, max_chars=1000)
    total = len(messages)
    assert total > 1
    for number, message in enumerate(messages, start=1):
        assert message.text.split("\n")[0] == f"<b>Radar reminder · 80 open ({number}/{total})</b>"
        assert telegram_length(message.text) <= 1000
        assert len(message.text.split("\n")) - 1 == len(message.keys)
    assert keys_of(messages) == [a.key for a in alerts]


@pytest.mark.parametrize("language", ["en", "ru"])
def test_reminder_has_no_claude_or_priority_text(language: Language) -> None:
    alerts = [high("A", pid="a"), high("B", pid="b", action="audit.tests")]
    lines = render_reminders(alerts, language=language)[0].text.split("\n")
    for line in lines[1:]:
        assert line.startswith("🔴 <b>")
        for forbidden in ("P1", "P2", "P3", "Claude", "since", "Seen"):
            assert forbidden not in line


@pytest.mark.parametrize("language", ["en", "ru"])
def test_reminder_escapes_name_and_repo(language: Language) -> None:
    alerts = [
        high('<b>&"x', pid="p", repo_id="r1", repo="a<i>/b&c"),
        high('<b>&"x', pid="p", repo_id="r2", repo="d"),
    ]
    text = render_reminders(alerts, language=language)[0].text
    assert '<b>&lt;b&gt;&amp;"x</b>' in text
    assert "(a&lt;i&gt;/b&amp;c)" in text
    assert "<i>" not in text


# --- invalid input ---


@pytest.mark.parametrize("max_chars", [999, 4097])
def test_invalid_max_chars_raises(max_chars: int) -> None:
    with pytest.raises(ValueError, match="max_chars"):
        render_reminders([high()], max_chars=max_chars)


@pytest.mark.parametrize(
    "alert",
    [
        high(repo=None),
        high(age=None),
        high(count=None),
    ],
)
def test_invalid_alert_missing_a_field_raises(alert: Alert) -> None:
    with pytest.raises(ValueError, match="needs"):
        render_reminders([alert])


def test_invalid_naive_now_and_zero_interval_raise() -> None:
    alert = high()
    with pytest.raises(ValueError, match="timezone"):
        due([alert], now=datetime(2026, 11, 18, 9, 0))  # noqa: DTZ001
    with pytest.raises(ValueError, match="every_days"):
        due([alert], every_days=0)


# --- mark_reminded ---


def clock(alert: Alert, when: datetime) -> ReminderClock:
    return ReminderClock(
        project_id=alert.key.project_id,
        repo_id=alert.key.repo_id,
        action_key=alert.key.action_key,
        last_reminded_at=when,
    )


def test_mark_reminded_inserts_a_clock_for_a_high_key() -> None:
    alert = high()
    result = mark_reminded(State.empty(), [alert.key], NOW)
    assert result.reminders == (clock(alert, NOW),)


def test_mark_reminded_replaces_the_clock_of_a_series_already_present() -> None:
    alert = high()
    state = State(reminders=(clock(alert, NOW - timedelta(days=7)),))
    result = mark_reminded(state, [alert.key], NOW)
    assert result.reminders == (clock(alert, NOW),)


def test_mark_reminded_ignores_a_key_that_is_not_high() -> None:
    stale = AlertKey(project_id="p1", level="stale")
    assert mark_reminded(State.empty(), [stale], NOW).reminders == ()


def test_mark_reminded_keeps_clocks_sorted_and_one_per_series() -> None:
    first = high(pid="p1", repo_id="r2")
    second = high(pid="p1", repo_id="r1")
    result = mark_reminded(State.empty(), [first.key, second.key, first.key], NOW)
    assert result.reminders == (clock(second, NOW), clock(first, NOW))


def test_mark_reminded_leaves_the_rest_of_the_state_untouched() -> None:
    alert = high()
    other = high(pid="p2")
    condition = ConditionRecord(
        key=alert.key, first_seen_at=NOW, last_seen_at=NOW, baseline=False, alerted_at=NOW, count=3
    )
    state = State(conditions=(condition,), reminders=(clock(other, NOW - timedelta(days=9)),))
    result = mark_reminded(state, [alert.key], NOW)
    assert result.conditions == state.conditions
    assert result.digest == state.digest
    assert result.resolved == state.resolved
    assert clock(other, NOW - timedelta(days=9)) in result.reminders
    assert len(result.reminders) == 2


def test_mark_reminded_rejects_a_naive_now() -> None:
    with pytest.raises(ValueError, match="timezone"):
        mark_reminded(State.empty(), [high().key], datetime(2026, 11, 18, 9, 0))  # noqa: DTZ001
