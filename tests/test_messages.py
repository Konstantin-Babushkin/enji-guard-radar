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
    audit_short,
    clean_name,
    marker,
    pack_items,
    project_high_line,
    render_alert,
    render_messages,
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
    assert render_alert(alert) == (
        "🔴 <b>Project A</b>: high problems 1, in audit security · open for 8 days"
    )
    assert render_alert(alert, language="ru") == (
        "🔴 <b>Project A</b>: высоких проблем 1, в аудите security · держится 8 дней"
    )


@pytest.mark.parametrize(
    ("age", "english", "russian"),
    [
        (0, "today", "сегодня"),
        (1, "open for 1 day", "держится 1 день"),
        (2, "open for 2 days", "держится 2 дня"),
        (5, "open for 5 days", "держится 5 дней"),
        (21, "open for 21 days", "держится 21 день"),
    ],
)
def test_alert_line_spells_the_age_out(age: int, english: str, russian: str) -> None:
    alert = high(age=age)
    assert render_alert(alert).endswith(f" · {english}")
    assert render_alert(alert, language="ru").endswith(f" · {russian}")


@pytest.mark.parametrize(
    ("count", "previous", "english", "russian"),
    [
        (3, 1, "high problems 3 (was 1)", "высоких проблем 3 (было 1)"),
        (3, None, "high problems 3,", "высоких проблем 3,"),
        (3, 0, "high problems 3,", "высоких проблем 3,"),
    ],
    ids=["rise", "new series", "was zero"],
)
def test_alert_line_shows_the_rise_only_from_a_previous_count(
    count: int, previous: int | None, english: str, russian: str
) -> None:
    alert = high(count=count, previous=previous)
    assert english in render_alert(alert)
    assert russian in render_alert(alert, language="ru")
    if previous in (None, 0):
        assert "was" not in render_alert(alert)
        assert "было" not in render_alert(alert, language="ru")


def test_alert_line_for_the_project_example_in_both_languages() -> None:
    alert = high("Enji Fleet", audit="audit.dependency-hygiene", count=3, previous=1, age=1)
    assert render_alert(alert, language="ru") == (
        "🔴 <b>Enji Fleet</b>: высоких проблем 3 (было 1), в аудите dependency-hygiene"
        " · держится 1 день"
    )
    assert render_alert(alert) == (
        "🔴 <b>Enji Fleet</b>: high problems 3 (was 1), in audit dependency-hygiene"
        " · open for 1 day"
    )


def test_alert_line_for_several_audits_counts_them_and_names_the_worst() -> None:
    group = [
        high(audit="audit.security", count=1, age=3),
        high(audit="audit.tests", count=4, age=9),
        high(audit="audit.dead-code", count=2, age=5, repo="r2"),
    ]
    assert project_high_line(group, "en") == (
        "🔴 <b>Project A</b>: high problems 7, in 3 audits, mostly tests · open for 9 days"
    )
    assert project_high_line(group, "ru") == (
        "🔴 <b>Project A</b>: высоких проблем 7, в 3 аудитах, больше всего tests · держится 9 дней"
    )


def test_alert_line_for_two_audits_uses_the_plural_of_the_noun() -> None:
    group = [high(audit="audit.security"), high(audit="audit.tests", repo="r2")]
    assert "in 2 audits" in project_high_line(group, "en")
    assert "в 2 аудитах" in project_high_line(group, "ru")


def test_alert_line_leaves_the_worst_audit_out_when_it_would_not_fit_one_line() -> None:
    group = [
        high("Project A", audit="audit.security", count=1),
        high("Project A", audit="audit." + "x" * 60, count=5, repo="r2"),
    ]
    line = project_high_line(group, "en")
    assert line == "🔴 <b>Project A</b>: high problems 6, in 2 audits · open for 8 days"
    assert "x" not in line


def test_alert_line_breaks_a_tie_between_audits_by_name() -> None:
    group = [high(audit="audit.tests", count=2), high(audit="audit.dead-code", count=2, repo="r2")]
    assert "mostly dead-code" in project_high_line(group, "en")


def test_stale_line() -> None:
    assert render_alert(stale("Project B", age=28)) == "🟡 <b>Project B</b>: no scan for 28 days"
    assert render_alert(stale("Project B", age=1)) == "🟡 <b>Project B</b>: no scan for 1 day"
    assert render_alert(stale("Project B", age=2), language="ru") == (
        "🟡 <b>Project B</b>: нет проверок 2 дня"
    )


def test_never_ran_line_does_not_depend_on_the_repo_count() -> None:
    expected = "🟡 <b>Project C</b>: no scan yet"
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


@pytest.mark.parametrize(
    ("days", "english", "russian"),
    [
        (0, "today", "сегодня"),
        (1, "1 day", "1 день"),
        (3, "3 days", "3 дня"),
        (11, "11 days", "11 дней"),
    ],
)
def test_age_text_is_today_or_the_counted_days(days: int, english: str, russian: str) -> None:
    assert (age_text(days, "en"), age_text(days, "ru")) == (english, russian)


