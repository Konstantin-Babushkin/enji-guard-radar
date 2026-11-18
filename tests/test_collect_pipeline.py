"""03-09: scores, tooling observations and the resolved log are collected in every run.

Tracer through cli.main on the fixtures, then pipeline rows over in-memory worlds.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telegram_fakes import FakeTransport
from test_live_run_e2e import A_ID, BASELINE_NOW, load_state, run_live

B1 = "repo_00000000-0000-4000-8000-00000000b001"
B2 = "repo_00000000-0000-4000-8000-00000000b002"


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def test_fixture_run_records_scores_and_tooling(tmp_path: Path) -> None:
    run_live(tmp_path, BASELINE_NOW, FakeTransport([]))
    state = load_state(tmp_path)
    scores = {record.key(): record for record in state.scores}
    tooling = {record.key(): record for record in state.tooling}

    assert scores[(A_ID, B2, "audit.ai-readiness")].score == 81
    assert scores[(A_ID, B2, "audit.tests")].score == 79
    assert [key for key in scores if key[1] == B1] == []

    clock = datetime(2026, 11, 16, 12, tzinfo=UTC)
    dead_code = tooling[(A_ID, B1, "audit.dead-code")]
    assert dead_code.last_completed_at == datetime(2026, 11, 9, 23, 36, 47, 368153, tzinfo=UTC)
    assert dead_code.observed_since == clock
    assert tooling[(A_ID, B1, "audit.implementation-unification")].last_completed_at is None

    raw = (tmp_path / "state" / "state.json").read_bytes()
    assert b"Project A" not in raw
    assert b"acme" not in raw

    run_live(tmp_path, BASELINE_NOW, FakeTransport([]))
    again = load_state(tmp_path)
    assert again.scores == state.scores
    assert again.tooling == state.tooling
