import random
import re
from datetime import UTC, datetime

import pytest

from enji_guard_radar.messages import (
    DEFAULT_MAX_CHARS,
    MAX_NAME_CHARS,
    MIN_MAX_CHARS,
    TELEGRAM_MAX_CHARS,
    UNNAMED_PROJECT,
    age_text,
    ambiguous_series,
    audit_short,
    clean_name,
    high_line,
    marker,
    render_alert,
    render_messages,
    render_summary_item,
    telegram_length,
)
from enji_guard_radar.model import Alert, AlertKey, AlertKind, OutboundMessage, Severity
from enji_guard_radar.ports import Delivered, DeliveryError, NotDelivered, SendOutcome
from enji_guard_radar.secret_guard import SecretGatedNotifier

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


def high(
    name: str = "Project A",
    *,
    pid: str = "p1",
    repo: str = "r1",
    repo_name: str = "acme/r1",
    audit: str = "audit.security",
    count: int = 1,
    previous: int | None = None,
    age: int = 8,
) -> Alert:
    return Alert(
        key=AlertKey(project_id=pid, repo_id=repo, action_key=audit, level="high"),
        kind=AlertKind.HIGH_FINDINGS,
        project_name=name,
        severity=Severity.HIGH,
        age_days=age,
        last_run_at=None,
        repo_count=1,
        count=count,
        repo_name=repo_name,
        previous_count=previous,
        seen_since=datetime(2026, 11, 9, tzinfo=UTC),
    )


class RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> SendOutcome:
        self.sent.append(message)
        return Delivered()


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


def test_high_alert_is_one_line() -> None:
    alert = high()
    assert render_alert(alert) == "🔴 <b>Project A</b> · security High 0→1 · 8d"
    assert render_alert(alert, language="ru") == "🔴 <b>Project A</b> · security высокие 0→1 · 8 д"


def test_high_rise_starts_from_the_previous_count() -> None:
    assert render_alert(high(count=3, previous=1)) == "🔴 <b>Project A</b> · security High 1→3 · 8d"
    # a retried new series (no previous count) starts from 0
    assert render_alert(high(count=4, previous=None)).endswith("High 0→4 · 8d")


def test_stale_line() -> None:
    assert render_alert(stale("Project B", age=28)) == "🟡 <b>Project B</b> · no scan 28d"
    assert render_alert(stale("Project B", age=1)) == "🟡 <b>Project B</b> · no scan 1d"


def test_never_ran_line_does_not_depend_on_the_repo_count() -> None:
    expected = "🟡 <b>Project C</b> · no scan yet"
    for repos in (0, 1, 3):
        assert render_alert(never_ran("Project C", repos=repos)) == expected


def test_a_single_alert_is_one_line_in_every_kind() -> None:
    for alert in (high(), stale("B"), never_ran("C")):
        for language in ("en", "ru"):
            assert "\n" not in render_alert(alert, language=language)


def test_marker_comes_from_the_kind_only() -> None:
    assert marker(AlertKind.HIGH_FINDINGS, "en") == marker(AlertKind.HIGH_FINDINGS, "ru") == "🔴"
    for kind in (AlertKind.STALE, AlertKind.NEVER_RAN):
        assert marker(kind, "en") == marker(kind, "ru") == "🟡"


def test_audit_short_strips_the_prefix_and_escapes() -> None:
    assert audit_short("audit.security") == "security"
    assert audit_short("tests") == "tests"
    assert audit_short("audit.<u>&") == "&lt;u&gt;&amp;"


def test_age_text_in_both_languages() -> None:
    assert (age_text(8, "en"), age_text(8, "ru"), age_text(105, "ru")) == ("8d", "8 д", "105 д")


def test_ambiguous_series_needs_two_repos_of_one_project_and_audit() -> None:
    first, second = high(repo="r1"), high(repo="r2")
    other_audit = high(repo="r1", audit="audit.tests")
    assert ambiguous_series([first, second, other_audit]) == {("p1", "audit.security")}
    assert ambiguous_series([first, other_audit]) == frozenset()
    assert ambiguous_series([first, first]) == frozenset()
    assert ambiguous_series([stale("B"), stale("B")]) == frozenset()


def test_high_line_arrow_and_repo_variants() -> None:
    alert = high()
    plain = "🔴 <b>Project A</b> · security High 1 · 8d"
    assert high_line(alert, language="en", show_repo=False, arrow=False) == plain
    assert high_line(alert, language="en", show_repo=False, arrow=True).endswith("High 0→1 · 8d")
    assert high_line(alert, language="en", show_repo=True, arrow=True) == (
        "🔴 <b>Project A</b> · security (acme/r1) High 0→1 · 8d"
    )
    assert high_line(alert, language="ru", show_repo=False, arrow=False) == (
        "🔴 <b>Project A</b> · security высокие 1 · 8 д"
    )


def test_repo_shown_only_for_two_repos_of_one_audit_in_separate_messages() -> None:
    first = high(repo="r1", repo_name="acme/r1")
    second = high(repo="r2", repo_name="acme/r2")
    tests_alert = high(repo="r1", audit="audit.tests", count=2)
    texts = [m.text for m in render_messages([first, second, tests_alert], collapse_above=5)]
    assert texts == [
        "🔴 <b>Project A</b> · security (acme/r1) High 0→1 · 8d",
        "🔴 <b>Project A</b> · tests High 0→2 · 8d",
        "🔴 <b>Project A</b> · security (acme/r2) High 0→1 · 8d",
    ]
    alone = [m.text for m in render_messages([first, tests_alert], collapse_above=5)]
    assert all("acme/" not in text for text in alone)


