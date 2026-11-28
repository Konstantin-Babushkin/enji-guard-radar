"""One group line per project: totals, plurals, order, collapse, splitting, Claude line."""

import logging
import random

import pytest
from test_messages import high, never_ran, stale

from enji_guard_radar.messages import (
    MIN_MAX_CHARS,
    render_messages,
    summary_header,
    telegram_length,
)
from enji_guard_radar.model import Alert, AlertKey
from enji_guard_radar.texts import Language
from enji_guard_radar.triage import ItemTriage


def texts(alerts: list[Alert], *, language: Language = "en", collapse_above: int = 9) -> list[str]:
    return [
        m.text for m in render_messages(alerts, collapse_above=collapse_above, language=language)
    ]


def audits(count: int, *, pid: str = "p1", age: int = 8) -> list[Alert]:
    return [high(pid=pid, audit=f"audit.a{n:02d}", repo=f"r{n}", age=age) for n in range(count)]


def claude(priority: str, cause: str) -> ItemTriage:
    return ItemTriage.model_validate({"item_id": "i1", "priority": priority, "cause": cause})


# --- totals ---


def test_project_line_sums_counts_counts_audit_types_and_takes_the_oldest_age() -> None:
    alerts = [
        high("P", pid="p", repo="r1", audit="audit.security", count=3, age=15),
        high("P", pid="p", repo="r2", audit="audit.security", count=2, age=9),
        high("P", pid="p", repo="r1", audit="audit.tests", count=1, age=4),
    ]
    assert texts(alerts) == [
        "🔴 <b>P</b>: high problems 6, in 2 audits, mostly security · open for 15 days"
    ]
    assert texts(alerts, language="ru") == [
        "🔴 <b>P</b>: высоких проблем 6, в 2 аудитах, больше всего security · держится 15 дней"
    ]


def test_one_message_carries_the_keys_of_every_series_of_the_project() -> None:
    alerts = audits(3)
    (message,) = render_messages(alerts, collapse_above=3)
    assert sorted(k.as_text() for k in message.keys) == sorted(a.key.as_text() for a in alerts)


def test_the_group_line_never_shows_the_repo() -> None:
    line = texts([high(repo="r1", repo_name="acme/secret-repo", audit="audit.security")])[0]
    assert "acme" not in line
    assert "secret-repo" not in line


@pytest.mark.parametrize(
    ("count", "english", "russian"),
    [
        (1, "in audit a00 ·", "в аудите a00 ·"),
        (2, "in 2 audits, mostly a00 ·", "в 2 аудитах, больше всего a00 ·"),
        (5, "in 5 audits, mostly a00 ·", "в 5 аудитах, больше всего a00 ·"),
        (21, "in 21 audits, mostly a00 ·", "в 21 аудите, больше всего a00 ·"),
    ],
)
def test_audit_plural_forms(count: int, english: str, russian: str) -> None:
    alerts = [a.model_copy(update={"count": 1}) for a in audits(count)]
    assert english in texts(alerts)[0]
    assert russian in texts(alerts, language="ru")[0]


# --- first alert and rise ---


@pytest.mark.parametrize("previous", [None, 0])
def test_a_first_alert_shows_the_count(previous: int | None) -> None:
    assert texts([high(count=4, previous=previous)]) == [
        "🔴 <b>Project A</b>: high problems 4, in audit security · open for 8 days"
    ]


def test_a_rise_shows_previous_to_total_and_a_mixed_project_sums_both() -> None:
    assert texts([high(count=3, previous=1)]) == [
        "🔴 <b>Project A</b>: high problems 3 (was 1), in audit security · open for 8 days"
    ]
    mixed = [high(count=3, previous=1), high(audit="audit.tests", count=2)]
    assert texts(mixed) == [
        "🔴 <b>Project A</b>: high problems 5 (was 1), in 2 audits, mostly security"
        " · open for 8 days"
    ]
    assert texts(mixed, language="ru") == [
        "🔴 <b>Project A</b>: высоких проблем 5 (было 1), в 2 аудитах, больше всего security"
        " · держится 8 дней"
    ]


