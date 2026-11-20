"""The triage cost file: atomic save, forgiving load. Shell module (does I/O).

It is not dedup state: a missing, corrupt or unwritable file never halts a run and never
touches the state file.
"""

import logging
from pathlib import Path

from enji_guard_radar.state_store import DIR_MODE, atomic_write
from enji_guard_radar.triage_cost import (
    CostDay,
    TriageCostStore,
    deserialize_days,
    serialize_days,
)

logger = logging.getLogger(__name__)

COST_FILE = "triage-cost.json"


class FileTriageCostStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def path(self) -> Path:
        return self._directory / COST_FILE

    def load(self) -> tuple[CostDay, ...]:
        try:
            return deserialize_days(self.path.read_bytes())
        except FileNotFoundError:
            return ()
        except (OSError, ValueError) as exc:
            logger.warning(
                "triage cost file unreadable, starting a new one: %s", type(exc).__name__
            )
            return ()

    def save(self, days: tuple[CostDay, ...]) -> None:
        try:
            self._directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            atomic_write(self._directory, self.path, serialize_days(days))
        except OSError as exc:
            logger.warning("triage cost not written: %s", type(exc).__name__)


class ReadOnlyTriageCostStore:
    """Dry-run: reads the real file, writes nothing."""

    def __init__(self, inner: TriageCostStore) -> None:
        self._inner = inner

    def load(self) -> tuple[CostDay, ...]:
        return self._inner.load()

    def save(self, days: tuple[CostDay, ...]) -> None:
        logger.debug("dry-run: triage cost not written")
