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
    "noun.high_problem": ("high problem", "high problems"),
    "noun.audit_in": ("audit", "audits"),
    "noun.project": ("project", "projects"),
    "noun.project_in": ("project", "projects"),
    # Plural for one as well: the English text has always said "1 failed polls in a row".
    "noun.failed_poll": ("failed polls", "failed polls"),
    "noun.failed_read": ("failed read", "failed reads"),
    "severity.high": "high",
    "severity.medium": "medium",
    "severity.low": "low",
    "severity.unknown": "unknown",
    "name.unnamed": "(unnamed project)",
    "marker.high": "🔴",
    "marker.other": "🟡",
    "marker.green": "🟢",
    "alert.age.today": "today",
    "alert.age.held": "open for {days}",
    "alert.problems": "high problems {count}",
    "alert.rise": " (was {previous})",
    "alert.where.one": "in audit {audit}",
    "alert.where.many": "in {audits}",
    "alert.where.worst": "in {audits}, mostly {audit}",
    "alert.project.line": "{marker} <b>{name}</b>: {problems}, {where} · {age}",
    "alert.stale.line": "{marker} <b>{name}</b>: no scan for {days}",
    "alert.no_scan_yet.line": "{marker} <b>{name}</b>: no scan yet",
    "alert.triage_line": "   {priority} · {cause}",
    "summary.short_header": "Radar · problems: {projects}",
    "reminder.short_title": "Radar reminder · open: {projects}",
    "digest.title": "Radar · weekly digest",
    "digest.short_clear": "<b>{title}</b> · {what} · last poll {stamp} UTC",
    "digest.overview": "High problems: {count} in {projects}, closed this week: {resolved}.",
    "digest.overview_none": "No open high problems, closed this week: {resolved}.",
    "digest.project.line": "{marker} <b>{name}</b> — {problems}; most in: {audits}; {age}",
    "digest.project.line_low": (
        "{marker} <b>{name}</b> — {problems}, low priority; most in: {audits}; {age}"
    ),
    "digest.project.low_extra": "{count} more in low-priority audits",
    "digest.project.stale": "no scan for {days}",
    "digest.project.never_ran": "no scan yet",
    "digest.project.line_state": "{marker} <b>{name}</b> — {state}",
    "digest.project.clean_state": "clean",
    "digest.age.oldest": "oldest {age}",
    "digest.clean_more": "and {count} more {noun} clean",
    "digest.unread": "⚠️ Could not read {projects}",
    "digest.unread_poll": "⚠️ Could not read {projects} · poll {stamp} UTC",
    "digest.poll": "Poll {stamp} UTC",
    "digest.nothing_open": "nothing open",
    "digest.nothing_open_read": "nothing open in the projects read",
    "audit_name.ai-readiness": "AI readiness",
    "audit_name.dead-code": "dead code",
    "audit_name.dependency-hygiene": "dependencies",
    "audit_name.implementation-unification": "code consistency",
    "audit_name.oss-readiness": "open-source readiness",
    "audit_name.recon": "overview",
    "audit_name.security": "security",
    "audit_name.tech-health": "tech health",
    "audit_name.tests": "tests",
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
    "ops.sheet.outage": (
        "Radar settings sheet cannot be used: {reason}. Failing since {since} UTC "
        "({failures} in a row). {fallback} "
        "The radar keeps reading the sheet and reports here once when it works again."
    ),
    "ops.sheet.recovered": "Radar settings sheet works again (failing since {since} UTC).",
    "ops.sheet.fallback_copy": "The last good copy from {fetched} UTC stays in use.",
    "ops.sheet.fallback_yaml": "No good copy exists, so the radar runs on radar.yaml alone.",
    "ops.sheet.reason.key": (
        "the Google service account key is missing or invalid (check the key variable "
        "in the env file)"
    ),
    "ops.sheet.reason.network": "Google is unreachable or failing",
    "ops.sheet.reason.denied": (
        "Google refused access (share the sheet with the service account and enable the Sheets API)"
    ),
    "ops.sheet.reason.not_found": "the spreadsheet was not found (check sheet.spreadsheet_id)",
    "ops.sheet.reason.response": "Google answered with data the radar does not accept",
    "ops.sheet.reason.invalid": "the sheet is invalid ({detail})",
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
    "noun.high_problem": ("высокая проблема", "высокие проблемы", "высоких проблем"),
    # Prepositional case: "в 1 аудите", "в 6 аудитах".
    "noun.audit_in": ("аудите", "аудитах", "аудитах"),
    "noun.project": ("проект", "проекта", "проектов"),
    # Prepositional case: "в 1 проекте", "в 7 проектах".
    "noun.project_in": ("проекте", "проектах", "проектах"),
    "noun.failed_poll": ("неудачный опрос", "неудачных опроса", "неудачных опросов"),
    "noun.failed_read": ("неудачное чтение", "неудачных чтения", "неудачных чтений"),
    "severity.high": "высокая",
    "severity.medium": "средняя",
    "severity.low": "низкая",
    "severity.unknown": "не определена",
    "name.unnamed": "(проект без названия)",
    "marker.high": "🔴",
    "marker.other": "🟡",
    "marker.green": "🟢",
    "alert.age.today": "сегодня",
    "alert.age.held": "держится {days}",
    "alert.problems": "высоких проблем {count}",
    "alert.rise": " (было {previous})",
    "alert.where.one": "в аудите {audit}",
    "alert.where.many": "в {audits}",
    "alert.where.worst": "в {audits}, больше всего {audit}",
    "alert.project.line": "{marker} <b>{name}</b>: {problems}, {where} · {age}",
    "alert.stale.line": "{marker} <b>{name}</b>: нет проверок {days}",
    "alert.no_scan_yet.line": "{marker} <b>{name}</b>: проверок ещё не было",
    "alert.triage_line": "   {priority} · {cause}",
    "summary.short_header": "Радар · проблемы: {projects}",
    "reminder.short_title": "Радар · напоминание · открыто: {projects}",
    "digest.title": "Радар · недельная сводка",
    "digest.short_clear": "<b>{title}</b> · {what} · последний опрос {stamp} UTC",
    "digest.overview": "Высоких проблем: {count} в {projects}, закрыто за неделю: {resolved}.",
    "digest.overview_none": "Открытых высоких проблем нет, закрыто за неделю: {resolved}.",
    "digest.project.line": "{marker} <b>{name}</b> — {problems}; больше всего: {audits}; {age}",
    "digest.project.line_low": (
        "{marker} <b>{name}</b> — {problems}, низкий приоритет; больше всего: {audits}; {age}"
    ),
    "digest.project.low_extra": "ещё {count} в аудитах низкого приоритета",
    "digest.project.stale": "нет проверок {days}",
    "digest.project.never_ran": "проверок ещё не было",
    "digest.project.line_state": "{marker} <b>{name}</b> — {state}",
    "digest.project.clean_state": "чисто",
    "digest.age.oldest": "самая старая {age}",
    "digest.clean_more": "и ещё {count} {noun} чисто",
    "digest.unread": "⚠️ Не удалось прочитать {projects}",
    "digest.unread_poll": "⚠️ Не удалось прочитать {projects} · опрос {stamp} UTC",
    "digest.poll": "Опрос {stamp} UTC",
    "digest.nothing_open": "ничего не открыто",
    "digest.nothing_open_read": "ничего не открыто в прочитанных проектах",
    "audit_name.ai-readiness": "готовность к ИИ",
    "audit_name.dead-code": "мёртвый код",
    "audit_name.dependency-hygiene": "зависимости",
    "audit_name.implementation-unification": "единообразие кода",
    "audit_name.oss-readiness": "готовность к open source",
    "audit_name.recon": "обзор",
    "audit_name.security": "безопасность",
    "audit_name.tech-health": "техсостояние",
    "audit_name.tests": "тесты",
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
    "ops.sheet.outage": (
        "Таблицу настроек радара нельзя использовать: {reason}. Сбои с {since} UTC "
        "({failures} подряд). {fallback} "
        "Радар продолжает читать таблицу и один раз сообщит здесь, когда она заработает."
    ),
    "ops.sheet.recovered": "Таблица настроек радара снова работает (сбои с {since} UTC).",
    "ops.sheet.fallback_copy": "Продолжает действовать последняя рабочая копия от {fetched} UTC.",
    "ops.sheet.fallback_yaml": "Рабочей копии нет, поэтому радар работает только по radar.yaml.",
    "ops.sheet.reason.key": (
        "ключ сервисного аккаунта Google не задан или неверен (проверьте переменную с ключом "
        "в env-файле)"
    ),
    "ops.sheet.reason.network": "Google недоступен или сбоит",
    "ops.sheet.reason.denied": (
        "Google отказал в доступе (откройте таблицу сервисному аккаунту и включите Sheets API)"
    ),
    "ops.sheet.reason.not_found": "таблица не найдена (проверьте sheet.spreadsheet_id)",
    "ops.sheet.reason.response": "Google ответил данными, которые радар не принимает",
    "ops.sheet.reason.invalid": "таблица неверна ({detail})",
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


def audit_name(language: Language, action_key: str) -> str:
    """The short human word for an audit type; the key without `audit.` when none is known."""
    short = action_key.removeprefix("audit.")
    entry = CATALOGS[language].get(f"audit_name.{short}")
    return entry if isinstance(entry, str) else short


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