# --- stale and never ran ---


def test_stale_and_never_ran_stay_their_own_lines() -> None:
    assert texts([stale("B", pid="b", age=28), never_ran("C", pid="c")]) == [
        "🟡 <b>B</b>: no scan for 28 days",
        "🟡 <b>C</b>: no scan yet",
    ]


def test_a_project_with_high_and_stale_has_two_lines_and_counts_once_in_the_header() -> None:
    (message,) = render_messages([stale("Project A"), *audits(2)], collapse_above=1)
    assert message.text.split("\n") == [
        "Radar · problems: 1 project",
        "🔴 <b>Project A</b>: high problems 2, in 2 audits, mostly a00 · open for 8 days",
        "🟡 <b>Project A</b>: no scan for 27 days",
    ]
    assert len(message.keys) == 3


# --- order ---


def test_lines_are_ordered_worst_first_then_oldest_then_name() -> None:
    alerts = [
        stale("S", pid="s", age=90),
        high("Young", pid="y", age=2),
        high("Old", pid="o", age=30),
        high("Old", pid="o", audit="audit.tests", age=3),
        never_ran("N", pid="n"),
    ]
    assert [t.split(":")[0] for t in texts(alerts)] == [
        "🔴 <b>Old</b>",
        "🔴 <b>Young</b>",
        "🟡 <b>S</b>",
        "🟡 <b>N</b>",
    ]


def test_output_does_not_depend_on_the_input_order() -> None:
    alerts = [*audits(3, pid="a", age=5), *audits(2, pid="b", age=9), stale("S", pid="s")]
    expected = render_messages(alerts, collapse_above=1)
    for seed in range(5):
        shuffled = alerts[:]
        random.Random(seed).shuffle(shuffled)
        result = render_messages(shuffled, collapse_above=1)
        assert [m.text for m in result] == [m.text for m in expected]
        assert [sorted(k.as_text() for k in m.keys) for m in result] == [
            sorted(k.as_text() for k in m.keys) for m in expected
        ]


# --- collapse counts lines ---


def projects(count: int) -> list[Alert]:
    return [high(f"Project {n:03d}", pid=f"p{n:03d}", age=10 + n) for n in range(count)]


def test_many_series_of_one_project_never_collapse() -> None:
    messages = render_messages(audits(12), collapse_above=3)
    assert len(messages) == 1
    assert "\n" not in messages[0].text
    assert len(messages[0].keys) == 12


def test_collapse_at_and_above_the_threshold_counts_lines() -> None:
    at = render_messages(projects(3), collapse_above=3)
    assert len(at) == 3
    assert all(len(m.keys) == 1 and not m.text.startswith("Radar") for m in at)
    (above,) = render_messages(projects(4), collapse_above=3)
    assert above.text.split("\n")[0] == "Radar · problems: 4 projects"
    assert len(above.keys) == 4


def test_headers_in_both_languages() -> None:
    assert summary_header(2, 1, 1) == "Radar · problems: 2 projects"
    assert summary_header(2, 1, 1, language="ru") == "Радар · проблемы: 2 проекта"
    assert summary_header(5, 2, 3, language="ru") == "Радар · проблемы: 5 проектов (2/3)"


# --- splitting ---


@pytest.mark.parametrize("language", ["en", "ru"])
def test_summary_splits_between_lines_within_the_limit(language: Language) -> None:
    alerts = [
        high(("Project <&> " + str(n)).ljust(80, "Ж"), pid=f"p{n:03d}", age=10 + n, count=n + 1)
        for n in range(60)
    ]
    parts = render_messages(alerts, collapse_above=3, max_chars=MIN_MAX_CHARS, language=language)
    assert len(parts) > 1
    for number, part in enumerate(parts, start=1):
        assert part.text.split("\n")[0].endswith(f"({number}/{len(parts)})")
        assert telegram_length(part.text) <= MIN_MAX_CHARS
        assert len(part.text.split("\n")) - 1 == len(part.keys)
    keys = [key for part in parts for key in part.keys]
    assert len(keys) == len(set(keys)) == 60


