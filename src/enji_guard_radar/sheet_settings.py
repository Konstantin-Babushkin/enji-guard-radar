"""Settings from a Google Sheet: validate the cells and merge them over the YAML config.

Pure: the shell reads the sheet and hands over its cells as strings. The sheet overrides the
YAML field by field; an empty cell means "not set", so the YAML value or the default applies.
Nothing is applied unless the whole sheet is valid. Errors name the tab, the row and the column
and never carry a cell value (the sheet is edited by people, so a value may be anything).
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from enji_guard_radar.config import RadarConfig

logger = logging.getLogger(__name__)

PROJECTS_TAB = "projects"
GLOBALS_TAB = "globals"
PROJECT_ID_COLUMN = "project_id"
# Free text for humans; the radar ignores it.
NAME_COLUMN = "name"
PROJECT_COLUMNS = (
    PROJECT_ID_COLUMN,
    NAME_COLUMN,
    "mode",
    "high_priority_audit_types",
    "stale_after_days",
    "high_aged_after_days",
    "reminder_every_days",
    "tooling_window_days",
    "ai_readiness_below",
    "tests_below",
)
GLOBALS_COLUMNS = ("key", "value")
# The config sections a non-developer may set. Triage, ops and sheet are not among them.
GLOBAL_SECTIONS = (
    "defaults",
    "alerts",
    "high",
    "messages",
    "schedule",
    "digest",
    "reminders",
    "scores",
    "recurring",
    "tooling_gaps",
)
# Keys whose cell holds a comma-separated list; every other value is a number or a word.
LIST_PROJECT_COLUMNS = frozenset({"high_priority_audit_types"})
LIST_GLOBAL_KEYS = frozenset({"high.high_priority_audit_types", "tooling_gaps.audit_types"})
MAX_REPORTED_PROBLEMS = 10
CONFIG_TAB = "merged config"

_INTEGER = re.compile(r"-?\d+")


class SheetData(BaseModel):
    """The two tabs as the sheet holds them: rows of strings, row 0 is the header (sheet row 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    projects: tuple[tuple[str, ...], ...] = ()
    globals: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class SheetProblem:
    tab: str
    row: int | None
    column: str
    reason: str

    def text(self) -> str:
        where = f"{self.tab} row {self.row}" if self.row is not None else self.tab
        return f"{where} column {self.column}: {self.reason}"


class SheetInvalid(Exception):  # noqa: N818 - named for what the sheet is
    """The sheet is not valid; the message lists tab, row and column, never a cell value."""

    def __init__(self, problems: list[SheetProblem]) -> None:
        shown = [problem.text() for problem in problems[:MAX_REPORTED_PROBLEMS]]
        more = len(problems) - len(shown)
        suffix = f" (and {more} more)" if more > 0 else ""
        super().__init__("; ".join(shown) + suffix)
        self.problems = tuple(problems)


@dataclass(frozen=True)
class _Cell:
    tab: str
    row: int
    column: str
    key: str | None = None


@dataclass
class _Overlay:
    """What the sheet sets: values by config path, and which cell each one came from."""

    values: dict[tuple[str, ...], object] = field(default_factory=dict)
    origins: dict[tuple[str, ...], _Cell] = field(default_factory=dict)
    problems: list[SheetProblem] = field(default_factory=list)


def allowed_global_keys() -> frozenset[str]:
    """Dotted config paths the `globals` tab may set, derived from the config models."""
    keys: set[str] = set()
    for section in GLOBAL_SECTIONS:
        annotation = RadarConfig.model_fields[section].annotation
        model_fields = getattr(annotation, "model_fields", {})
        keys.update(f"{section}.{name}" for name in model_fields)
    return frozenset(keys)


def apply_sheet(base: RadarConfig, data: SheetData) -> RadarConfig:
    """The YAML config with the sheet's values over it. Raises SheetInvalid; never applies half."""
    overlay = _Overlay()
    _read_projects(data.projects, overlay)
    _read_globals(data.globals, overlay)
    if overlay.problems:
        raise SheetInvalid(overlay.problems)
    merged = base.model_dump()
    for path, value in overlay.values.items():
        _set_path(merged, path, value)
    try:
        config = RadarConfig.model_validate(merged)
    except ValidationError as error:
        raise SheetInvalid(_problems_of(error, overlay)) from None
    return config


def _set_path(target: dict[str, Any], path: tuple[str, ...], value: object) -> None:
    node = target
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def _problems_of(error: ValidationError, overlay: _Overlay) -> list[SheetProblem]:
    problems: list[SheetProblem] = []
    for item in error.errors():
        loc = tuple(str(part) for part in item["loc"])
        cell = _origin_of(loc, overlay.origins)
        if cell is None:
            problems.append(SheetProblem(CONFIG_TAB, None, ".".join(loc), item["msg"]))
            continue
        reason = item["msg"] if cell.key is None else f"{cell.key}: {item['msg']}"
        problems.append(SheetProblem(cell.tab, cell.row, cell.column, reason))
    return problems


def _origin_of(loc: tuple[str, ...], origins: dict[tuple[str, ...], _Cell]) -> _Cell | None:
    for length in range(len(loc), 0, -1):
        cell = origins.get(loc[:length])
        if cell is not None:
            return cell
    return None


def _scalar(text: str) -> object:
    return int(text) if _INTEGER.fullmatch(text) else text.lower()


def _listed(text: str) -> list[str]:
    return [item.strip().lower() for item in text.split(",") if item.strip()]


@dataclass(frozen=True)
class _Row:
    number: int
    cells: dict[str, str]


def _rows_of(
    grid: tuple[tuple[str, ...], ...], tab: str, allowed: tuple[str, ...], required: tuple[str, ...]
) -> tuple[list[_Row], list[SheetProblem]]:
    """The data rows keyed by column name. A bad header or a stray cell is a problem."""
    if not grid:
        reason = "the tab is empty: the header row is missing"
        return [], [SheetProblem(tab, 1, required[0], reason)]
    header = [cell.strip() for cell in grid[0]]
    problems = _header_problems(header, tab, allowed, required)
    if problems:
        return [], problems
    rows: list[_Row] = []
    for number, raw in enumerate(grid[1:], start=2):
        cells = [cell.strip() for cell in raw]
        if not any(cells):
            continue
        stray = [c for i, c in enumerate(cells) if c and (i >= len(header) or not header[i])]
        if stray:
            problems.append(SheetProblem(tab, number, "(no header)", "a cell has no column header"))
            continue
        padded = cells + [""] * (len(header) - len(cells))
        rows.append(_Row(number, {h: c for h, c in zip(header, padded, strict=False) if h}))
    return rows, problems


def _header_problems(
    header: list[str], tab: str, allowed: tuple[str, ...], required: tuple[str, ...]
) -> list[SheetProblem]:
    problems: list[SheetProblem] = []
    seen: set[str] = set()
    for name in header:
        if not name:
            continue
        if name not in allowed:
            problems.append(SheetProblem(tab, 1, "(header)", "unknown column"))
        elif name in seen:
            problems.append(SheetProblem(tab, 1, name, "column is listed twice"))
        seen.add(name)
    problems.extend(
        SheetProblem(tab, 1, column, "required column is missing")
        for column in required
        if column not in seen
    )
    return problems


def _read_projects(grid: tuple[tuple[str, ...], ...], overlay: _Overlay) -> None:
    rows, problems = _rows_of(grid, PROJECTS_TAB, PROJECT_COLUMNS, required=(PROJECT_ID_COLUMN,))
    overlay.problems.extend(problems)
    first_seen: dict[str, int] = {}
    for row in rows:
        project_id = row.cells.get(PROJECT_ID_COLUMN, "")
        if not project_id:
            overlay.problems.append(
                SheetProblem(PROJECTS_TAB, row.number, PROJECT_ID_COLUMN, "must not be empty")
            )
            continue
        if project_id in first_seen:
            reason = f"duplicate project id (first at row {first_seen[project_id]})"
            overlay.problems.append(
                SheetProblem(PROJECTS_TAB, row.number, PROJECT_ID_COLUMN, reason)
            )
            continue
        first_seen[project_id] = row.number
        _read_project_cells(project_id, row, overlay)


def _read_project_cells(project_id: str, row: _Row, overlay: _Overlay) -> None:
    for column, text in row.cells.items():
        if column in (PROJECT_ID_COLUMN, NAME_COLUMN) or not text:
            continue
        path = ("projects", project_id, column)
        overlay.values[path] = _listed(text) if column in LIST_PROJECT_COLUMNS else _scalar(text)
        overlay.origins[path] = _Cell(PROJECTS_TAB, row.number, column)


def _read_globals(grid: tuple[tuple[str, ...], ...], overlay: _Overlay) -> None:
    rows, problems = _rows_of(grid, GLOBALS_TAB, GLOBALS_COLUMNS, required=GLOBALS_COLUMNS)
    overlay.problems.extend(problems)
    allowed = allowed_global_keys()
    first_seen: dict[str, int] = {}
    for row in rows:
        key = row.cells.get("key", "")
        if key not in allowed:
            reason = "key is empty" if not key else "unknown key or a key the sheet may not set"
            overlay.problems.append(SheetProblem(GLOBALS_TAB, row.number, "key", reason))
            continue
        if key in first_seen:
            reason = f"{key}: key is listed twice (first at row {first_seen[key]})"
            overlay.problems.append(SheetProblem(GLOBALS_TAB, row.number, "key", reason))
            continue
        first_seen[key] = row.number
        text = row.cells.get("value", "")
        if not text:
            continue
        path = tuple(key.split("."))
        overlay.values[path] = _listed(text) if key in LIST_GLOBAL_KEYS else _scalar(text)
        overlay.origins[path] = _Cell(GLOBALS_TAB, row.number, "value", key)
