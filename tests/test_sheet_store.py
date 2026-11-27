"""The sheet record file: private, atomic, forgiving on load, read-only for dry runs."""

import stat
from datetime import UTC, datetime
from pathlib import Path

from sheet_fakes import PROJECT_A, projects_grid, sheet

from enji_guard_radar.sheet_store import SHEET_FILE, FileSheetStore, ReadOnlySheetStore
from enji_guard_radar.sheet_sync import SheetCopy, SheetHealth, SheetRecord

RECORD = SheetRecord(
    last_good=SheetCopy(
        fetched_at=datetime(2026, 11, 27, 10, tzinfo=UTC),
        data=sheet(projects_grid({"project_id": PROJECT_A, "mode": "paused"})),
    ),
    health=SheetHealth(consecutive_failures=2, last_failure_class="network"),
)


def test_a_record_survives_a_restart_and_the_file_is_private(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    FileSheetStore(directory).save(RECORD)
    assert FileSheetStore(directory).load() == RECORD
    assert stat.S_IMODE((directory / SHEET_FILE).stat().st_mode) == 0o600


def test_a_missing_file_is_an_empty_record(tmp_path: Path) -> None:
    assert FileSheetStore(tmp_path).load() == SheetRecord()


def test_a_corrupt_or_foreign_file_is_an_empty_record(tmp_path: Path) -> None:
    for content in (b"not json", b'{"schema_version": 1, "surprise": true}', b""):
        (tmp_path / SHEET_FILE).write_bytes(content)
        assert FileSheetStore(tmp_path).load() == SheetRecord()


def test_the_read_only_store_reads_but_never_writes(tmp_path: Path) -> None:
    FileSheetStore(tmp_path).save(RECORD)
    before = (tmp_path / SHEET_FILE).read_bytes()
    store = ReadOnlySheetStore(FileSheetStore(tmp_path))
    assert store.load() == RECORD
    store.save(SheetRecord())
    assert (tmp_path / SHEET_FILE).read_bytes() == before
