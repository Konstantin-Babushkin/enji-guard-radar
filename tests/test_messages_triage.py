"""The Claude line is an extra on one item: it never changes order, keys, counts or the set."""

import logging

import pytest
from test_messages import high, never_ran, stale

from enji_guard_radar.messages import MIN_MAX_CHARS, render_messages, telegram_length
from enji_guard_radar.model import Alert, AlertKey, AlertKind
from enji_guard_radar.texts import Language, text
from enji_guard_radar.triage import ItemTriage


def item(cause: str = "SQL injection in order search", priority: str = "p1") -> ItemTriage:
    return ItemTriage.model_validate({"item_id": "i1", "priority": priority, "cause": cause})


def test_single_alert_with_cause() -> None:
    alert = high()
    messages = render_messages([alert], collapse_above=3, triage={alert.key: item()})
    assert [m.text for m in messages] == [
        (
            "🔴 <b>Project A</b>: high problems 1, in audit security · open for 8 days\n"
            "   P1 · SQL injection in order search"
        )
    ]
    assert messages[0].keys == (alert.key,)


def mixed(count: int) -> list[Alert]:
    """Distinct alerts of all three kinds with different ages, so the order is not the input."""
    alerts: list[Alert] = []
    for n in range(count):
        pid = f"p{n:03d}"
        if n % 3 == 0:
            alerts.append(high(f"Project {n}", pid=pid, age=n + 1, count=n + 1))
        elif n % 3 == 1:
            alerts.append(stale(f"Stale {n}", pid=pid, age=n + 10))
        else:
            alerts.append(never_ran(f"Never {n}", pid=pid))
    return alerts


def entries(alerts: list[Alert], cause: str = "cause") -> dict[AlertKey, ItemTriage]:
    return {alert.key: item(cause) for alert in alerts}


def indented(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.startswith("   ")]


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("count", [1, 3, 60])
@pytest.mark.parametrize("triage", [None, {}], ids=["none", "empty"])
def test_no_triage_output_is_identical(
    count: int, language: Language, triage: dict[AlertKey, ItemTriage] | None
) -> None:
    alerts = mixed(count)
    plain = render_messages(alerts, collapse_above=3, language=language)
    assert render_messages(alerts, collapse_above=3, language=language, triage=triage) == plain


def test_summary_lines_carry_their_claude_line() -> None:
    alerts = mixed(5)
    plain = render_messages(alerts, collapse_above=3)
    highs = [alert for alert in alerts if alert.kind is AlertKind.HIGH_FINDINGS]
    assert len(highs) == 2
    chosen = {highs[0].key: item("first cause"), highs[1].key: item("second cause", "p3")}
    with_triage = render_messages(alerts, collapse_above=3, triage=chosen)
    assert [m.keys for m in with_triage] == [m.keys for m in plain]
    assert len(with_triage) == 1
    lines = with_triage[0].text.split("\n")
    # the older project (highs[1], 4 days) is listed first
    assert indented(with_triage[0].text) == ["   P3 · second cause", "   P1 · first cause"]
    for number, line in enumerate(lines):
        if line.startswith("   "):
            assert not lines[number - 1].startswith("   ")
    assert [line for line in lines if not line.startswith("   ")] == plain[0].text.split("\n")
    assert lines[2] == "   P3 · second cause"


def test_only_project_lines_of_high_alerts_get_a_claude_line() -> None:
    alerts = mixed(6)
    chosen = entries(alerts)
    with_triage = render_messages(alerts, collapse_above=3, triage=chosen)
    highs = sum(1 for alert in alerts if alert.kind is AlertKind.HIGH_FINDINGS)
    assert len(indented(with_triage[0].text)) == highs == 2


@pytest.mark.parametrize("count", [1, 3, 5])
def test_strip_claude_lines_gives_the_no_triage_texts(count: int) -> None:
    alerts = mixed(count)
    plain = render_messages(alerts, collapse_above=3)
    with_triage = render_messages(alerts, collapse_above=3, triage=entries(alerts))
    stripped = [
        "\n".join(line for line in m.text.split("\n") if not line.startswith("   "))
        for m in with_triage
    ]
    assert stripped == [m.text for m in plain]
    assert [m.keys for m in with_triage] == [m.keys for m in plain]


def test_split_keeps_lines_and_their_claude_lines_together() -> None:
    alerts = mixed(60)
    cause = "x" * 60
    plain = render_messages(alerts, collapse_above=3, max_chars=MIN_MAX_CHARS)
    with_triage = render_messages(
        alerts, collapse_above=3, max_chars=MIN_MAX_CHARS, triage=entries(alerts, cause)
    )
    assert len(with_triage) > len(plain) > 1
    assert all(telegram_length(m.text) <= MIN_MAX_CHARS for m in with_triage)
    assert [k for m in with_triage for k in m.keys] == [k for m in plain for k in m.keys]
    body = [line for m in with_triage for line in m.text.split("\n")[1:]]
    plain_body = [line for m in plain for line in m.text.split("\n")[1:]]
    assert [line for line in body if not line.startswith("   ")] == plain_body
    claude = [line for line in body if line.startswith("   ")]
    assert claude == [f"   P1 · {cause}"] * 20
    for message in with_triage:
        lines = message.text.split("\n")
        assert all(
            not line.startswith("   ") or lines[number - 1].startswith("🔴")
            for number, line in enumerate(lines)
        )


def test_escape_cause_is_escaped() -> None:
    alert = high()
    (message,) = render_messages([alert], collapse_above=3, triage={alert.key: item("a & b")})
    assert message.text.endswith("\n   P1 · a &amp; b")


def test_stray_key_changes_nothing_and_logs_the_count(caplog: pytest.LogCaptureFixture) -> None:
    alert = high()
    absent = high(pid="gone").key
    plain = render_messages([alert], collapse_above=3)
    with caplog.at_level(logging.WARNING):
        result = render_messages([alert], collapse_above=3, triage={absent: item("secret cause")})
    assert result == plain
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "1" in warnings[0].getMessage()
    assert "secret cause" not in caplog.text
    assert "gone" not in caplog.text


def test_stray_key_next_to_a_real_one_is_counted_once(caplog: pytest.LogCaptureFixture) -> None:
    alert = high()
    triage = {alert.key: item(), high(pid="gone").key: item("other")}
    with caplog.at_level(logging.WARNING):
        (message,) = render_messages([alert], collapse_above=3, triage=triage)
    assert message.text.endswith("\n   P1 · SQL injection in order search")
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_russian_uses_the_russian_texts() -> None:
    alert = high()
    (message,) = render_messages(
        [alert], collapse_above=3, language="ru", triage={alert.key: item("причина", "p2")}
    )
    (plain,) = render_messages([alert], collapse_above=3, language="ru")
    assert message.text == plain.text + "\n" + text(
        "ru", "alert.triage_line", priority="P2", cause="причина"
    )
    assert message.text.endswith("\n   P2 · причина")
