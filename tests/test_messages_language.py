"""Every message kind in English and Russian: same alerts, same order, other words."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from enji_guard_radar.digest import DigestInput, build_digest, render_digest
from enji_guard_radar.guard_auth import AUTH_FAILURE_TEXT
from enji_guard_radar.health import Outage, Recovered, ops_message
from enji_guard_radar.high_rules import LowSeries
from enji_guard_radar.ledger import ResolvedItem
from enji_guard_radar.messages import (
    DEFAULT_MAX_CHARS,
    MIN_MAX_CHARS,
    UNNAMED_PROJECT,
    clean_name,
    days_text,
    render_alert,
    render_messages,
    summary_header,
    telegram_length,
)
from enji_guard_radar.model import Alert, AlertKey, AlertKind, OutboundMessage, Severity
from enji_guard_radar.pipeline import ops_message_for_actions, ops_message_for_halt
from enji_guard_radar.ports import AuthFailureKind, GuardFailure
from enji_guard_radar.reminders import render_reminders
from enji_guard_radar.secret_guard import find_secret
from enji_guard_radar.texts import ActionCode, ActionNotice, Language, render_action

NOW = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
CYRILLIC = re.compile("[А-Яа-яЁё]")


def stale(name: str = "Project B", *, pid: str = "p1", age: int = 27) -> Alert:
    return Alert(
        key=AlertKey(project_id=pid, level="stale"),
        kind=AlertKind.STALE,
        project_name=name,
        severity=Severity.MEDIUM,
        age_days=age,
        last_run_at=NOW - timedelta(days=age),
        repo_count=1,
    )


def never_ran(name: str = "Project C", *, pid: str = "p2", repos: int = 0) -> Alert:
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
    count: int = 3,
    *,
    previous: int | None = None,
    aged: bool = False,
    age: int = 8,
    name: str = "Project A",
    pid: str = "p3",
    repo: str = "acme/service-a",
) -> Alert:
    return Alert(
        key=AlertKey(project_id=pid, repo_id="r1", action_key="audit.security", level="high"),
        kind=AlertKind.HIGH_FINDINGS,
        project_name=name,
        severity=Severity.HIGH,
        age_days=age,
        last_run_at=None,
        repo_count=1,
        count=count,
        repo_name=repo,
        previous_count=previous,
        seen_since=NOW - timedelta(days=age),
        aged=aged,
    )


def keys_of(messages: list[OutboundMessage]) -> list[AlertKey]:
    return [key for message in messages for key in message.keys]


# --- one alert per kind, both languages ---

HIGH_ONE = "🔴 <b>Project A</b>: high problems {count}{rise}, in audit security · open for {age}"
HIGH_ONE_RU = (
    "🔴 <b>Project A</b>: высоких проблем {count}{rise}, в аудите security · держится {age}"
)

ALERT_TABLE = [
    pytest.param(
        stale(age=1),
        "🟡 <b>Project B</b>: no scan for 1 day",
        "🟡 <b>Project B</b>: нет проверок 1 день",
        id="stale-1-day",
    ),
    pytest.param(
        stale(age=22),
        "🟡 <b>Project B</b>: no scan for 22 days",
        "🟡 <b>Project B</b>: нет проверок 22 дня",
        id="stale-22-days",
    ),
    pytest.param(
        never_ran(repos=0),
        "🟡 <b>Project C</b>: no scan yet",
        "🟡 <b>Project C</b>: проверок ещё не было",
        id="never-ran-no-repos",
    ),
    pytest.param(
        never_ran(repos=5),
        "🟡 <b>Project C</b>: no scan yet",
        "🟡 <b>Project C</b>: проверок ещё не было",
        id="never-ran-5-repos",
    ),
    pytest.param(
        high(1),
        HIGH_ONE.format(count=1, rise="", age="8 days"),
        HIGH_ONE_RU.format(count=1, rise="", age="8 дней"),
        id="high-new-series",
    ),
    pytest.param(
        high(5),
        HIGH_ONE.format(count=5, rise="", age="8 days"),
        HIGH_ONE_RU.format(count=5, rise="", age="8 дней"),
        id="high-5",
    ),
    pytest.param(
        high(3, previous=1),
        HIGH_ONE.format(count=3, rise=" (was 1)", age="8 days"),
        HIGH_ONE_RU.format(count=3, rise=" (было 1)", age="8 дней"),
        id="high-rise",
    ),
    pytest.param(
        high(21, aged=True, age=21),
        HIGH_ONE.format(count=21, rise="", age="21 days"),
        HIGH_ONE_RU.format(count=21, rise="", age="21 день"),
        id="high-aged-looks-the-same",
    ),
    pytest.param(
        high(2, age=0),
        "🔴 <b>Project A</b>: high problems 2, in audit security · today",
        "🔴 <b>Project A</b>: высоких проблем 2, в аудите security · сегодня",
        id="high-seen-today",
    ),
]


@pytest.mark.parametrize(("alert", "english", "russian"), ALERT_TABLE)
def test_render_alert_in_both_languages(alert: Alert, english: str, russian: str) -> None:
    assert render_alert(alert) == english
    assert render_alert(alert, language="en") == english
    assert render_alert(alert, language="ru") == russian


LINE_TABLE = [
    pytest.param(
        stale(age=5),
        "🟡 <b>Project B</b>: no scan for 5 days",
        "🟡 <b>Project B</b>: нет проверок 5 дней",
        id="stale",
    ),
    pytest.param(
        never_ran(repos=2),
        "🟡 <b>Project C</b>: no scan yet",
        "🟡 <b>Project C</b>: проверок ещё не было",
        id="never-ran",
    ),
    pytest.param(
        high(3),
        HIGH_ONE.format(count=3, rise="", age="8 days"),
        HIGH_ONE_RU.format(count=3, rise="", age="8 дней"),
        id="high",
    ),
    pytest.param(
        high(3, previous=1),
        HIGH_ONE.format(count=3, rise=" (was 1)", age="8 days"),
        HIGH_ONE_RU.format(count=3, rise=" (было 1)", age="8 дней"),
        id="high-rise",
    ),
]


@pytest.mark.parametrize(("alert", "english", "russian"), LINE_TABLE)
def test_group_line_in_both_languages(alert: Alert, english: str, russian: str) -> None:
    assert [m.text for m in render_messages([alert], collapse_above=3)] == [english]
    assert [m.text for m in render_messages([alert], collapse_above=3, language="ru")] == [russian]


@pytest.mark.parametrize(
    ("total", "index", "count", "english", "russian"),
    [
        (1, 1, 1, "Radar · problems: 1 project", "Радар · проблемы: 1 проект"),
        (5, 1, 1, "Radar · problems: 5 projects", "Радар · проблемы: 5 проектов"),
        (21, 2, 3, "Radar · problems: 21 projects (2/3)", "Радар · проблемы: 21 проект (2/3)"),
        (22, 1, 2, "Radar · problems: 22 projects (1/2)", "Радар · проблемы: 22 проекта (1/2)"),
    ],
)
def test_summary_header_in_both_languages(
    total: int, index: int, count: int, english: str, russian: str
) -> None:
    assert summary_header(total, index, count) == english
    assert summary_header(total, index, count, language="ru") == russian


@pytest.mark.parametrize(
    ("count", "english", "russian"),
    [
        (1, "1 day", "1 день"),
        (2, "2 days", "2 дня"),
        (5, "5 days", "5 дней"),
        (11, "11 days", "11 дней"),
        (12, "12 days", "12 дней"),
        (21, "21 days", "21 день"),
        (22, "22 days", "22 дня"),
        (25, "25 days", "25 дней"),
    ],
)
def test_days_text_in_both_languages(count: int, english: str, russian: str) -> None:
    assert days_text(count) == english
    assert days_text(count, language="ru") == russian


# --- names ---


def test_unnamed_project_is_shown_in_the_message_language() -> None:
    assert clean_name("  ") == UNNAMED_PROJECT == "(unnamed project)"
    assert clean_name("  ", language="ru") == "(проект без названия)"
    assert "<b>(проект без названия)</b>" in render_alert(stale("  "), language="ru")
    assert "<b>(unnamed project)</b>" in render_alert(stale("  "))


def test_html_in_names_is_escaped_once_in_russian() -> None:
    nasty = 'Проект <b>& "Ко"'
    alert = high(name=nasty, repo="acme/<web>&co")
    for text in (
        render_alert(alert, language="ru"),
        render_messages([alert], collapse_above=3, language="ru")[0].text,
        render_alert(stale(nasty), language="ru"),
    ):
        assert 'Проект &lt;b&gt;&amp; "Ко"' in text
        assert "&amp;amp;" not in text
        assert not re.search(r"<(?!/?b>)", text)


# --- ordering and the same set of alerts ---


def mixed() -> list[Alert]:
    return [
        stale("Zeta", pid="s1", age=40),
        stale("Alpha", pid="s2", age=40),
        never_ran("  ", pid="n1", repos=2),
        never_ran("Beta", pid="n2", repos=0),
        high(2, pid="h1", name="Gamma"),
        high(3, previous=1, pid="h2", name="Delta"),
        stale("Eta", pid="s3", age=3),
    ]


@pytest.mark.parametrize("collapse_above", [1, 3, 20])
def test_both_languages_send_the_same_alerts_in_the_same_order(collapse_above: int) -> None:
    english = render_messages(mixed(), collapse_above=collapse_above)
    russian = render_messages(mixed(), collapse_above=collapse_above, language="ru")
    assert len(english) == len(russian)
    assert keys_of(english) == keys_of(russian)
    assert [m.keys for m in english] == [m.keys for m in russian]
    assert all(CYRILLIC.search(m.text) for m in russian)
    assert not any(CYRILLIC.search(m.text) for m in english)


def test_unnamed_project_sorts_the_same_in_both_languages() -> None:
    alerts = [never_ran("  ", pid="a"), never_ran("Ёлка", pid="b"), never_ran("Apple", pid="c")]
    en = [k.project_id for k in keys_of(render_messages(alerts, collapse_above=9))]
    ru = [k.project_id for k in keys_of(render_messages(alerts, collapse_above=9, language="ru"))]
    assert en == ru


def test_summary_is_split_with_numbered_russian_headers() -> None:
    alerts = [stale(f"Проект {i:03d}", pid=f"p{i:03d}", age=10 + i) for i in range(60)]
    parts = render_messages(alerts, collapse_above=3, max_chars=MIN_MAX_CHARS, language="ru")
    assert len(parts) > 2
    for number, part in enumerate(parts, start=1):
        assert part.text.startswith(f"Радар · проблемы: 60 проектов ({number}/{len(parts)})\n")
        assert telegram_length(part.text) <= MIN_MAX_CHARS
        assert len(part.text.split("\n")) - 1 == len(part.keys)
    keys = keys_of(parts)
    assert len(keys) == len(set(keys)) == 60


# --- Cyrillic splitting near the limit ---


def cyrillic_alerts(count: int) -> list[Alert]:
    return [
        stale(
            ("Проект «Ёлка» & <тест> " + str(i) + " ").ljust(80, "Ж"), pid=f"p{i:03d}", age=14 + i
        )
        for i in range(count)
    ]


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("max_chars", [MIN_MAX_CHARS, 2000, DEFAULT_MAX_CHARS])
def test_cyrillic_names_split_between_items_within_the_limit(
    language: Language, max_chars: int
) -> None:
    alerts = cyrillic_alerts(60)
    parts = render_messages(alerts, collapse_above=3, max_chars=max_chars, language=language)
    assert len(parts) > 1
    assert all(telegram_length(p.text) <= max_chars for p in parts)
    keys = keys_of(parts)
    assert len(keys) == len(set(keys)) == 60
    for part in parts:  # whole items only: one line per key, each ending in its closing bracket
        lines = part.text.split("\n")[1:]
        assert len(lines) == len(part.keys)
        stale_line = r"🟡 <b>.+</b>: (no scan for \d+ days|нет проверок \d+ (день|дня|дней))"
        assert all(re.fullmatch(stale_line, line) for line in lines)
        assert "&lt;тест&gt;" in part.text
        assert "<тест>" not in part.text


def test_telegram_length_counts_cyrillic_as_one_unit_per_character() -> None:
    assert telegram_length("Привет") == len("Привет") == 6
    assert telegram_length("Проект") * 2 == telegram_length("ПроектПроект")


@pytest.mark.parametrize("name", ["&" * 80, "<" * 80, "Ж" * 80])
def test_russian_worst_case_item_fits_the_minimum_message_size(name: str) -> None:
    alerts = [
        stale(name, age=36500),
        never_ran(name, repos=36500),
        high(99999, previous=99998, aged=True, age=36500, name=name, repo=name),
    ]
    for alert in alerts:
        assert telegram_length(render_alert(alert, language="ru")) <= MIN_MAX_CHARS
        parts = render_messages(
            [alert, *[stale("o", pid=f"o{i}") for i in range(3)]],
            collapse_above=3,
            max_chars=MIN_MAX_CHARS,
            language="ru",
        )
        assert all(telegram_length(p.text) <= MIN_MAX_CHARS for p in parts)


# --- the secret gate ---


def test_russian_texts_do_not_trip_the_secret_gate() -> None:
    kinds = [stale(), never_ran(repos=0), never_ran(repos=3), high(1), high(3, previous=1)]
    texts = [render_alert(a, language="ru") for a in kinds]
    texts += [m.text for m in render_messages(mixed(), collapse_above=1, language="ru")]
    texts += [m.text for m in render_messages(mixed(), collapse_above=20, language="ru")]
    assert texts
    assert all(find_secret(text, []) is None for text in texts)


# --- reminders ---


def test_reminder_in_both_languages() -> None:
    alerts = [high(1, pid="a", age=8), high(5, pid="b", age=22, name="Ёлка & Ко")]
    (english,) = render_reminders(alerts)
    (russian,) = render_reminders(alerts, language="ru")
    assert english.text == (
        "<b>Radar reminder · open: 2 projects</b>\n"
        "🔴 <b>Project A</b>: high problems 1, in audit security · open for 8 days\n"
        "🔴 <b>Ёлка &amp; Ко</b>: high problems 5, in audit security · open for 22 days"
    )
    assert russian.text == (
        "<b>Радар · напоминание · открыто: 2 проекта</b>\n"
        "🔴 <b>Project A</b>: высоких проблем 1, в аудите security · держится 8 дней\n"
        "🔴 <b>Ёлка &amp; Ко</b>: высоких проблем 5, в аудите security · держится 22 дня"
    )
    assert english.keys == russian.keys == tuple(a.key for a in alerts)


def test_reminder_split_keeps_every_series_once_in_order_in_russian() -> None:
    alerts = [
        high(2, pid=f"p{i:03d}", age=10 + i, name=("Проект & " + str(i)).ljust(80, "Ж"))
        for i in range(40)
    ]
    english = render_reminders(alerts, max_chars=MIN_MAX_CHARS)
    russian = render_reminders(alerts, max_chars=MIN_MAX_CHARS, language="ru")
    assert len(russian) > 1
    assert keys_of(russian) == keys_of(english) == [a.key for a in alerts]
    for number, message in enumerate(russian, start=1):
        title = "Радар · напоминание · открыто: 40 проектов"
        assert message.text.startswith(f"<b>{title} ({number}/{len(russian)})</b>\n")
        assert telegram_length(message.text) <= MIN_MAX_CHARS
        assert len(message.text.split("\n")) - 1 == len(message.keys)
        assert find_secret(message.text, []) is None


# --- the weekly digest ---

POLL = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)


def low_series(name: str = "Gamma") -> LowSeries:
    return LowSeries(
        project_id="p4",
        project_name=name,
        repo_id="r4",
        repo_name="acme/web",
        action_key="audit.tests",
        high_now=4,
        opened_at=POLL - timedelta(days=8),
    )


def closed(level: str, pid: str = "p1", days: int = 2) -> ResolvedItem:
    high_level = level == "high"
    return ResolvedItem(
        key=AlertKey(
            project_id=pid,
            repo_id="r1" if high_level else "*",
            action_key="audit.security" if high_level else "*",
            level=level,
        ),
        resolved_at=POLL - timedelta(days=days),
    )


def full_input(**more: object) -> DigestInput:
    base: dict[str, object] = {
        "last_poll_at": POLL,
        "max_items": 50,
        "high": (high(2, pid="h1", age=16, name="Acme <web>"),),
        "low_priority": (low_series(),),
        "stale": (stale("Beta", pid="s1", age=21), never_ran("Nova", pid="n1", repos=1)),
        "resolved": (closed("high"), closed("stale", "p2"), closed("never_ran", "p3")),
        "clean_projects": ("c1",),
        "project_names": {"c1": "Delta"},
        "unread_projects": 2,
    }
    return DigestInput.model_validate(base | more)


def digest_text(inp: DigestInput, language: Language, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    messages = render_digest(
        build_digest(inp, language=language), language=language, max_chars=max_chars
    )
    assert all(m.audience.value == "group" and m.keys == () for m in messages)
    return "\n\n".join(m.text for m in messages)


def test_full_digest_in_english() -> None:
    assert digest_text(full_input(), "en") == "\n".join(
        [
            "<b>Radar · weekly digest</b>",
            "High problems: 6 in 2 projects, closed this week: 1.",
            "🔴 <b>Acme &lt;web&gt;</b> — 2 high problems; most in: security; 16 days",
            "🟡 <b>Gamma</b> — 4 high problems, low priority; most in: tests; 8 days",
            "🟡 <b>Beta</b> — no scan for 21 days",
            "🟡 <b>Nova</b> — no scan yet",
            "🟢 <b>Delta</b> — clean",
            "⚠️ Could not read 2 projects · poll 2026-11-18 06:00 UTC",
        ]
    )


def test_full_digest_in_russian() -> None:
    assert digest_text(full_input(), "ru") == "\n".join(
        [
            "<b>Радар · недельная сводка</b>",
            "Высоких проблем: 6 в 2 проектах, закрыто за неделю: 1.",
            "🔴 <b>Acme &lt;web&gt;</b> — 2 высокие проблемы; больше всего: безопасность; 16 дней",
            "🟡 <b>Gamma</b> — 4 высокие проблемы, низкий приоритет; больше всего: тесты; 8 дней",
            "🟡 <b>Beta</b> — нет проверок 21 день",
            "🟡 <b>Nova</b> — проверок ещё не было",
            "🟢 <b>Delta</b> — чисто",
            "⚠️ Не удалось прочитать 2 проекта · опрос 2026-11-18 06:00 UTC",
        ]
    )


@pytest.mark.parametrize(
    ("hidden", "english", "russian"),
    [
        (1, "and 1 more project clean", "и ещё 1 проект чисто"),
        (2, "and 2 more projects clean", "и ещё 2 проекта чисто"),
        (5, "and 5 more projects clean", "и ещё 5 проектов чисто"),
        (11, "and 11 more projects clean", "и ещё 11 проектов чисто"),
        (21, "and 21 more projects clean", "и ещё 21 проект чисто"),
        (22, "and 22 more projects clean", "и ещё 22 проекта чисто"),
    ],
)
def test_digest_clean_more_line_plurals(hidden: int, english: str, russian: str) -> None:
    ids = tuple(f"c{i:03d}" for i in range(hidden + 2))
    inp = full_input(max_items=2, clean_projects=ids, project_names=dict.fromkeys(ids, "X"))
    assert english in digest_text(inp, "en").split("\n")
    assert russian in digest_text(inp, "ru").split("\n")


@pytest.mark.parametrize(
    ("unread", "english", "russian"),
    [
        (1, "1 project", "1 проект"),
        (2, "2 projects", "2 проекта"),
        (5, "5 projects", "5 проектов"),
        (21, "21 projects", "21 проект"),
    ],
)
def test_digest_unread_note_plurals(unread: int, english: str, russian: str) -> None:
    inp = full_input(unread_projects=unread)
    assert f"⚠️ Could not read {english} · poll 2026-11-18 06:00 UTC" in digest_text(
        inp, "en"
    ).split("\n")
    assert f"⚠️ Не удалось прочитать {russian} · опрос 2026-11-18 06:00 UTC" in digest_text(
        inp, "ru"
    ).split("\n")


EN_CLEAR = "<b>Radar · weekly digest</b> · nothing open · last poll 2026-11-18 06:00 UTC"
RU_CLEAR = (
    "<b>Радар · недельная сводка</b> · ничего не открыто · последний опрос 2026-11-18 06:00 UTC"
)


def test_digest_all_clear() -> None:
    inp = DigestInput(last_poll_at=POLL, max_items=10)
    assert digest_text(inp, "en") == EN_CLEAR
    assert digest_text(inp, "ru") == RU_CLEAR


def test_digest_all_clear_with_unread_projects() -> None:
    inp = DigestInput(last_poll_at=POLL, max_items=10, unread_projects=3)
    assert digest_text(inp, "en") == (
        "<b>Radar · weekly digest</b> · nothing open in the projects read"
        " · last poll 2026-11-18 06:00 UTC\n"
        "⚠️ Could not read 3 projects"
    )
    assert digest_text(inp, "ru") == (
        "<b>Радар · недельная сводка</b> · ничего не открыто в прочитанных проектах"
        " · последний опрос 2026-11-18 06:00 UTC\n"
        "⚠️ Не удалось прочитать 3 проекта"
    )


def test_digest_has_the_same_projects_in_the_same_order_in_both_languages() -> None:
    inp = full_input()
    english = build_digest(inp)
    russian = build_digest(inp, language="ru")
    assert [line[:1] for line in english.project_lines] == [
        line[:1] for line in russian.project_lines
    ]
    assert english.model_dump(exclude={"project_lines", "clean_lines", "language"}) == (
        russian.model_dump(exclude={"project_lines", "clean_lines", "language"})
    )


def test_digest_split_with_cyrillic_names_is_numbered_and_whole() -> None:
    names = [("Проект & <тест> " + str(i)).ljust(80, "Ж") for i in range(40)]
    inp = full_input(
        high=(),
        low_priority=(),
        stale=tuple(stale(n, pid=f"s{i:02d}", age=10 + i) for i, n in enumerate(names)),
        clean_projects=(),
        resolved=(),
        unread_projects=0,
    )
    digest = build_digest(inp, language="ru")
    messages = render_digest(digest, max_chars=MIN_MAX_CHARS, language="ru")
    assert len(messages) > 1
    for number, message in enumerate(messages, start=1):
        assert message.text.startswith(
            f"<b>Радар · недельная сводка ({number}/{len(messages)})</b>\n"
            "Открытых высоких проблем нет, закрыто за неделю: 0.\n"
        )
        assert telegram_length(message.text) <= MIN_MAX_CHARS
        assert find_secret(message.text, []) is None
        assert not re.search(r"<(?!/?b>)", message.text)
    body = [line for m in messages for line in m.text.split("\n")[2:]]
    assert sum(line.startswith("🟡 <b>Проект &amp; &lt;тест&gt;") for line in body) == 40


# --- ops chat ---

SINCE = datetime(2026, 11, 18, 5, 0, tzinfo=UTC)
RAW_LT = re.compile(r"<(?!/?b>)")


def outage(failures: int = 3, cause: GuardFailure = GuardFailure.UNAVAILABLE) -> Outage:
    return Outage(cause=cause, auth_kind=None, since=SINCE, failures=failures)


def test_outage_message_in_both_languages() -> None:
    assert ops_message(outage()).text == (
        "Guard radar cannot read Guard: Guard is unreachable or failing. "
        "Failing since 2026-11-18 05:00 UTC (3 failed polls in a row). "
        "The radar keeps polling and reports here once when it recovers."
    )
    assert ops_message(outage(), language="ru").text == (
        "Guard radar не может прочитать данные Guard: Guard недоступен или сбоит. "
        "Сбои с 2026-11-18 05:00 UTC (3 неудачных опроса подряд). "
        "Радар продолжает опрос и один раз сообщит здесь, когда связь восстановится."
    )


@pytest.mark.parametrize(
    ("failures", "russian"),
    [
        (1, "1 неудачный опрос"),
        (2, "2 неудачных опроса"),
        (5, "5 неудачных опросов"),
        (11, "11 неудачных опросов"),
        (21, "21 неудачный опрос"),
        (22, "22 неудачных опроса"),
        (25, "25 неудачных опросов"),
    ],
)
def test_outage_failed_poll_plurals(failures: int, russian: str) -> None:
    assert f"({russian} подряд)" in ops_message(outage(failures), language="ru").text
    assert f"({failures} failed polls in a row)" in ops_message(outage(failures)).text


def test_recovery_message_in_both_languages() -> None:
    assert ops_message(Recovered(since=SINCE)).text == (
        "Guard radar recovered: Guard polls work again (failing since 2026-11-18 05:00 UTC)."
    )
    assert ops_message(Recovered(since=SINCE), language="ru").text == (
        "Guard radar восстановился: опросы Guard снова работают (сбои с 2026-11-18 05:00 UTC)."
    )


@pytest.mark.parametrize("kind", list(AuthFailureKind))
def test_auth_outage_reason_in_both_languages(kind: AuthFailureKind) -> None:
    action = Outage(cause=GuardFailure.AUTH, auth_kind=kind, since=SINCE, failures=3)
    english = ops_message(action).text
    russian = ops_message(action, language="ru").text
    assert english.startswith(f"Guard radar cannot read Guard: {AUTH_FAILURE_TEXT[kind]}.")
    assert "`enji-guard-radar auth`" in russian
    assert CYRILLIC.search(russian.split(": ", 1)[1])
    assert not re.search("Guard login|Guard refused", russian)


def test_auth_outage_without_a_kind_and_data_outage_in_russian() -> None:
    unknown = Outage(cause=GuardFailure.AUTH, auth_kind=None, since=SINCE, failures=3)
    assert "проблема со входом в Guard: выполните `enji-guard-radar auth`" in (
        ops_message(unknown, language="ru").text
    )
    assert (
        "посмотрите лог радара" in ops_message(outage(cause=GuardFailure.DATA), language="ru").text
    )


def test_halt_message_in_both_languages_escapes_the_reason_once() -> None:
    english = ops_message_for_halt("bad <json> & more", True)
    russian = ops_message_for_halt("bad <json> & more", False, language="ru")
    assert english.text == (
        "Guard radar halted: the state file is corrupt (bad &lt;json&gt; &amp; more). "
        "Nothing was sent and nothing was reset. Backup valid: yes. "
        "Check the file and restore the backup by hand only if it is right."
    )
    assert russian.text == (
        "Guard radar остановлен: файл состояния повреждён (bad &lt;json&gt; &amp; more). "
        "Ничего не отправлено и ничего не сброшено. Резервная копия цела: нет. "
        "Проверьте файл и восстановите копию вручную, только если она верна."
    )
    assert english.audience.value == russian.audience.value == "ops"


def test_action_notice_message_in_both_languages() -> None:
    notices = (
        ActionNotice(
            code=ActionCode.CHAT_FORBIDDEN,
            params={"chat": "-100123", "env": "TELEGRAM_CHAT_ID"},
        ),
        ActionNotice(code=ActionCode.BAD_REQUEST, params={"description": "Bad Request: <b> & co"}),
        None,
    )
    actions = tuple(
        a if n is None else render_action(n, "en")
        for a, n in zip(("", "", "check the bot token <now>"), notices, strict=True)
    )
    english = ops_message_for_actions(actions, notices=notices)
    russian = ops_message_for_actions(actions, notices=notices, language="ru")
    assert english.text == (
        "Guard radar needs attention:\n"
        "- Telegram refused to post to chat -100123 (HTTP 403): the bot was removed, blocked or "
        "cannot post there; add it back or fix TELEGRAM_CHAT_ID\n"
        "- Telegram rejected the message: Bad Request: &lt;b&gt; &amp; co\n"
        "- check the bot token &lt;now&gt;"
    )
    assert russian.text == (
        "Guard radar: нужно вмешательство оператора:\n"
        "- Telegram не разрешил писать в чат -100123 (HTTP 403): бота удалили, заблокировали "
        "или он не может там писать; верните его в чат или поправьте TELEGRAM_CHAT_ID\n"
        "- Telegram отклонил сообщение: Bad Request: &lt;b&gt; &amp; co\n"
        "- check the bot token &lt;now&gt;"
    )


def test_plain_action_strings_keep_the_old_behaviour() -> None:
    assert ops_message_for_actions(("a <b>", "c")).text == (
        "Guard radar needs attention:\n- a &lt;b&gt;\n- c"
    )


def test_every_ops_text_is_clean() -> None:
    notice = ActionNotice(code=ActionCode.SECRET_WITHHELD)
    messages = [ops_message(outage(), language=language) for language in ("en", "ru")] + [
        ops_message_for_actions(("x",), notices=(notice,), language="ru"),
        ops_message_for_halt("r", True, language="ru"),
        ops_message(Recovered(since=SINCE), language="ru"),
    ]
    for message in messages:
        assert not RAW_LT.search(message.text)
        assert find_secret(message.text, []) is None


@pytest.mark.parametrize("code", list(ActionCode))
def test_every_action_notice_renders_in_both_languages(code: ActionCode) -> None:
    params = {"code": "401", "env": "E", "chat": "-1", "old": "-1", "new": "-2", "description": "d"}
    notice = ActionNotice(code=code, params=params)
    assert not CYRILLIC.search(render_action(notice, "en"))
    assert CYRILLIC.search(render_action(notice, "ru"))