def test_a_project_is_never_split_across_chunks() -> None:
    alerts = [a for n in range(40) for a in audits(3, pid=f"p{n:03d}", age=10 + n)]
    parts = render_messages(alerts, collapse_above=3, max_chars=MIN_MAX_CHARS)
    assert len(parts) > 1
    for part in parts:
        owners = {key.project_id for key in part.keys}
        assert len(part.keys) == 3 * len(owners)
    keys = [key for part in parts for key in part.keys]
    assert len(keys) == len(set(keys)) == 120


# --- text safety ---


def test_project_name_is_escaped_once_and_cut() -> None:
    nasty = high('A<b>&"x"\n' + "y" * 120)
    (text,) = texts([nasty])
    assert text.startswith('🔴 <b>A&lt;b&gt;&amp;"x" ')
    assert "&amp;amp;" not in text
    assert "...</b>" in text
    assert "\n" not in text


# --- the Claude line ---


def pair() -> tuple[Alert, Alert]:
    return high(audit="audit.security", count=3, age=15), high(audit="audit.tests", count=2, age=9)


def test_the_most_urgent_claude_line_goes_under_the_project_line() -> None:
    first, second = pair()
    triage = {first.key: claude("p3", "Old dependency"), second.key: claude("p1", "SQL injection")}
    (message,) = render_messages([first, second], collapse_above=3, triage=triage)
    assert message.text == (
        "🔴 <b>Project A</b>: high problems 5, in 2 audits, mostly security · open for 15 days\n"
        "   P1 · SQL injection"
    )


def test_a_tie_takes_the_oldest_audit() -> None:
    first, second = pair()
    triage = {first.key: claude("p2", "Cause A"), second.key: claude("p2", "Cause B")}
    (message,) = render_messages([second, first], collapse_above=3, triage=triage)
    assert message.text.endswith("\n   P2 · Cause A")


def test_no_claude_line_without_an_entry_or_for_stale_and_never_ran() -> None:
    first, second = pair()
    assert texts([first, second])[0].count("\n") == 0
    others = [stale("S", pid="s"), never_ran("N", pid="n")]
    triage = {a.key: claude("p1", "Cause") for a in others}
    assert texts(others) == [
        m.text for m in render_messages(others, collapse_above=9, triage=triage)
    ]


def test_triage_changes_neither_lines_order_nor_keys() -> None:
    alerts = [*projects(5), stale("S", pid="s")]
    triage = {a.key: claude("p1", "Cause") for a in alerts}
    plain = render_messages(alerts, collapse_above=2)
    with_triage = render_messages(alerts, collapse_above=2, triage=triage)
    assert [m.keys for m in with_triage] == [m.keys for m in plain]
    stripped = "\n".join(
        line for line in with_triage[0].text.split("\n") if not line.startswith("   ")
    )
    assert stripped == plain[0].text


# --- logging ---


def test_grouping_and_triage_choice_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    first, second = pair()
    triage = {first.key: claude("p2", "Cause A"), second.key: claude("p1", "Cause B")}
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.messages"):
        render_messages([first, second], collapse_above=3, triage=triage)
    messages = [r.getMessage() for r in caplog.records]
    assert "grouped 2 alerts into 1 lines" in messages
    assert any(m.startswith("kept 1 of 2 triage lines") for m in messages)
    assert "Cause" not in caplog.text


def test_collapse_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.messages"):
        render_messages(projects(4), collapse_above=3)
    assert "collapsed 4 lines into 1 summary message(s)" in caplog.text


def test_stray_triage_keys_are_ignored() -> None:
    first, _ = pair()
    stray = AlertKey(project_id="gone", repo_id="r", action_key="audit.x", level="high")
    (message,) = render_messages([first], collapse_above=3, triage={stray: claude("p1", "Nope")})
    assert "\n" not in message.text
