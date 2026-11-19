"""Sanitise Guard finding titles before they can reach Claude. Core module: pure, no I/O.

Order of steps in `sanitize_title`: secret check on the raw text, control and format
characters, code spans, link/path/identifier tokens, whitespace collapse, 80-character cap,
letter count, secret check on the result. Instruction-like text is not removed here; the
delimiting and output validation of the triage request are the controls for that.
"""

import re
import unicodedata
from collections.abc import Iterable

from enji_guard_radar.model import Severity
from enji_guard_radar.secret_guard import find_secret

MAX_TITLE_CHARS = 80
MAX_TITLES_PER_SERIES = 3
MIN_LETTERS = 3

_REPLACED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
_TRAILING_PUNCTUATION = ".,;:!?)"
_CODE_SPAN = re.compile(r"`[^`]*`")
_ADDRESS_MARKERS = ("://", "www.", "t.me", "mailto:", "@")
# A stem, then a dot and 1 to 5 ASCII letters or digits: "config.yaml", "main.py", "e.g".
_EXTENSION_TAIL = re.compile(r"^.+\.[A-Za-z0-9]{1,5}$")
# "F-006", "CVE-2024-1234", "SOME-DESCRIPTIVE-SLUG": upper-case parts joined by hyphens.
_CAPS_SLUG = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
# A hex run needs a digit, so words made of a..f letters ("defaced") are kept.
_HEX_RUN = re.compile(r"^[0-9a-fA-F]{7,}$")


def _is_dropped(token: str) -> bool:
    core = token.rstrip(_TRAILING_PUNCTUATION)
    if not core:
        return False
    lowered = core.lower()
    if any(marker in lowered for marker in _ADDRESS_MARKERS):
        return True
    if "/" in core or "\\" in core:
        return True
    if _EXTENSION_TAIL.match(core):
        return True
    if _CAPS_SLUG.match(core) or _UUID.match(core):
        return True
    return bool(_HEX_RUN.match(core)) and any(char.isdigit() for char in core)


def _cap(text: str) -> str:
    if len(text) <= MAX_TITLE_CHARS:
        return text
    cut = text[: MAX_TITLE_CHARS + 1].rfind(" ")
    return (text[:cut] if cut > 0 else text[:MAX_TITLE_CHARS]).rstrip()


def sanitize_title(raw: str, known_secrets: Iterable[str] = ()) -> str | None:
    """A short title free of ids, paths, links and secrets, or None when nothing safe is left."""
    secrets = tuple(known_secrets)
    if find_secret(raw, secrets) is not None:
        return None
    spaced = "".join(
        " " if unicodedata.category(char) in _REPLACED_CATEGORIES else char for char in raw
    )
    without_code = _CODE_SPAN.sub(" ", spaced)
    kept = " ".join(token for token in without_code.split() if not _is_dropped(token))
    result = _cap(kept)
    if sum(char.isalpha() for char in result) < MIN_LETTERS:
        return None
    if find_secret(result, secrets) is not None:
        return None
    return result


def pick_titles(
    findings: Iterable[tuple[Severity, str]], known_secrets: Iterable[str] = ()
) -> tuple[str, ...]:
    """Up to three distinct sanitised High titles, in report order."""
    secrets = tuple(known_secrets)
    picked: list[str] = []
    seen: set[str] = set()
    for severity, raw in findings:
        if severity is not Severity.HIGH:
            continue
        title = sanitize_title(raw, secrets)
        if title is None or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        picked.append(title)
        if len(picked) == MAX_TITLES_PER_SERIES:
            break
    return tuple(picked)
