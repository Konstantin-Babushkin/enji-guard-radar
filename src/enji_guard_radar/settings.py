"""Secrets and chat ids, read only from the environment or a `.env` file. Shell module."""

import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

from enji_guard_radar.env_file import EnvFileError, read_env_file

ENV_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_OPS_CHAT_ID = "TELEGRAM_OPS_CHAT_ID"

_CHAT_ID = re.compile(r"-?\d+|@[A-Za-z0-9_]{5,32}")


class SettingsError(Exception):
    """Names a variable, a path or a line number, never a value."""


class TelegramSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bot_token: SecretStr | None
    chat_id: str | None
    ops_chat_id: str | None


class LiveTelegram(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bot_token: SecretStr
    chat_id: str
    ops_chat_id: str | None


def read_dotenv(path: Path) -> dict[str, str]:
    """A missing file is valid and gives {}; a malformed one names the line, not its text."""
    try:
        return read_env_file(path)
    except EnvFileError as error:
        if not path.exists():
            return {}
        raise SettingsError(str(error)) from None


def _pick(name: str, environ: Mapping[str, str], dotenv: Mapping[str, str]) -> str | None:
    """A non-empty environment value wins; an empty one counts as unset."""
    for source in (environ, dotenv):
        value = source.get(name, "").strip()
        if value:
            return value
    return None


def _chat_id(name: str, value: str | None) -> str | None:
    if value is not None and not _CHAT_ID.fullmatch(value):
        message = f"{name} must be a numeric chat id or an @username"
        raise SettingsError(message)
    return value


def load_settings(environ: Mapping[str, str], dotenv_path: Path | None) -> TelegramSettings:
    dotenv = read_dotenv(dotenv_path) if dotenv_path is not None else {}
    token = _pick(ENV_BOT_TOKEN, environ, dotenv)
    return TelegramSettings(
        bot_token=SecretStr(token) if token is not None else None,
        chat_id=_chat_id(ENV_CHAT_ID, _pick(ENV_CHAT_ID, environ, dotenv)),
        ops_chat_id=_chat_id(ENV_OPS_CHAT_ID, _pick(ENV_OPS_CHAT_ID, environ, dotenv)),
    )


def require_live(settings: TelegramSettings) -> LiveTelegram:
    missing = [
        name
        for name, value in ((ENV_BOT_TOKEN, settings.bot_token), (ENV_CHAT_ID, settings.chat_id))
        if value is None
    ]
    if missing or settings.bot_token is None or settings.chat_id is None:
        message = f"missing required setting(s): {', '.join(missing)}"
        raise SettingsError(message)
    return LiveTelegram(
        bot_token=settings.bot_token,
        chat_id=settings.chat_id,
        ops_chat_id=settings.ops_chat_id,
    )


def known_secrets(settings: TelegramSettings) -> tuple[str, ...]:
    if settings.bot_token is None:
        return ()
    return (settings.bot_token.get_secret_value(),)
