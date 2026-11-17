"""FileStateStore and DryRunStateStore on a real temp directory."""

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from enji_guard_radar.ledger import (
    ConditionRecord,
    Corrupt,
    CorruptStateError,
    Loaded,
    Missing,
    ProjectRecord,
    State,
    serialize_state,
)
from enji_guard_radar.model import AlertKey
from enji_guard_radar.state_store import DryRunStateStore, FileStateStore

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)


def make_state(project_id: str) -> State:
    return State(
        projects=(ProjectRecord(project_id=project_id, first_seen_at=NOW, last_evaluated_at=NOW),),
        conditions=(
            ConditionRecord(
                key=AlertKey(project_id=project_id, level="stale"),
                first_seen_at=NOW,
                last_seen_at=NOW,
                baseline=True,
            ),
        ),
    )


def snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(directory.iterdir())
        if p.is_file()
    }


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_load_of_an_empty_directory_is_missing(directory: Path) -> None:
    assert isinstance(FileStateStore(directory).load(), Missing)
    assert not directory.exists()


def test_first_save_creates_state_only_and_second_save_keeps_a_backup(directory: Path) -> None:
    store = FileStateStore(directory)
    first, second = make_state("a"), make_state("b")
    store.save(first)
    assert sorted(p.name for p in directory.iterdir()) == ["state.json"]

    store.save(second)
    assert store.backup_path.read_bytes() == serialize_state(first)
    assert store.state_path.read_bytes() == serialize_state(second)
    loaded = store.load()
    assert isinstance(loaded, Loaded)
    assert loaded.state == second


def test_files_have_mode_0600_and_directory_0700(directory: Path) -> None:
    store = FileStateStore(directory)
    store.save(make_state("a"))
    store.save(make_state("b"))
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.backup_path.stat().st_mode) == 0o600


def test_leftover_temp_file_does_not_affect_load(directory: Path) -> None:
    store = FileStateStore(directory)
    store.save(make_state("a"))
    (directory / ".state.json.abc123.tmp").write_bytes(b"half written")
    loaded = store.load()
    assert isinstance(loaded, Loaded)
    assert loaded.state == make_state("a")


@pytest.mark.parametrize(
    ("main", "backup", "backup_valid"),
    [
        (b"{", "valid", True),
        (b"{", "corrupt", False),
        (b"{", None, False),
        (None, "valid", True),
        (None, "corrupt", False),
    ],
)
def test_corrupt_or_missing_main_loads_as_corrupt_and_changes_nothing(
    directory: Path, main: bytes | None, backup: str | None, backup_valid: bool
) -> None:
    directory.mkdir()
    if main is not None:
        (directory / "state.json").write_bytes(main)
    if backup == "valid":
        (directory / "state.json.bak").write_bytes(serialize_state(make_state("a")))
    elif backup == "corrupt":
        (directory / "state.json.bak").write_bytes(b"nope")
    before = snapshot(directory)

    result = FileStateStore(directory).load()

    assert isinstance(result, Corrupt)
    assert result.backup_valid is backup_valid
    assert result.reason
    assert snapshot(directory) == before


def test_unsupported_schema_version_loads_as_corrupt(directory: Path) -> None:
    directory.mkdir()
    (directory / "state.json").write_bytes(b'{"schema_version": 2}')
    result = FileStateStore(directory).load()
    assert isinstance(result, Corrupt)
    assert "unsupported schema_version 2" in result.reason


def test_save_refuses_to_replace_corrupt_state(directory: Path) -> None:
    store = FileStateStore(directory)
    store.save(make_state("a"))
    store.save(make_state("b"))
    store.state_path.write_bytes(b"{")
    backup = store.backup_path.read_bytes()

    with pytest.raises(CorruptStateError) as info:
        store.save(State.empty())

    assert info.value.backup_valid is True
    assert store.state_path.read_bytes() == b"{"
    assert store.backup_path.read_bytes() == backup
    assert [p.name for p in directory.iterdir() if p.name.endswith(".tmp")] == []


def test_save_refuses_when_state_is_missing_but_a_backup_exists(directory: Path) -> None:
    directory.mkdir()
    (directory / "state.json.bak").write_bytes(serialize_state(make_state("a")))
    with pytest.raises(CorruptStateError):
        FileStateStore(directory).save(State.empty())
    assert not (directory / "state.json").exists()


def test_crash_during_replace_keeps_the_previous_state_and_leaves_no_temp_file(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FileStateStore(directory)
    store.save(make_state("a"))
    real_replace = os.replace

    def failing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst).name == "state.json":
            message = "simulated crash"
            raise OSError(message)
        real_replace(src, dst)

    monkeypatch.setattr("enji_guard_radar.state_store.os.replace", failing_replace)
    with pytest.raises(OSError, match="simulated crash"):
        store.save(make_state("b"))
    monkeypatch.undo()

    loaded = store.load()
    assert isinstance(loaded, Loaded)
    assert loaded.state == make_state("a")
    assert [p.name for p in directory.iterdir() if p.name.endswith(".tmp")] == []


def test_second_lock_is_refused_at_once_and_succeeds_after_release(directory: Path) -> None:
    first, second = FileStateStore(directory), FileStateStore(directory)
    with first.lock() as held:
        assert held is True
        with second.lock() as also_held:
            assert also_held is False
    with second.lock() as after_release:
        assert after_release is True


def test_lock_creates_a_missing_directory_with_mode_0700(directory: Path) -> None:
    with FileStateStore(directory).lock() as held:
        assert held is True
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_dry_run_store_creates_nothing_when_the_directory_is_absent(directory: Path) -> None:
    dry = DryRunStateStore(FileStateStore(directory))
    assert isinstance(dry.load(), Missing)
    dry.save(make_state("a"))
    with dry.lock() as held:
        assert held is True
    assert not directory.exists()


def test_dry_run_store_leaves_existing_files_untouched_and_loads_the_real_state(
    directory: Path,
) -> None:
    real = FileStateStore(directory)
    real.save(make_state("a"))
    real.save(make_state("b"))
    before = snapshot(directory)

    dry = DryRunStateStore(real)
    loaded = dry.load()
    assert isinstance(loaded, Loaded)
    assert loaded.state == make_state("b")
    dry.save(make_state("c"))
    with dry.lock() as held:
        assert held is True

    assert snapshot(directory) == before


def test_dry_run_store_reports_corrupt_state_like_the_real_store(directory: Path) -> None:
    directory.mkdir()
    (directory / "state.json").write_bytes(b"{")
    assert isinstance(DryRunStateStore(FileStateStore(directory)).load(), Corrupt)
