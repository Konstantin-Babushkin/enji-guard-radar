"""The language of every Telegram message (pure: no I/O, no clock)."""

from typing import Literal

Language = Literal["en", "ru"]
DEFAULT_LANGUAGE: Language = "en"