def test_escaping_name_repo_and_audit_once() -> None:
    first = high('A<b>&"x"', repo="r1", repo_name="r<i>&", audit="audit.<u>")
    second = high('A<b>&"x"', repo="r2", repo_name="r2", audit="audit.<u>")
    line = render_messages([first, second], collapse_above=5)[0].text
    assert line == '🔴 <b>A&lt;b&gt;&amp;"x"</b> · &lt;u&gt; (r&lt;i&gt;&amp;) High 0→1 · 8d'
    assert "&amp;amp;" not in line


def test_secret_gate_withholds_an_alert_whose_project_name_holds_a_token() -> None:
    inner = RecordingNotifier()
    gated = SecretGatedNotifier(inner, ())
    for alert in (high("ghp_" + "a" * 36), stale("ghp_" + "a" * 36)):
        for language in ("en", "ru"):
            (message,) = render_messages([alert], collapse_above=5, language=language)
            outcome = gated.send(message)
            assert isinstance(outcome, NotDelivered)
            assert outcome.error is DeliveryError.BLOCKED_SECRET
    assert inner.sent == []
    (clean,) = render_messages([high()], collapse_above=5)
    assert isinstance(gated.send(clean), Delivered)


def test_both_languages_send_the_same_keys_in_the_same_order() -> None:
    alerts = [stale("B"), high(), never_ran("C"), stale("E", pid="p9", age=105)]
    english = render_messages(alerts, collapse_above=9)
    russian = render_messages(alerts, collapse_above=9, language="ru")
    assert [m.keys for m in english] == [m.keys for m in russian]
    assert [m.text for m in english] != [m.text for m in russian]


def test_summary_item_is_the_single_alert_line() -> None:
    for alert in (stale("B"), never_ran("C"), high(), high(count=3, previous=1)):
        for language in ("en", "ru"):
            assert render_summary_item(alert, language=language) == render_alert(
                alert, language=language
            )
    assert render_summary_item(high(), show_repo=True) == render_alert(high(), show_repo=True)


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
    assert messages[0].text.startswith("Radar · 4 need attention\n")
    assert sorted(k.as_text() for k in messages[0].keys) == sorted(a.key.as_text() for a in alerts)


def test_collapse_header_and_items_worst_first() -> None:
    alerts = [high(), stale("Project E", pid="pe", age=105), stale("Project B", age=28)]
    (message,) = render_messages(alerts, collapse_above=2)
    assert message.text.splitlines() == [
        "Radar · 3 need attention",
        "🔴 <b>Project A</b> · security High 0→1 · 8d",
        "🟡 <b>Project E</b> · no scan 105d",
        "🟡 <b>Project B</b> · no scan 28d",
    ]
    (russian,) = render_messages(alerts, collapse_above=2, language="ru")
    assert russian.text.splitlines()[0] == "Радар · требуют внимания: 3"
    separate = render_messages(alerts, collapse_above=3)
    assert [m.text for m in separate] == message.text.splitlines()[1:]


def test_repo_shown_inside_a_summary_for_two_repos_of_one_audit() -> None:
    first = high(repo="r1", repo_name="acme/r1")
    second = high(repo="r2", repo_name="acme/r2")
    tests_alert = high(repo="r1", audit="audit.tests", count=2)
    (message,) = render_messages([first, second, tests_alert, stale("B")], collapse_above=2)
    lines = message.text.splitlines()
    assert lines[0] == "Radar · 4 need attention"
    assert "🔴 <b>Project A</b> · security (acme/r1) High 0→1 · 8d" in lines
    assert "🔴 <b>Project A</b> · security (acme/r2) High 0→1 · 8d" in lines
    assert "🔴 <b>Project A</b> · tests High 0→2 · 8d" in lines


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
    assert parts[0].text.startswith("Radar · 30 need attention (1/2)\n")
    assert parts[1].text.startswith("Radar · 30 need attention (2/2)\n")
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
        assert all(re.fullmatch(r"🟡 <b>.+</b> · no scan \d+d", line) for line in item_lines)


@pytest.mark.parametrize("language", ["en", "ru"])
def test_split_sixty_long_names_into_numbered_chunks(language: str) -> None:
    alerts = [stale(f"{i:02d}" + "x" * 78, pid=f"p{i:02d}", age=30 + i) for i in range(60)]
    parts = render_messages(alerts, collapse_above=2, max_chars=MIN_MAX_CHARS, language=language)  # type: ignore[arg-type]
    assert len(parts) > 1
    for number, part in enumerate(parts, start=1):
        lines = part.text.splitlines()
        assert lines[0].endswith(f" ({number}/{len(parts)})")
        assert telegram_length(part.text) <= MIN_MAX_CHARS
        assert len(lines) - 1 == len(part.keys)
    assert sorted(k.project_id for k in keys_of(parts)) == sorted(a.key.project_id for a in alerts)


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


@pytest.mark.parametrize("language", ["en", "ru"])
def test_worst_case_high_item_with_repo_fits_the_smallest_message(language: str) -> None:
    name = "\U0001f600" * 80
    first = high(name, repo="r1", repo_name="&" * 80, audit="audit." + "<" * 60, age=36500)
    second = high(name, repo="r2", repo_name="&" * 80, audit="audit." + "<" * 60, age=36500)
    for collapse_above in (5, 1):
        messages = render_messages(
            [first, second],
            collapse_above=collapse_above,
            max_chars=MIN_MAX_CHARS,
            language=language,  # type: ignore[arg-type]
        )
        assert all(telegram_length(m.text) <= MIN_MAX_CHARS for m in messages)
        assert len(keys_of(messages)) == 2


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
