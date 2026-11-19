"""The strings of every Telegram message, per language (pure: no I/O, no clock).

Each catalog maps a dotted key to a `str.format` template or, for a counted noun, to a tuple of
bare noun forms: English has two (one, other), Russian three (one, few, many). Code formats the
numbers, dates and names and passes them in already escaped; a template holds fixed text and the
literal `<b>` / `</b>` tags only. `parity_problems` keeps the two catalogs in step.
"""

import string
from collections.abc import Mapping
from typing import Literal

Language = Literal["en", "ru"]
DEFAULT_LANGUAGE: Language = "en"

Entry = str | tuple[str, ...]
Catalog = Mapping[str, Entry]

EN_PLURAL_FORMS = 2
RU_PLURAL_FORMS = 3
_RU_FEW_FIRST = 2
_RU_FEW_LAST = 4
_RU_TEENS_FIRST = 12
_RU_TEENS_LAST = 14

_EN: dict[str, Entry] = {
    # Counted nouns: bare forms, the number is added by `counted`.
    "noun.day": ("day", "days"),
    "noun.finding": ("High finding", "High findings"),
    "noun.repository_in": ("repository", "repositories"),
    "noun.project": ("project", "projects"),
    "noun.problem": ("problem", "problems"),
    "noun.item": ("item", "items"),
    "noun.failed_poll": ("failed poll", "failed polls"),
    "severity.high": "high",
    "severity.medium": "medium",
    "severity.low": "low",
    "severity.unknown": "unknown",
    "name.unnamed": "(unnamed project)",
    "alert.severity": "Severity: {severity}.",
    "alert.stale.title": "Stale Guard scans: <b>{name}</b>",
    "alert.stale.body": "No completed Guard run for {days} (last: {last}).",
    "alert.never_ran.title": "No Guard runs: <b>{name}</b>",
    "alert.never_ran.no_repos": "It has no repositories in Guard.",
    "alert.never_ran.repos": "No completed Guard run found in its {repos}.",
    "alert.high.title": "High findings: <b>{name}</b>",
    "alert.high.rise": "High findings rose from {previous} to {count} in {where}.",
    "alert.high.count": "{findings} in {where}.",
    "alert.high.seen": "Seen by the radar since {since} ({days}).",
    "alert.high.seen_long": "Long-standing: seen by the radar since {since} ({days}).",
    "summary.header": "Guard radar: {problems} need attention",
    "summary.item.stale": "- <b>{name}</b>: no completed Guard run for {days} ({severity})",
    "summary.item.high": "- <b>{name}</b>: {count} High in {where} ({severity})",
    "summary.item.high_rise": (
        "- <b>{name}</b>: High rose from {previous} to {count} in {where} ({severity})"
    ),
    "summary.item.never_ran": "- <b>{name}</b>: no completed Guard run found ({severity})",
}

_RU: dict[str, Entry] = {
    "noun.day": ("день", "дня", "дней"),
    "noun.finding": (
        "находка высокой серьёзности",
        "находки высокой серьёзности",
        "находок высокой серьёзности",
    ),
    # Prepositional case: "в 1 репозитории", "в 5 репозиториях".
    "noun.repository_in": ("репозитории", "репозиториях", "репозиториях"),
    "noun.project": ("проект", "проекта", "проектов"),
    "noun.problem": ("проблема", "проблемы", "проблем"),
    "noun.item": ("открытый пункт", "открытых пункта", "открытых пунктов"),
    "noun.failed_poll": ("неудачный опрос", "неудачных опроса", "неудачных опросов"),
    "severity.high": "высокая",
    "severity.medium": "средняя",
    "severity.low": "низкая",
    "severity.unknown": "не определена",
    "name.unnamed": "(проект без названия)",
    "alert.severity": "Серьёзность: {severity}.",
    "alert.stale.title": "Устаревшие проверки Guard: <b>{name}</b>",
    "alert.stale.body": "Нет завершённых запусков Guard уже {days} (последний: {last}).",
    "alert.never_ran.title": "Нет запусков Guard: <b>{name}</b>",
    "alert.never_ran.no_repos": "В Guard у проекта нет репозиториев.",
    "alert.never_ran.repos": "Завершённых запусков Guard не найдено в {repos} проекта.",
    "alert.high.title": "Находки высокой серьёзности: <b>{name}</b>",
    "alert.high.rise": (
        "Число находок высокой серьёзности выросло с {previous} до {count} в {where}."
    ),
    "alert.high.count": "{findings} в {where}.",
    "alert.high.seen": "Радар видит с {since} ({days}).",
    "alert.high.seen_long": "Давняя проблема: радар видит её с {since} ({days}).",
    "summary.header": "Guard radar: требуется внимание — {problems}",
    "summary.item.stale": "- <b>{name}</b>: нет завершённых запусков Guard уже {days} ({severity})",
    "summary.item.high": (
        "- <b>{name}</b>: находок высокой серьёзности: {count} в {where} ({severity})"
    ),
    "summary.item.high_rise": (
        "- <b>{name}</b>: число находок высокой серьёзности выросло "
        "с {previous} до {count} в {where} ({severity})"
    ),
    "summary.item.never_ran": "- <b>{name}</b>: завершённых запусков Guard не найдено ({severity})",
}