@pytest.mark.parametrize("missing", ["age_days", "last_run_at"])
def test_render_alert_rejects_a_stale_alert_without_age_or_last_run(missing: str) -> None:
    alert = stale("B").model_copy(update={missing: None})
    with pytest.raises(ValueError, match=r"^a stale alert needs age_days and last_run_at$"):
        render_alert(alert)


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("age_days", "a High alert needs age_days and count"),
        ("count", "a High alert needs age_days and count"),
        ("seen_since", "a High alert needs seen_since"),
    ],
)
def test_render_alert_rejects_a_high_alert_missing_a_field(missing: str, message: str) -> None:
    alert = high().model_copy(update={missing: None})
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        render_alert(alert)


@pytest.mark.parametrize("missing", ["age_days", "count"])
@pytest.mark.parametrize("bad_first", [True, False])
def test_render_messages_rejects_a_high_alert_without_age_or_count(
    missing: str, bad_first: bool
) -> None:
    good = high(repo="r1", audit="audit.a", age=3)
    bad = high(repo="r2", audit="audit.b", age=9).model_copy(update={missing: None})
    alerts = [bad, good] if bad_first else [good, bad]
    with pytest.raises(ValueError, match=r"^a High alert needs age_days and count$"):
        render_messages(alerts, collapse_above=9)


@pytest.mark.parametrize(
    ("items", "header_units", "max_chars", "expected"),
    [
        ([], 10, 20, []),
        (["aaa", "bbb"], 10, 20, [[0, 1]]),
        (["aaa", "bbb"], 10, 18, [[0, 1]]),
        (["aaa", "bbb"], 10, 17, [[0], [1]]),
        (["a" * 9], 10, 20, [[0]]),
        (["\U0001f600" * 4], 10, 19, [[0]]),
        (["aaa", "bbb", "ccc"], 10, 18, [[0, 1], [2]]),
    ],
    ids=["empty", "fits", "exact", "one-over", "single-exact", "utf16", "three"],
)
def test_pack_items_greedy_chunks(
    items: list[str], header_units: int, max_chars: int, expected: list[list[int]]
) -> None:
    assert pack_items(items, header_units, max_chars) == expected


@pytest.mark.parametrize(
    ("items", "header_units", "max_chars"),
    [
        (["a" * 10], 10, 20),
        (["aa", "a" * 20], 5, 20),
        (["\U0001f600" * 5], 10, 20),
    ],
    ids=["first-one-over", "later-item", "utf16-units"],
)
def test_pack_items_rejects_an_item_that_cannot_fit_with_the_header(
    items: list[str], header_units: int, max_chars: int
) -> None:
    with pytest.raises(ValueError, match=r"^a single summary item does not fit in max_chars$"):
        pack_items(items, header_units, max_chars)


def test_group_message_never_shows_repo_or_audit() -> None:
    first = high(repo="r1", repo_name="acme/r1")
    second = high(repo="r2", repo_name="acme/r2")
    tests_alert = high(repo="r1", audit="audit.tests", count=2)
    (message,) = render_messages([first, second, tests_alert], collapse_above=5)
    assert message.text == (
        "🔴 <b>Project A</b>: high problems 4, in 2 audits, mostly security · open for 8 days"
    )
    assert "acme/" not in message.text
    assert len(message.keys) == 3


def test_escaping_name_once() -> None:
    first = high('A<b>&"x"', repo="r1", repo_name="r<i>&", audit="audit.<u>")
    second = high('A<b>&"x"', repo="r2", repo_name="r2", audit="audit.<u>")
    line = render_messages([first, second], collapse_above=5)[0].text
    assert line == (
        '🔴 <b>A&lt;b&gt;&amp;"x"</b>: high problems 2, in audit &lt;u&gt; · open for 8 days'
    )
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
    assert messages[0].text.startswith("Radar · problems: 4 projects\n")
    assert sorted(k.as_text() for k in messages[0].keys) == sorted(a.key.as_text() for a in alerts)


def test_collapse_header_and_items_worst_first() -> None:
    alerts = [high(), stale("Project E", pid="pe", age=105), stale("Project B", pid="pb", age=28)]
    (message,) = render_messages(alerts, collapse_above=2)
    assert message.text.splitlines() == [
        "Radar · problems: 3 projects",
        "🔴 <b>Project A</b>: high problems 1, in audit security · open for 8 days",
        "🟡 <b>Project E</b>: no scan for 105 days",
        "🟡 <b>Project B</b>: no scan for 28 days",
    ]
    (russian,) = render_messages(alerts, collapse_above=2, language="ru")
    assert russian.text.splitlines()[0] == "Радар · проблемы: 3 проекта"
    separate = render_messages(alerts, collapse_above=3)
    assert [m.text for m in separate] == message.text.splitlines()[1:]


def test_summary_has_one_line_per_project() -> None:
    first = high(repo="r1", repo_name="acme/r1")
    second = high(repo="r2", repo_name="acme/r2")
    tests_alert = high(repo="r1", audit="audit.tests", count=2)
    (message,) = render_messages(
        [first, second, tests_alert, stale("B", pid="pb")], collapse_above=1
    )
    assert message.text.splitlines() == [
        "Radar · problems: 2 projects",
        "🔴 <b>Project A</b>: high problems 4, in 2 audits, mostly security · open for 8 days",
        "🟡 <b>B</b>: no scan for 27 days",
    ]


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
    assert parts[0].text.startswith("Radar · problems: 30 projects (1/2)\n")
    assert parts[1].text.startswith("Radar · problems: 30 projects (2/2)\n")
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
        assert all(re.fullmatch(r"🟡 <b>.+</b>: no scan for \d+ days", line) for line in item_lines)


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
