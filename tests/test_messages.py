import random
from datetime import UTC, datetime

import pytest

from enji_guard_radar.messages import (
    DEFAULT_MAX_CHARS,
    MAX_NAME_CHARS,
    MIN_MAX_CHARS,
    TELEGRAM_MAX_CHARS,
    UNNAMED_PROJECT,
    clean_name,
    render_alert,
    render_messages,
    render_summary_item,
    telegram_length,
)
from enji_guard_radar.model import Alert, AlertKey, AlertKind, OutboundMessage, Severity

LAST_RUN = datetime(2026, 10, 20, 8, 0, tzinfo=UTC)


def stale(
    name: str, *, pid: str = "p1", age: int = 27, severity: Severity = Severity.MEDIUM
) -> Alert:
    return Alert(
        key=AlertKey(project_id=pid, level="stale"),
        kind=AlertKind.STALE,
        project_name=name,
        severity=severity,
        age_days=age,
        last_run_at=LAST_RUN,
        repo_count=1,
    )


def never_ran(name: str, *, pid: str = "p2", repos: int = 0) -> Alert:
    return Alert(
        key=AlertKey(project_id=pid, level="never_ran"),
        kind=AlertKind.NEVER_RAN,
        project_name=name,
        severity=Severity.MEDIUM,
        age_days=None,
        last_run_at=None,
        repo_count=repos,
    )


def many(count: int) -> list[Alert]:
    return [stale(f"Project number {i:03d}", pid=f"p{i:03d}", age=10 + i) for i in range(count)]


def keys_of(messages: list[OutboundMessage]) -> list[AlertKey]:
    return [key for message in messages for key in message.keys]


# --- names ---


def test_clean_name_turns_control_and_separator_characters_into_single_spaces() -> None:
    assert clean_name("Acme\nAPI\t<beta> & co") == "Acme API <beta> & co"
    separators = "a" + chr(0x2028) + "b" + chr(0x2029) + "c" + chr(0) + "d" + chr(0x200B) + "e"
    assert clean_name(separators) == "a b c d e"
    assert clean_name("  many   spaces  ") == "many spaces"


@pytest.mark.parametrize("name", ["", "   ", "\n\t", "\x00"])
def test_clean_name_maps_empty_to_unnamed(name: str) -> None:
    assert clean_name(name) == UNNAMED_PROJECT


def test_clean_name_cuts_long_names_to_77_plus_dots() -> None:
    assert clean_name("x" * MAX_NAME_CHARS) == "x" * MAX_NAME_CHARS
    cut = clean_name("x" * 120)
    assert cut == "x" * 77 + "..."
    assert len(cut) == MAX_NAME_CHARS


def test_telegram_length_counts_utf16_units() -> None:
    assert telegram_length("a") == 1
    assert telegram_length("\U0001f600") == 2
    assert telegram_length("&amp;") == 5


# --- individual alerts ---


def test_render_alert_stale_is_three_plain_lines() -> None:
    assert render_alert(stale("Project B")).split("\n") == [
        "Stale Guard scans: <b>Project B</b>",
        "No completed Guard run for 27 days (last: 2026-10-20).",
        "Severity: medium.",
    ]


def test_render_alert_never_ran_variants() -> None:
    assert "It has no repositories in Guard." in render_alert(never_ran("C", repos=0))
    assert "in its 1 repository." in render_alert(never_ran("C", repos=1))
    assert "in its 3 repositories." in render_alert(never_ran("C", repos=3))


def test_escape_name_once_in_alert_and_summary_item() -> None:
    alert = stale("Acme\nAPI\t<beta> & co")
    assert "<b>Acme API &lt;beta&gt; &amp; co</b>" in render_alert(alert)
    assert "<b>Acme API &lt;beta&gt; &amp; co</b>" in render_summary_item(alert)
    assert "&amp;amp;" not in render_alert(stale("a & b"))
    assert "<b>a &amp; b</b>" in render_alert(stale("a & b"))


def test_render_alert_age_of_one_day_is_singular() -> None:
    assert "for 1 day (last" in render_alert(stale("P", age=1))


def test_render_alert_cleans_empty_and_long_names() -> None:
    assert f"<b>{UNNAMED_PROJECT}</b>" in render_alert(stale("  "))
    assert f"<b>{'x' * 77}...</b>" in render_alert(stale("x" * 120))


def test_summary_item_formats() -> None:
    assert render_summary_item(stale("B")) == (
        "- <b>B</b>: no completed Guard run for 27 days (medium)"
    )
    assert (
        render_summary_item(never_ran("C")) == "- <b>C</b>: no completed Guard run found (medium)"
    )


# --- collapse ---


def test_collapse_zero_alerts_send_nothing() -> None:
    assert render_messages([], collapse_above=3) == []


def test_collapse_at_threshold_sends_each_alert_alone() -> None:
    messages = render_messages(many(3), collapse_above=3)
    assert len(messages) == 3
    assert all(len(m.keys) == 1 for m in messages)


