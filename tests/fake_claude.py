"""A scriptable fake `claude` executable. Tests never run the real CLI.

The fake is a Python script named `claude` in its own bin directory. When run it first writes
`record.json` beside itself (argv, environment key names, pid, cwd and its listing, stdin), then
sleeps, then prints the scripted envelope and exits with the scripted code. Writing the record
before sleeping lets a test read the pid and cwd of a call that was killed on timeout.
"""

import json
import os
import stat
import sys
from pathlib import Path

RECORD = "record.json"
SESSION_ID = "session-id-for-the-radar-tests"
DEFAULT_CAUSE = "Injection flaw in order search"

_SCRIPT = """#!{python}
import json
import os
import sys
import time
from pathlib import Path

record = {{
    "argv": sys.argv[1:],
    # Python itself adds LC_CTYPE when it coerces the C locale; the caller did not pass it.
    "env_keys": sorted(k for k in os.environ if k != "LC_CTYPE"),
    "pid": os.getpid(),
    "cwd": os.getcwd(),
    "cwd_listing": sorted(os.listdir(".")),
    "stdin": sys.stdin.buffer.read().decode("utf-8"),
}}
Path(__file__).resolve().with_name({record!r}).write_text(json.dumps(record), encoding="utf-8")
time.sleep({sleep!r})
sys.stdout.buffer.write({envelope!r}.encode("utf-8"))
sys.stdout.buffer.flush()
sys.exit({exit_code!r})
"""


def install_fake_claude(
    tmp_path: Path, *, envelope: str, exit_code: int = 0, sleep: float = 0.0
) -> Path:
    """Write the fake into `tmp_path/bin/claude` and return that directory (put it on PATH)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(
        _SCRIPT.format(
            python=sys.executable,
            record=RECORD,
            sleep=sleep,
            envelope=envelope,
            exit_code=exit_code,
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def read_record(bin_dir: Path) -> dict[str, object]:
    data: dict[str, object] = json.loads((bin_dir / RECORD).read_text(encoding="utf-8"))
    return data


def with_path(bin_dir: Path) -> str:
    """PATH value that finds the fake first and keeps the rest of the current PATH."""
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def ok_envelope(
    *,
    item_ids: tuple[str, ...] = ("i1",),
    priority: str = "p1",
    cause: str = DEFAULT_CAUSE,
    cost: float | None = 0.0123,
) -> str:
    """A success envelope shaped as in docs/triage-notes.md "JSON envelope"."""
    structured = {"items": [{"item_id": i, "priority": priority, "cause": cause} for i in item_ids]}
    body: dict[str, object] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "terminal_reason": "completed",
        "api_error_status": None,
        "num_turns": 2,
        "result": json.dumps(structured),
        "structured_output": structured,
        "session_id": SESSION_ID,
        "uuid": "uuid-for-the-radar-tests",
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_creation_input_tokens": 300,
            "cache_read_input_tokens": 4000,
        },
    }
    if cost is not None:
        body["total_cost_usd"] = cost
    return json.dumps(body)


def error_envelope(**fields: object) -> str:
    """An `is_error: true` envelope; `fields` override or add keys (see "Exit codes and errors")."""
    body: dict[str, object] = {
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "api_error_status": None,
        "result": "something went wrong",
        "session_id": SESSION_ID,
        "total_cost_usd": 0.0106,
        "usage": {"input_tokens": 5, "output_tokens": 0},
    }
    body.update(fields)
    return json.dumps(body)
