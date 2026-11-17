import re
from pathlib import Path

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class EnvFileError(Exception):
    """Never carries file content: only the path and a fixed reason or line number."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"env file {path}: {reason}")


def read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise EnvFileError(path, "not found") from None
    except (OSError, UnicodeDecodeError):
        # `from None`: UnicodeDecodeError would echo the offending bytes.
        raise EnvFileError(path, "cannot be read as UTF-8 text") from None

    values: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        key = key.strip()
        if not separator or not _KEY.fullmatch(key):
            raise EnvFileError(path, f"line {number} is not KEY=VALUE")
        values[key] = _unquote(value.strip())
    return values


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value
