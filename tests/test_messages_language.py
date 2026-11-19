"""Every message kind in English and Russian: same alerts, same order, other words."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from enji_guard_radar.messages import (
    DEFAULT_MAX_CHARS,
    MIN_MAX_CHARS,
    UNNAMED_PROJECT,
    clean_name,
    days_text,
    high_findings_text,
    render_alert,
    render_messages,
    render_summary_item,
    summary_header,
    telegram_length,
)
from enji_guard_radar.model import Alert, AlertKey, AlertKind, OutboundMessage, Severity
from enji_guard_radar.reminders import render_reminders
from enji_guard_radar.secret_guard import find_secret
from enji_guard_radar.texts import Language

NOW = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
CYRILLIC = re.compile("[А-Яа-яЁё]")
HIGH_WHERE = "audit.security (acme/service-a)"


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

ALERT_TABLE = [
    pytest.param(
        stale(age=1),
        "Stale Guard scans: <b>Project B</b>\n"
        "No completed Guard run for 1 day (last: 2026-11-17).\n"
        "Severity: medium.",
        "Устаревшие проверки Guard: <b>Project B</b>\n"
        "Нет завершённых запусков Guard уже 1 день (последний: 2026-11-17).\n"
        "Серьёзность: средняя.",
        id="stale-1-day",
    ),
    pytest.param(
        stale(age=22),
        "Stale Guard scans: <b>Project B</b>\n"
        "No completed Guard run for 22 days (last: 2026-10-27).\n"
        "Severity: medium.",
        "Устаревшие проверки Guard: <b>Project B</b>\n"
        "Нет завершённых запусков Guard уже 22 дня (последний: 2026-10-27).\n"
        "Серьёзность: средняя.",
        id="stale-22-days",
    ),
    pytest.param(
        never_ran(repos=0),
        "No Guard runs: <b>Project C</b>\nIt has no repositories in Guard.\nSeverity: medium.",
        "Нет запусков Guard: <b>Project C</b>\n"
        "В Guard у проекта нет репозиториев.\n"
        "Серьёзность: средняя.",
        id="never-ran-no-repos",
    ),
    pytest.param(
        never_ran(repos=1),
        "No Guard runs: <b>Project C</b>\n"
        "No completed Guard run found in its 1 repository.\n"
        "Severity: medium.",
        "Нет запусков Guard: <b>Project C</b>\n"
        "Завершённых запусков Guard не найдено в 1 репозитории проекта.\n"
        "Серьёзность: средняя.",
        id="never-ran-1-repo",
    ),
    pytest.param(
        never_ran(repos=5),
        "No Guard runs: <b>Project C</b>\n"
        "No completed Guard run found in its 5 repositories.\n"
        "Severity: medium.",
        "Нет запусков Guard: <b>Project C</b>\n"
        "Завершённых запусков Guard не найдено в 5 репозиториях проекта.\n"
        "Серьёзность: средняя.",
        id="never-ran-5-repos",
    ),
    pytest.param(
        high(1),
        f"High findings: <b>Project A</b>\n1 High finding in {HIGH_WHERE}.\n"
        "Seen by the radar since 2026-11-10 (8 days).\nSeverity: high.",
        f"Находки высокой серьёзности: <b>Project A</b>\n"
        f"1 находка высокой серьёзности в {HIGH_WHERE}.\n"
        "Радар видит с 2026-11-10 (8 дней).\nСерьёзность: высокая.",
        id="high-1",
    ),
    pytest.param(
        high(2),
        f"High findings: <b>Project A</b>\n2 High findings in {HIGH_WHERE}.\n"
        "Seen by the radar since 2026-11-10 (8 days).\nSeverity: high.",
        f"Находки высокой серьёзности: <b>Project A</b>\n"
        f"2 находки высокой серьёзности в {HIGH_WHERE}.\n"
        "Радар видит с 2026-11-10 (8 дней).\nСерьёзность: высокая.",
        id="high-2",
    ),
    pytest.param(
        high(5),
        f"High findings: <b>Project A</b>\n5 High findings in {HIGH_WHERE}.\n"
        "Seen by the radar since 2026-11-10 (8 days).\nSeverity: high.",
        f"Находки высокой серьёзности: <b>Project A</b>\n"
        f"5 находок высокой серьёзности в {HIGH_WHERE}.\n"
        "Радар видит с 2026-11-10 (8 дней).\nСерьёзность: высокая.",
        id="high-5",
    ),
    pytest.param(
        high(3, previous=1),
        f"High findings: <b>Project A</b>\nHigh findings rose from 1 to 3 in {HIGH_WHERE}.\n"
        "Seen by the radar since 2026-11-10 (8 days).\nSeverity: high.",
        f"Находки высокой серьёзности: <b>Project A</b>\n"
        f"Число находок высокой серьёзности выросло с 1 до 3 в {HIGH_WHERE}.\n"
        "Радар видит с 2026-11-10 (8 дней).\nСерьёзность: высокая.",
        id="high-rise",
    ),
    pytest.param(
        high(21, aged=True, age=21),
        f"High findings: <b>Project A</b>\n21 High findings in {HIGH_WHERE}.\n"
        "Long-standing: seen by the radar since 2026-10-28 (21 days).\nSeverity: high.",
        f"Находки высокой серьёзности: <b>Project A</b>\n"
        f"21 находка высокой серьёзности в {HIGH_WHERE}.\n"
        "Давняя проблема: радар видит её с 2026-10-28 (21 день).\nСерьёзность: высокая.",
        id="high-long-standing",
    ),
]


@pytest.mark.parametrize(("alert", "english", "russian"), ALERT_TABLE)
def test_render_alert_in_both_languages(alert: Alert, english: str, russian: str) -> None:
    assert render_alert(alert) == english
    assert render_alert(alert, language="en") == english
    assert render_alert(alert, language="ru") == russian


SUMMARY_TABLE = [
    pytest.param(
        stale(age=5),
        "- <b>Project B</b>: no completed Guard run for 5 days (medium)",
        "- <b>Project B</b>: нет завершённых запусков Guard уже 5 дней (средняя)",
        id="stale",
    ),
    pytest.param(
        never_ran(repos=2),
        "- <b>Project C</b>: no completed Guard run found (medium)",
        "- <b>Project C</b>: завершённых запусков Guard не найдено (средняя)",
        id="never-ran",
    ),
    pytest.param(
        high(3),
        f"- <b>Project A</b>: 3 High in {HIGH_WHERE} (high)",
        f"- <b>Project A</b>: находок высокой серьёзности: 3 в {HIGH_WHERE} (высокая)",
        id="high",
    ),
    pytest.param(
        high(3, previous=1),
        f"- <b>Project A</b>: High rose from 1 to 3 in {HIGH_WHERE} (high)",
        "- <b>Project A</b>: число находок высокой серьёзности выросло "
        f"с 1 до 3 в {HIGH_WHERE} (высокая)",
        id="high-rise",
    ),
]


@pytest.mark.parametrize(("alert", "english", "russian"), SUMMARY_TABLE)
def test_render_summary_item_in_both_languages(alert: Alert, english: str, russian: str) -> None:
    assert render_summary_item(alert) == english
    assert render_summary_item(alert, language="ru") == russian


@pytest.mark.parametrize(
    ("total", "index", "count", "english", "russian"),
    [
        (5, 1, 1, "5 problems need attention", "требуется внимание — 5 проблем"),
        (21, 2, 3, "21 problems need attention (2/3)", "требуется внимание — 21 проблема (2/3)"),
        (22, 1, 2, "22 problems need attention (1/2)", "требуется внимание — 22 проблемы (1/2)"),
    ],
)
def test_summary_header_in_both_languages(
    total: int, index: int, count: int, english: str, russian: str
) -> None:
    assert summary_header(total, index, count) == f"Guard radar: {english}"
    assert summary_header(total, index, count, language="ru") == f"Guard radar: {russian}"


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


@pytest.mark.parametrize(
    ("count", "russian"),
    [
        (1, "1 находка высокой серьёзности"),
        (2, "2 находки высокой серьёзности"),
        (5, "5 находок высокой серьёзности"),
        (11, "11 находок высокой серьёзности"),
        (12, "12 находок высокой серьёзности"),
        (21, "21 находка высокой серьёзности"),
        (22, "22 находки высокой серьёзности"),
        (25, "25 находок высокой серьёзности"),
    ],
)
def test_high_findings_text_in_russian(count: int, russian: str) -> None:
    assert high_findings_text(count, language="ru") == russian
    assert high_findings_text(count) == (
        "1 High finding" if count == 1 else f"{count} High findings"
    )


@pytest.mark.parametrize(
    ("repos", "russian"),
    [(1, "1 репозитории"), (2, "2 репозиториях"), (21, "21 репозитории"), (25, "25 репозиториях")],
)
def test_repositories_use_the_prepositional_case(repos: int, russian: str) -> None:
    assert russian in render_alert(never_ran(repos=repos), language="ru")


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
        render_summary_item(alert, language="ru"),
        render_alert(stale(nasty), language="ru"),
    ):
        assert 'Проект &lt;b&gt;&amp; "Ко"' in text
        assert "&amp;amp;" not in text
        assert not re.search(r"<(?!/?b>)", text)
    assert "acme/&lt;web&gt;&amp;co" in render_alert(alert, language="ru")


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
        assert part.text.startswith(
            f"Guard radar: требуется внимание — 60 проблем ({number}/{len(parts)})\n"
        )
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
        assert all(line.startswith("- <b>") and line.endswith(")") for line in lines)
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
        item = render_summary_item(alert, language="ru")
        parts = render_messages(
            [alert, *[stale("o", pid=f"o{i}") for i in range(3)]],
            collapse_above=3,
            max_chars=MIN_MAX_CHARS,
            language="ru",
        )
        assert telegram_length(item) < MIN_MAX_CHARS
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
        "<b>Reminder: High findings are still open</b>\n"
        f"- <b>Project A</b>: 1 High finding in {HIGH_WHERE}. "
        "Seen by the radar since 2026-11-10 (8 days).\n"
        f"- <b>Ёлка &amp; Ко</b>: 5 High findings in {HIGH_WHERE}. "
        "Seen by the radar since 2026-10-27 (22 days)."
    )
    assert russian.text == (
        "<b>Напоминание: находки высокой серьёзности всё ещё открыты</b>\n"
        f"- <b>Project A</b>: 1 находка высокой серьёзности в {HIGH_WHERE}. "
        "Радар видит с 2026-11-10 (8 дней).\n"
        f"- <b>Ёлка &amp; Ко</b>: 5 находок высокой серьёзности в {HIGH_WHERE}. "
        "Радар видит с 2026-10-27 (22 дня)."
    )
    assert english.keys == russian.keys == tuple(a.key for a in alerts)


@pytest.mark.parametrize(
    ("count", "phrase"),
    [
        (1, "1 находка"),
        (2, "2 находки"),
        (11, "11 находок"),
        (21, "21 находка"),
        (22, "22 находки"),
    ],
)
def test_reminder_findings_plural_in_russian(count: int, phrase: str) -> None:
    (message,) = render_reminders([high(count)], language="ru")
    assert phrase in message.text


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
        title = "Напоминание: находки высокой серьёзности всё ещё открыты"
        assert message.text.startswith(f"<b>{title} ({number}/{len(russian)})</b>\n")
        assert telegram_length(message.text) <= MIN_MAX_CHARS
        assert len(message.text.split("\n")) - 1 == len(message.keys)
        assert find_secret(message.text, []) is None
