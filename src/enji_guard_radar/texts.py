"""The strings of every Telegram message, per language (pure: no I/O, no clock).

Each catalog maps a dotted key to a `str.format` template or, for a counted noun, to a tuple of
bare noun forms: English has two (one, other), Russian three (one, few, many). Code formats the
numbers, dates and names and passes them in already escaped; a template holds fixed text and the
literal `<b>` / `</b>` tags only. `parity_problems` keeps the two catalogs in step.
"""

import string
from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

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
    # Plural for one as well: the English text has always said "1 failed polls in a row".
    "noun.failed_poll": ("failed polls", "failed polls"),
    "severity.high": "high",
    "severity.medium": "medium",
    "severity.low": "low",
    "severity.unknown": "unknown",
    "name.unnamed": "(unnamed project)",
    "marker.high": "🔴",
    "marker.other": "🟡",
    "alert.age": "{days}d",
    "alert.high.line": "{marker} <b>{name}</b> · {audit} High {previous}→{count} · {age}",
    "alert.high.line_count": "{marker} <b>{name}</b> · {audit} High {count} · {age}",
    "alert.stale.line": "{marker} <b>{name}</b> · no scan {age}",
    "alert.no_scan_yet.line": "{marker} <b>{name}</b> · no scan yet",
    "alert.where_repo": "{audit} ({repo})",
    "alert.triage_line": "   {priority} · {cause}",
    "summary.short_header": "Radar · {count} need attention",
    "alert.high.count": "{findings} in {where}.",
    "alert.high.seen": "Seen by the radar since {since} ({days}).",
    "reminder.short_title": "Radar reminder · {count} open",
    "digest.title": "Guard radar weekly digest",
    "digest.poll": "Last successful Guard poll: {stamp} UTC.",
    "digest.open_items": "Open items: {total}.",
    "digest.unread": "{projects} could not be read in that poll.",
    "digest.more_items": "+{hidden} more open {noun} not shown.",
    "digest.resolved_head": "Resolved since the last digest ({total}):",
    "digest.resolved_more": "+{hidden} more resolved.",
    "digest.nothing_open": "nothing open",
    "digest.nothing_open_read": "nothing open in the projects read",
    "digest.item.high": "- High: <b>{name}</b>: {what} {seen}",
    "digest.item.recurring": "Recurring: High findings in each of the last {runs} runs.",
    "digest.item.stale": (
        "- Stale: <b>{name}</b>: no completed Guard run for {days} (last: {last})."
    ),
    "digest.item.stale_never": "- Stale: <b>{name}</b>: no completed Guard run found.",
    "digest.item.low_score": (
        "- Low score: <b>{name}</b>: {audit} score {score}/100 in {repo} (run {day})."
    ),
    "digest.item.gap": (
        "- Tooling gap: <b>{name}</b>: no completed {audit} run in the last {days} "
        "in {repo} {tail}."
    ),
    "digest.gap.none": "(none seen)",
    "digest.gap.last": "(last run: {day})",
    "digest.resolved.high": "- <b>{name}</b>: High findings in {audit} are closed.",
    "digest.resolved.stale": "- <b>{name}</b>: no longer stale.",
    "digest.resolved.never_ran": "- <b>{name}</b>: now has completed Guard runs.",
    "digest.resolved.other": "- <b>{name}</b>: no longer open.",
    "action.token_rejected": "Telegram rejected the bot token (HTTP {code}); check {env}",
    "action.chat_forbidden": (
        "Telegram refused to post to chat {chat} (HTTP 403): the bot was removed, blocked or "
        "cannot post there; add it back or fix {env}"
    ),
    "action.chat_migrated": "Telegram chat {old} was upgraded to {new}; set {env}={new}",
    "action.bad_request": "Telegram rejected the message: {description}",
    "action.secret_withheld": (
        "A message was withheld because it looked like it contained a secret; "
        "check the radar log and the Guard data it came from"
    ),
    "ops.attention": "Guard radar needs attention:",
    "ops.recovered": "Guard radar recovered: Guard polls work again (failing since {since} UTC).",
    "ops.outage": (
        "Guard radar cannot read Guard: {reason}. Failing since {since} UTC "
        "({failures} in a row). "
        "The radar keeps polling and reports here once when it recovers."
    ),
    "ops.reason.unavailable": "Guard is unreachable or failing",
    "ops.reason.data": "Guard answered with data the radar does not accept; check the radar log",
    "ops.reason.auth_fallback": "Guard login problem: run `{command}`",
    "ops.reason.not_authenticated": "Guard login missing or unreadable: run `{command}`",
    "ops.reason.auth_expired": "Guard login expired or revoked: run `{command}`",
    "ops.reason.auth_client_gone": "Guard login client registration is gone: run `{command}`",
    "ops.reason.auth_rejected": "Guard login rejected: run `{command}`",
    "ops.reason.auth_forbidden": "Guard refused access (scope or permission): run `{command}`",
    "ops.halt": (
        "Guard radar halted: the state file is corrupt ({reason}). "
        "Nothing was sent and nothing was reset. "
        "Backup valid: {backup}. "
        "Check the file and restore the backup by hand only if it is right."
    ),
    "ops.yes": "yes",
    "ops.no": "no",
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
    "marker.high": "🔴",
    "marker.other": "🟡",
    "alert.age": "{days} д",
    "alert.high.line": "{marker} <b>{name}</b> · {audit} высокие {previous}→{count} · {age}",
    "alert.high.line_count": "{marker} <b>{name}</b> · {audit} высокие {count} · {age}",
    "alert.stale.line": "{marker} <b>{name}</b> · нет проверок {age}",
    "alert.no_scan_yet.line": "{marker} <b>{name}</b> · проверок ещё не было",
    "alert.where_repo": "{audit} ({repo})",
    "alert.triage_line": "   {priority} · {cause}",
    "summary.short_header": "Радар · требуют внимания: {count}",
    "alert.high.count": "{findings} в {where}.",
    "alert.high.seen": "Радар видит с {since} ({days}).",
    "reminder.short_title": "Радар · напоминание · открыто: {count}",
    "digest.title": "Еженедельный дайджест Guard radar",
    "digest.poll": "Последний успешный опрос Guard: {stamp} UTC.",
    "digest.open_items": "Открытых пунктов: {total}.",
    "digest.unread": "В этом опросе не удалось прочитать: {projects}.",
    "digest.more_items": "Не показано ещё: {hidden} {noun}.",
    "digest.resolved_head": "Закрыто с прошлого дайджеста ({total}):",
    "digest.resolved_more": "Закрыто ещё: {hidden}.",
    "digest.nothing_open": "ничего не открыто",
    "digest.nothing_open_read": "ничего не открыто в прочитанных проектах",
    "digest.item.high": "- Высокая: <b>{name}</b>: {what} {seen}",
    "digest.item.recurring": (
        "Повторяется: находки высокой серьёзности в каждом из последних {runs} запусков."
    ),
    "digest.item.stale": (
        "- Устарело: <b>{name}</b>: нет завершённых запусков Guard уже {days} (последний: {last})."
    ),
    "digest.item.stale_never": "- Устарело: <b>{name}</b>: завершённых запусков Guard не найдено.",
    "digest.item.low_score": (
        "- Низкий балл: <b>{name}</b>: {audit}: оценка {score}/100 в {repo} (запуск {day})."
    ),
    "digest.item.gap": (
        "- Нет проверки: <b>{name}</b>: нет завершённых запусков {audit} "
        "за последние {days} в {repo} {tail}."
    ),
    "digest.gap.none": "(запусков не видно)",
    "digest.gap.last": "(последний запуск: {day})",
    "digest.resolved.high": "- <b>{name}</b>: находки высокой серьёзности в {audit} закрыты.",
    "digest.resolved.stale": "- <b>{name}</b>: проверки больше не устарели.",
    "digest.resolved.never_ran": "- <b>{name}</b>: появились завершённые запуски Guard.",
    "digest.resolved.other": "- <b>{name}</b>: больше не открыто.",
    "action.token_rejected": "Telegram отклонил токен бота (HTTP {code}); проверьте {env}",
    "action.chat_forbidden": (
        "Telegram не разрешил писать в чат {chat} (HTTP 403): бота удалили, заблокировали "
        "или он не может там писать; верните его в чат или поправьте {env}"
    ),
    "action.chat_migrated": "Чат Telegram {old} преобразован в {new}; задайте {env}={new}",
    "action.bad_request": "Telegram отклонил сообщение: {description}",
    "action.secret_withheld": (
        "Сообщение задержано: оно похоже на секрет; "
        "проверьте лог радара и данные Guard, из которых оно взято"
    ),
    "ops.attention": "Guard radar: нужно вмешательство оператора:",
    "ops.recovered": "Guard radar восстановился: опросы Guard снова работают (сбои с {since} UTC).",
    "ops.outage": (
        "Guard radar не может прочитать данные Guard: {reason}. Сбои с {since} UTC "
        "({failures} подряд). "
        "Радар продолжает опрос и один раз сообщит здесь, когда связь восстановится."
    ),
    "ops.reason.unavailable": "Guard недоступен или сбоит",
    "ops.reason.data": "Guard ответил данными, которые радар не принимает; посмотрите лог радара",
    "ops.reason.auth_fallback": "проблема со входом в Guard: выполните `{command}`",
    "ops.reason.not_authenticated": (
        "вход в Guard не найден или не читается: выполните `{command}`"
    ),
    "ops.reason.auth_expired": "вход в Guard истёк или отозван: выполните `{command}`",
    "ops.reason.auth_client_gone": (
        "регистрация клиента входа в Guard пропала: выполните `{command}`"
    ),
    "ops.reason.auth_rejected": "вход в Guard отклонён: выполните `{command}`",
    "ops.reason.auth_forbidden": (
        "Guard отказал в доступе (права или область): выполните `{command}`"
    ),
    "ops.halt": (
        "Guard radar остановлен: файл состояния повреждён ({reason}). "
        "Ничего не отправлено и ничего не сброшено. "
        "Резервная копия цела: {backup}. "
        "Проверьте файл и восстановите копию вручную, только если она верна."
    ),
    "ops.yes": "да",
    "ops.no": "нет",
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


class ActionCode(StrEnum):
    TOKEN_REJECTED = "token_rejected"
    CHAT_FORBIDDEN = "chat_forbidden"
    CHAT_MIGRATED = "chat_migrated"
    BAD_REQUEST = "bad_request"
    SECRET_WITHHELD = "secret_withheld"


class ActionNotice(BaseModel):
    """One thing the operator must do, as data: the code picks the text, `params` fill it.

    Ids, codes, env names and the already sanitised Telegram description go in `params`
    verbatim; the sentence around them comes from the catalog in the message language.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ActionCode
    params: dict[str, str] = {}


def render_action(notice: ActionNotice, language: Language) -> str:
    return text(language, f"action.{notice.code.value}", **notice.params)


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
