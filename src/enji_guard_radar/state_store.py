"""File-backed state: atomic save, strict load, one lock holder. Shell module (does I/O)."""

import contextlib
import fcntl
import logging
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from enji_guard_radar.ledger import (
    Corrupt,
    CorruptStateError,
    Loaded,
    Missing,
    State,
    StateFormatError,
    StateLoad,
    deserialize_state,
    serialize_state,
)

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"
BACKUP_FILE = "state.json.bak"
LOCK_FILE = "radar.lock"
FILE_MODE = 0o600
DIR_MODE = 0o700


def atomic_write(directory: Path, path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    """Write `data` to `path` (inside `directory`) so a reader sees the old or the new file."""
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.chmod(mode)
        os.replace(tmp, path)  # noqa: PTH105
        _fsync_directory(directory)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class FileStateStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def state_path(self) -> Path:
        return self._directory / STATE_FILE

    @property
    def backup_path(self) -> Path:
        return self._directory / BACKUP_FILE

    def load(self) -> StateLoad:
        """Never writes, creates or deletes anything."""
        result = self._read()
        if isinstance(result, Corrupt):
            logger.error("state file corrupt: %s (%s)", self.state_path, result.reason)
        return result

    def save(self, state: State) -> None:
        """Refuses to replace a corrupt state: the operator decides, nothing is reset."""
        current = self._read()
        if isinstance(current, Corrupt):
            logger.error("refusing to save over corrupt state: %s", current.reason)
            raise CorruptStateError(current.reason, current.backup_valid)
        self._make_directory()
        if isinstance(current, Loaded):
            atomic_write(self._directory, self.backup_path, serialize_state(current.state))
        atomic_write(self._directory, self.state_path, serialize_state(state))
        logger.info("state saved: %s (%d conditions)", self.state_path, len(state.conditions))

    @contextlib.contextmanager
    def lock(self) -> Iterator[bool]:
        """Yield True when this caller holds the lock, False at once when another does."""
        self._make_directory()
        fd = os.open(self._directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, FILE_MODE)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                logger.info("state lock is held by another run: %s", self._directory)
            yield acquired
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _make_directory(self) -> None:
        self._directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)

    def _read(self) -> StateLoad:
        try:
            data = self.state_path.read_bytes()
        except FileNotFoundError:
            if self.backup_path.exists():
                return Corrupt(
                    "state.json is missing but state.json.bak exists; "
                    "refusing to start from an empty state",
                    self._backup_valid(),
                )
            return Missing()
        except OSError as exc:
            return Corrupt(f"state.json is unreadable ({type(exc).__name__})", self._backup_valid())
        try:
            return Loaded(deserialize_state(data))
        except StateFormatError as exc:
            return Corrupt(exc.reason, self._backup_valid())

    def _backup_valid(self) -> bool:
        try:
            deserialize_state(self.backup_path.read_bytes())
        except (OSError, StateFormatError):
            return False
        return True


class DryRunStateStore:
    """Reads the real state; saves and locks touch nothing on disk."""

    def __init__(self, inner: FileStateStore) -> None:
        self._inner = inner

    def load(self) -> StateLoad:
        return self._inner.load()

    def save(self, state: State) -> None:
        logger.debug("dry-run: state not saved")

    @contextlib.contextmanager
    def lock(self) -> Iterator[bool]:
        yield True