CATALOGS: Mapping[Language, Catalog] = {"en": _EN, "ru": _RU}


def plural_index(language: Language, count: int) -> int:
    """Index of the noun form for `count`: en (one, other), ru (one, few, many)."""
    if language == "en":
        return 0 if count == 1 else 1
    last, last_two = count % 10, count % 100
    if last == 1 and last_two != 11:
        return 0
    if _RU_FEW_FIRST <= last <= _RU_FEW_LAST and not _RU_TEENS_FIRST <= last_two <= _RU_TEENS_LAST:
        return 1
    return 2


def text(language: Language, key: str, /, **params: object) -> str:
    """The template under `key` filled with `params` (already escaped by the caller)."""
    entry = CATALOGS[language][key]
    if not isinstance(entry, str):
        message = f"{key} is a counted noun, not a template"
        raise TypeError(message)
    return entry.format(**params)


def noun(language: Language, key: str, count: int) -> str:
    """The bare noun form that goes with `count`."""
    entry = CATALOGS[language][key]
    if isinstance(entry, str):
        message = f"{key} is a template, not a counted noun"
        raise TypeError(message)
    return entry[plural_index(language, count)]


def counted(language: Language, key: str, count: int) -> str:
    return f"{count} {noun(language, key, count)}"


def severity_word(language: Language, value: str) -> str:
    return text(language, f"severity.{value}")


def placeholders(template: str) -> frozenset[str]:
    return frozenset(name for _, name, _, _ in string.Formatter().parse(template) if name)


def _plural_problems(key: str, language: str, forms: tuple[str, ...], want: int) -> list[str]:
    problems = []
    if len(forms) != want:
        problems.append(f"{key}: {language} needs {want} noun forms, has {len(forms)}")
    if any(placeholders(form) for form in forms):
        problems.append(f"{key}: {language} noun forms must not hold placeholders")
    return problems


def parity_problems(en: Catalog, ru: Catalog) -> list[str]:
    """Every way the Russian catalog disagrees with the English one; empty when in step."""
    problems = [f"{key}: missing in ru" for key in sorted(en.keys() - ru.keys())]
    problems += [f"{key}: missing in en" for key in sorted(ru.keys() - en.keys())]
    for key in sorted(en.keys() & ru.keys()):
        english, russian = en[key], ru[key]
        if isinstance(english, str) and isinstance(russian, str):
            lacking = placeholders(english) - placeholders(russian)
            extra = placeholders(russian) - placeholders(english)
            if lacking:
                problems.append(f"{key}: ru misses placeholder(s) {sorted(lacking)}")
            if extra:
                problems.append(f"{key}: ru has unknown placeholder(s) {sorted(extra)}")
        elif isinstance(english, tuple) and isinstance(russian, tuple):
            problems += _plural_problems(key, "en", english, EN_PLURAL_FORMS)
            problems += _plural_problems(key, "ru", russian, RU_PLURAL_FORMS)
        else:
            problems.append(f"{key}: en and ru differ in kind (template vs counted noun)")
    return problems
