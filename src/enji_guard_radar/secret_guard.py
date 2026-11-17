"""Final secret gate and log redaction. Core module: no I/O imports."""

import logging
import re
from collections.abc import Iterable, Sequence

from enji_guard_radar.model import OutboundMessage
from enji_guard_radar.ports import DeliveryError, NotDelivered, Notifier, SendOutcome

logger = logging.getLogger(__name__)

REDACTED = "[REDACTED]"
MIN_SECRET_CHARS = 8

# ASSUMPTION: a Telegram bot token has the shape <6 or more digits>:<30 or more of A-Za-z0-9_->.
# The exact token value from the environment is always masked as well, whatever its shape.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("telegram_bot_token", re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")),
    ("telegram_bot_url", re.compile(r"api\.telegram\.org/bot[^/\s]+")),
)

_BLOCKED_ACTION = (
    "A message was withheld because it looked like it contained a secret; "
    "check the radar log and the Guard data it came from"
)


def _usable(known_secrets: Iterable[str]) -> list[str]:
    return [secret for secret in known_secrets if len(secret) >= MIN_SECRET_CHARS]


def find_secret(text: str, known_secrets: Iterable[str]) -> str | None:
    """Name of what was found, never the matched value."""
    if any(secret in text for secret in _usable(known_secrets)):
        return "known_secret"
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return name
    return None


def redact(text: str, known_secrets: Iterable[str]) -> str:
    # Longest first, so a secret that contains another one is masked as a whole.
    for secret in sorted(_usable(known_secrets), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for _name, pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


class SecretGatedNotifier:
    """Withholds any message that carries a secret; never calls the inner notifier then."""

    def __init__(self, inner: Notifier, known_secrets: Sequence[str]) -> None:
        self._inner = inner
        self._known_secrets = tuple(known_secrets)

    def send(self, message: OutboundMessage) -> SendOutcome:
        pattern = find_secret(message.text, self._known_secrets)
        if pattern is None:
            return self._inner.send(message)
        logger.error(
            "message withheld: it looks like it contains a secret (pattern=%s, keys=%s)",
            pattern,
            ", ".join(key.as_text() for key in message.keys),
        )
        return NotDelivered(
            error=DeliveryError.BLOCKED_SECRET,
            detail=f"withheld by the secret gate (pattern {pattern})",
            stop_sending=False,
            operator_action=_BLOCKED_ACTION,
        )
