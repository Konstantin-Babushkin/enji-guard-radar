"""Shared builders and fakes for the settings-sheet tests."""

from enji_guard_radar.ports import SheetFailure, SheetUnavailable
from enji_guard_radar.sheet_settings import (
    GLOBALS_COLUMNS,
    PROJECT_COLUMNS,
    SheetData,
)

PROJECT_A = "00000000-0000-4000-8000-00000000a001"
PROJECT_B = "00000000-0000-4000-8000-00000000a002"
SPREADSHEET_ID = "1AbC-def_GHIjklmnop"

Grid = tuple[tuple[str, ...], ...]


def projects_grid(*rows: dict[str, str], columns: tuple[str, ...] = PROJECT_COLUMNS) -> Grid:
    """A projects tab: the header, then one row per dict (missing columns are empty cells)."""
    return (columns, *(tuple(row.get(column, "") for column in columns) for row in rows))


def globals_grid(*pairs: tuple[str, str]) -> Grid:
    return (GLOBALS_COLUMNS, *pairs)


def sheet(projects: Grid | None = None, globals_: Grid | None = None) -> SheetData:
    return SheetData(
        projects=projects if projects is not None else projects_grid(),
        globals=globals_ if globals_ is not None else globals_grid(),
    )


class FakeSheetReader:
    """Answers with the scripted sheets in order, the last one again and again."""

    def __init__(self, *answers: SheetData | SheetFailure) -> None:
        self.answers = list(answers)
        self.reads = 0

    def set(self, *answers: SheetData | SheetFailure) -> None:
        self.answers = list(answers)

    def read(self) -> SheetData:
        self.reads += 1
        answer = self.answers[0] if len(self.answers) == 1 else self.answers.pop(0)
        if isinstance(answer, SheetFailure):
            message = f"scripted failure: {answer.value}"
            raise SheetUnavailable(message, cause=answer)
        return answer