def test_collapse_above_threshold_sends_one_summary_with_all_keys() -> None:
    alerts = many(4)
    messages = render_messages(alerts, collapse_above=3)
    assert len(messages) == 1
    assert messages[0].text.startswith("Guard radar: 4 problems need attention\n")
    assert sorted(k.as_text() for k in messages[0].keys) == sorted(a.key.as_text() for a in alerts)


def test_alerts_are_ordered_worst_first_then_oldest_then_never_ran_last() -> None:
    alerts = [
        never_ran("Never", pid="p9"),
        stale("Young", pid="p1", age=20),
        stale("Old", pid="p2", age=90),
        stale("High", pid="p3", age=15, severity=Severity.HIGH),
    ]
    order = [m.keys[0].project_id for m in render_messages(alerts, collapse_above=9)]
    assert order == ["p3", "p2", "p1", "p9"]


def test_same_name_different_projects_are_both_sent_ordered_by_key() -> None:
    alerts = [stale("Same", pid="p2"), stale("Same", pid="p1")]
    messages = render_messages(alerts, collapse_above=5)
    assert [m.keys[0].project_id for m in messages] == ["p1", "p2"]
    summary = render_messages([*alerts, stale("Same", pid="p3")], collapse_above=2)
    assert [k.project_id for k in summary[0].keys] == ["p1", "p2", "p3"]


# --- split ---


def test_split_boundary_is_exactly_max_chars() -> None:
    alerts = many(30)
    (whole,) = render_messages(alerts, collapse_above=3, max_chars=TELEGRAM_MAX_CHARS)
    size = telegram_length(whole.text)
    assert size > MIN_MAX_CHARS
    assert len(render_messages(alerts, collapse_above=3, max_chars=size)) == 1
    parts = render_messages(alerts, collapse_above=3, max_chars=size - 1)
    assert len(parts) == 2
    assert parts[0].text.startswith("Guard radar: 30 problems need attention (1/2)\n")
    assert parts[1].text.startswith("Guard radar: 30 problems need attention (2/2)\n")
    assert all(telegram_length(p.text) <= size - 1 for p in parts)
    keys = keys_of(parts)
    assert len(keys) == len(set(keys)) == 30
    assert keys == keys_of([whole])  # worst-first order kept across chunks


def test_split_each_chunk_carries_the_keys_of_its_items() -> None:
    parts = render_messages(many(60), collapse_above=3, max_chars=MIN_MAX_CHARS)
    assert len(parts) > 2
    for part in parts:
        item_lines = part.text.split("\n")[1:]
        assert len(item_lines) == len(part.keys)
        assert all(line.startswith("- <b>") and line.endswith(")") for line in item_lines)


@pytest.mark.parametrize("max_chars", [MIN_MAX_CHARS - 1, TELEGRAM_MAX_CHARS + 1])
def test_split_rejects_max_chars_out_of_range(max_chars: int) -> None:
    with pytest.raises(ValueError, match="max_chars"):
        render_messages(many(1), collapse_above=3, max_chars=max_chars)


@pytest.mark.parametrize("name", ["&" * 80, "\U0001f600" * 80])
def test_split_worst_case_alert_fits_alone_and_as_a_one_item_chunk(name: str) -> None:
    alert = stale(name, age=36500, severity=Severity.UNKNOWN)
    (single,) = render_messages([alert], collapse_above=3, max_chars=MIN_MAX_CHARS)
    assert telegram_length(single.text) <= MIN_MAX_CHARS
    others = [stale("o", pid=f"o{i}") for i in range(3)]
    parts = render_messages([alert, *others], collapse_above=3, max_chars=MIN_MAX_CHARS)
    assert all(telegram_length(p.text) <= MIN_MAX_CHARS for p in parts)
    assert len(keys_of(parts)) == 4


def test_invariant_over_random_alert_sets() -> None:
    rng = random.Random(20261116)
    alphabet = ["a", "B", " ", "&", "<", ">", "\n", "\t", "\U0001f600", "é", "x"]
    for _ in range(300):
        alerts = []
        for i in range(rng.randint(0, 300)):
            name = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 200)))
            if rng.random() < 0.3:
                alerts.append(never_ran(name, pid=f"n{i}", repos=rng.randint(0, 5)))
            else:
                alerts.append(stale(name, pid=f"s{i}", age=rng.randint(1, 36500)))
        max_chars = rng.choice([MIN_MAX_CHARS, 2000, DEFAULT_MAX_CHARS])
        messages = render_messages(alerts, collapse_above=rng.randint(1, 5), max_chars=max_chars)
        assert (len(messages) == 0) == (len(alerts) == 0)
        for message in messages:
            assert message.text
            assert telegram_length(message.text) <= max_chars <= TELEGRAM_MAX_CHARS
            assert "&amp;amp;" not in message.text
        keys = keys_of(messages)
        assert len(keys) == len(set(keys))
        assert {k.as_text() for k in keys} == {a.key.as_text() for a in alerts}
