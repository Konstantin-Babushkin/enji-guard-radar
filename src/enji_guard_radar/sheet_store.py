"""The sheet record file: last good sheet copy and the sheet's failure streak. Shell module.

It is not dedup state: a missing or corrupt file is an empty record and never halts a run.
The file holds the sheet's cells, so it is written with mode 0600 like the rest of `state/`.
"""

import logging
from pathlib import Path

from pydantic import ValidationError

from enji_guard_radar.sheet_sync import SheetRecord, SheetStore
from enji_guard_radar.state_store import DIR_MODE, atomic_write

logger = logging.getLogger(__name__)

SHEET_FILE = "sheet-settings.json"


class FileSheetStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def path(self) -> Path:
        return self._directory / SHEET_FILE

    def load(self) -> SheetRecord:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return SheetRecord()
        except OSError as error:
            logger.warning("sheet record unreadable, starting a new one: %s", type(error).__name__)
            return SheetRecord()
        try:
            return SheetRecord.model_validate_json(raw)
        except ValidationError:
            logger.warning("sheet record corrupt, starting a new one")
            return SheetRecord()

    def save(self, record: SheetRecord) -> None:
        self._directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        atomic_write(self._directory, self.path, record.model_dump_json().encode("utf-8"))


class ReadOnlySheetStore:
    """Dry-run and read-only commands: reads the real file, writes nothing."""

    def __init__(self, inner: SheetStore) -> None:
        self._inner = inner

    def load(self) -> SheetRecord:
        return self._inner.load()

    def save(self, record: SheetRecord) -> None:
        logger.debug("read-only: sheet record not written")
