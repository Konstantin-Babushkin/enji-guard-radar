"""The sheet template in docs/deploy.md validates, and the key line in the runbook loads."""

import csv
import io
import json
import re
from pathlib import Path

from sheet_fakes import PROJECT_A

from enji_guard_radar.config import RadarConfig
from enji_guard_radar.env_file import read_env_file
from enji_guard_radar.settings import ENV_SHEET_KEY, load_sheet_key
from enji_guard_radar.sheet_settings import (
    GLOBALS_COLUMNS,
    PROJECT_COLUMNS,
    SheetData,
    apply_sheet,
)

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")


def csv_blocks() -> list[tuple[tuple[str, ...], ...]]:
    blocks = re.findall(r"```csv\n(.*?)```", RUNBOOK, flags=re.DOTALL)
    return [tuple(tuple(row) for row in csv.reader(io.StringIO(block))) for block in blocks]


def is_projects(block: tuple[tuple[str, ...], ...]) -> bool:
    return block[0] == PROJECT_COLUMNS


def is_globals(block: tuple[tuple[str, ...], ...]) -> bool:
    return block[0] == GLOBALS_COLUMNS


def test_the_runbook_ships_header_rows_and_examples_for_both_tabs() -> None:
    blocks = csv_blocks()
    assert [len(b) for b in blocks if is_projects(b)] == [1, 3]
    assert len(next(b for b in blocks if is_globals(b))) == 1
    assert len(blocks) == 4


def test_the_documented_templates_and_examples_validate() -> None:
    blocks = csv_blocks()
    projects = [b for b in blocks if is_projects(b)]
    globals_ = [b for b in blocks if is_globals(b)]
    base = RadarConfig()
    assert apply_sheet(base, SheetData(projects=projects[0], globals=globals_[0])) == base
    merged = apply_sheet(base, SheetData(projects=projects[1], globals=globals_[1]))
    assert merged.projects[PROJECT_A].high_priority_audit_types == (
        "audit.security",
        "audit.dependency-vulnerabilities",
    )
    assert merged.mode_for("00000000-0000-4000-8000-00000000a005") == "paused"
    assert merged.schedule.every_minutes == 60
    assert merged.messages.language == "ru"


def test_the_one_line_key_the_runbook_describes_loads_from_an_env_file(tmp_path: Path) -> None:
    key = {
        "type": "service_account",
        "client_email": "a@b.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nabc=\n-----END PRIVATE KEY-----\n",
    }
    env = tmp_path / "radar.env"
    env.write_text(f"TELEGRAM_BOT_TOKEN=x\n{ENV_SHEET_KEY}={json.dumps(key)}\n", encoding="utf-8")
    assert read_env_file(env)[ENV_SHEET_KEY] == json.dumps(key)
    loaded = load_sheet_key({}, env)
    assert loaded is not None
    assert json.loads(loaded.get_secret_value()) == key


def test_an_empty_key_variable_counts_as_not_set(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(f"{ENV_SHEET_KEY}=\n", encoding="utf-8")
    assert load_sheet_key({}, env) is None


def test_both_env_examples_name_the_key_variable_and_hold_no_real_value() -> None:
    assert read_env_file(ROOT / ".env.example")[ENV_SHEET_KEY] == ""
    placeholder = read_env_file(ROOT / "project.env.example")[ENV_SHEET_KEY]
    assert placeholder.startswith("replace-with-")
