"""State schema 3: a Phase 2 file upgrades through a live run; the new blocks are tabled below."""

import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telegram_fakes import FakeTransport
from test_live_run_e2e import BASELINE_NOW, load_state, run_live

from enji_guard_radar.ledger import PollHealth, RunPoint, SeriesHistory

T0 = datetime(2026, 11, 16, 12, tzinfo=UTC)
T1 = datetime(2026, 11, 17, 12, tzinfo=UTC)
UNLISTED_ID = "00000000-0000-4000-8000-00000000ffff"


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def _unlisted_history() -> SeriesHistory:
    return SeriesHistory(
        project_id=UNLISTED_ID,
        repo_id="r1",
        action_key="audit.security",
        points=(
            RunPoint(task_id="t1", completed_at=T0, high=2),
            RunPoint(task_id="t2", completed_at=T1, high=3),
        ),
        open=True,
        opened_at=T0,
        clean_runs=0,
    )


def test_v2_state_file_upgrades_through_a_live_run(tmp_path: Path) -> None:
    history = _unlisted_history()
    document = {
        "schema_version": 2,
        "projects": [],
        "conditions": [],
        "histories": [json.loads(history.model_dump_json())],
        "health": {},
    }
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(json.dumps(document), encoding="utf-8")

    transport = FakeTransport([])
    code, _ = run_live(tmp_path, BASELINE_NOW, transport)

    assert code == 0
    assert transport.requests == []
    state = load_state(tmp_path)
    assert state.schema_version == 3
    assert [h for h in state.histories if h.project_id == UNLISTED_ID] == [history]
    assert state.health == PollHealth()
