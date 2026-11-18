"""03-09: scores, tooling observations and the resolved log are collected in every run.

Tracer through cli.main on the fixtures, then pipeline rows over in-memory worlds.
"""

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from guard_fakes import InMemoryGuardTools, Key, Response, call_key, report_page, run_item, world
from telegram_fakes import FakeTransport
from test_high_pipeline import (
    BASE,
    CALM_CONFIG,
    PID,
    PROJECT,
    REPO,
    REPO2,
    RID,
    SECURITY,
    Accepting,
    Sim,
    assert_quiet,
    stamp,
    texts,
)
from test_live_run_e2e import A_ID, BASELINE_NOW, load_state, run_live

from enji_guard_radar.config import DEFAULT_TOOLING_AUDIT_TYPES, parse_config
from enji_guard_radar.guard import (
    ARG_CURSOR,
    ARG_PROJECT_ID,
    ARG_REPOSITORY_ID,
    TOOL_LIST_AUDIT_RUNS,
    TOOL_LIST_PROJECTS,
    TOOL_LIST_REPOSITORIES,
)
from enji_guard_radar.high_rules import skip_report_for
from enji_guard_radar.ledger import (
    Loaded,
    RunPoint,
    ScoreRecord,
    SeriesHistory,
    State,
    ToolingRecord,
    serialize_state,
)
from enji_guard_radar.pipeline import Deps, RunOutcome, run_once
from enji_guard_radar.ports import Delivered, GuardCallError, SendOutcome
from enji_guard_radar.state_store import DryRunStateStore, FileStateStore

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


# ---- edge table: in-memory worlds through run_once --------------------------------------------

AI_READINESS = "audit.ai-readiness"
TESTS = "audit.tests"
DEAD_CODE = "audit.dead-code"
SCOPED_TO_SECURITY = "high:\n  audit_types:\n    - audit.security\n"
THRESHOLD_50 = "scores:\n  ai_readiness_below: 50\n"


def day_clock(day: int) -> datetime:
    return BASE + timedelta(days=day, hours=2)


def add_scored_run(
    sim: Sim, action_key: str, score: int, *, day: int, report: bool = True, rid: str = RID
) -> str:
    task_id = sim.add_run(action_key, day=day, report=False, rid=rid)
    if report:
        sim.reports[task_id] = report_page(task_id, high=0, score=score)
    return task_id


def score_of(sim: Sim, action_key: str, rid: str = RID) -> ScoreRecord | None:
    found = [s for s in sim.state().scores if s.key() == (PID, rid, action_key)]
    return found[0] if found else None


def tooling_of(sim: Sim, action_key: str, rid: str = RID) -> ToolingRecord:
    (found,) = [t for t in sim.state().tooling if t.key() == (PID, rid, action_key)]
    return found


def frozen_scores_and_tooling(sim: Sim) -> bytes:
    state = sim.state()
    return serialize_state(State(scores=state.scores, tooling=state.tooling))


def test_threshold_flags_below_and_reads_once(tmp_path: Path) -> None:
    sim = Sim(tmp_path, CALM_CONFIG + SCOPED_TO_SECURITY + THRESHOLD_50)
    sim.poll(0)
    low_task = add_scored_run(sim, AI_READINESS, 40, day=1)

    first = sim.poll(1)
    assert sim.report_reads() == [low_task]
    (low,) = first.low_scores
    assert (low.repo_id, low.action_key, low.score, low.threshold) == (RID, AI_READINESS, 40, 50)
    assert low.completed_at == BASE + timedelta(days=1)

    second = sim.poll(2)
    assert sim.report_reads() == []
    assert second.low_scores == first.low_scores

    add_scored_run(sim, AI_READINESS, 70, day=3)
    assert sim.poll(3).low_scores == ()
    score = score_of(sim, AI_READINESS)
    assert score is not None
    assert score.score == 70

    add_scored_run(sim, AI_READINESS, 30, day=4)
    (again,) = sim.poll(4).low_scores
    assert again.score == 30


def test_threshold_equal_to_score_is_not_low(tmp_path: Path) -> None:
    sim = Sim(tmp_path, CALM_CONFIG + THRESHOLD_50)
    sim.poll(0)
    add_scored_run(sim, AI_READINESS, 50, day=1)
    assert sim.poll(1).low_scores == ()


def test_no_threshold_keeps_the_record_and_flags_nothing(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    add_scored_run(sim, AI_READINESS, 10, day=1)
    report = sim.poll(1)
    assert report.low_scores == ()
    score = score_of(sim, AI_READINESS)
    assert score is not None
    assert score.score == 10


def test_threshold_zero_override_keeps_the_record_and_flags_nothing(tmp_path: Path) -> None:
    other_id, other_rid = "p-2", "repo_r-9"
    config = parse_config(
        CALM_CONFIG + THRESHOLD_50 + f'projects:\n  "{PID}":\n    ai_readiness_below: 0\n',
        source="test",
    )
    runs = {
        RID: [
            run_item("t-1", AI_READINESS, stamp(1)),
            run_item("t-0", "audit.recon", stamp(0)),
        ],
        other_rid: [
            run_item("t-3", AI_READINESS, stamp(1)),
            run_item("t-2", "audit.recon", stamp(0)),
        ],
    }
    reports = {task: report_page(task, high=0, score=40) for task in ("t-1", "t-3")}
    guard = InMemoryGuardTools(
        world(
            [(PID, PROJECT), (other_id, "Other")],
            {PID: [(RID, REPO)], other_id: [(other_rid, REPO2)]},
            runs,
            reports,
        )
    )
    store = FileStateStore(tmp_path / "state")
    report = run_once(
        Deps(
            guard=guard,
            notifier=Accepting(),
            config=config,
            clock=lambda: day_clock(2),
            store=store,
        )
    )
    assert [(low.project_id, low.score) for low in report.low_scores] == [(other_id, 40)]
    load = store.load()
    assert isinstance(load, Loaded)
    assert {(s.project_id, s.score) for s in load.state.scores} == {(PID, 40), (other_id, 40)}


def test_unknown_report_never_flags_and_is_read_again(tmp_path: Path) -> None:
    sim = Sim(tmp_path, CALM_CONFIG + THRESHOLD_50)
    sim.poll(0)
    task_id = add_scored_run(sim, AI_READINESS, 40, day=1, report=False)
    assert sim.poll(1).low_scores == ()
    assert sim.report_reads() == [task_id]
    assert score_of(sim, AI_READINESS) is None
    assert sim.poll(2).low_scores == ()
    assert sim.report_reads() == [task_id]
    sim.reports[task_id] = report_page(task_id, high=0, score=40)
    assert len(sim.poll(3).low_scores) == 1
    assert sim.report_reads() == [task_id]


def test_unknown_newer_report_keeps_the_stored_score(tmp_path: Path) -> None:
    sim = Sim(tmp_path, CALM_CONFIG + THRESHOLD_50)
    sim.poll(0)
    add_scored_run(sim, AI_READINESS, 40, day=1)
    sim.poll(1)
    before = score_of(sim, AI_READINESS)
    newer = add_scored_run(sim, AI_READINESS, 90, day=2, report=False)
    report = sim.poll(2)
    assert sim.report_reads() == [newer]
    assert score_of(sim, AI_READINESS) == before
    assert [low.score for low in report.low_scores] == [40]


def test_report_counted_for_high_but_not_for_score_is_read_once(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    task_id = add_scored_run(sim, TESTS, 79, day=1)
    sim.poll(1)
    assert sim.report_reads() == [task_id]
    assert [p.task_id for p in sim.history(TESTS).points] == [task_id]

    store = FileStateStore(sim.state_dir)
    store.save(sim.state().model_copy(update={"scores": ()}))
    assert score_of(sim, TESTS) is None

    sim.poll(2)
    assert sim.report_reads() == [task_id]
    score = score_of(sim, TESTS)
    assert score is not None
    assert score.score == 79
    sim.poll(3)
    assert sim.report_reads() == []


def test_default_config_unchanged_run_costs_no_report_read(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    add_scored_run(sim, AI_READINESS, 60, day=1)
    sim.add_run(high=1, day=1)
    sim.poll(1)
    sim.poll(2)
    assert sim.report_reads() == []


def test_score_outside_high_scope_is_read_until_recorded(tmp_path: Path) -> None:
    sim = Sim(tmp_path, CALM_CONFIG + SCOPED_TO_SECURITY)
    sim.poll(0)
    task_id = add_scored_run(sim, TESTS, 79, day=1)
    sim.poll(1)
    assert sim.report_reads() == [task_id]
    assert [h for h in sim.state().histories if h.action_key == TESTS] == []
    sim.poll(2)
    assert sim.report_reads() == []


def _seed_scores_and_tooling(sim: Sim) -> None:
    sim.poll(0)
    add_scored_run(sim, AI_READINESS, 40, day=1)
    sim.add_run(DEAD_CODE, day=1)
    sim.poll(1)
    assert score_of(sim, AI_READINESS) is not None
    assert tooling_of(sim, DEAD_CODE).last_completed_at is not None


def test_paused_project_frozen_keeps_scores_and_tooling(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    _seed_scores_and_tooling(sim)
    before = frozen_scores_and_tooling(sim)
    sim.paused = True
    add_scored_run(sim, AI_READINESS, 10, day=2)
    sim.poll(2)
    assert frozen_scores_and_tooling(sim) == before


def test_excluded_project_frozen_keeps_scores_and_tooling(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    _seed_scores_and_tooling(sim)
    before = frozen_scores_and_tooling(sim)
    sim.config_text = CALM_CONFIG + f'projects:\n  "{PID}":\n    mode: excluded\n'
    add_scored_run(sim, AI_READINESS, 10, day=2)
    sim.poll(2)
    assert frozen_scores_and_tooling(sim) == before


def test_unknown_project_frozen_keeps_scores_and_tooling(tmp_path: Path) -> None:
    failing_listing: dict[Key, Response] = {
        call_key(TOOL_LIST_REPOSITORIES, **{ARG_PROJECT_ID: PID}): GuardCallError("outage")
    }
    broken_second_page: dict[Key, Response] = {
        call_key(TOOL_LIST_AUDIT_RUNS, **{ARG_REPOSITORY_ID: RID}): {
            "runs": [],
            "has_more": True,
            "next_cursor": "c2",
        },
        call_key(
            TOOL_LIST_AUDIT_RUNS, **{ARG_REPOSITORY_ID: RID, ARG_CURSOR: "c2"}
        ): GuardCallError("outage"),
    }
    for name, failures in (("repos", failing_listing), ("runs", broken_second_page)):
        sim = Sim(tmp_path / name)
        _seed_scores_and_tooling(sim)
        before = frozen_scores_and_tooling(sim)
        sim.failures = failures
        add_scored_run(sim, AI_READINESS, 10, day=2)
        sim.poll(2)
        assert frozen_scores_and_tooling(sim) == before


def test_failed_listing_frozen_keeps_scores_and_tooling(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    _seed_scores_and_tooling(sim)
    before = frozen_scores_and_tooling(sim)
    sim.failures = {call_key(TOOL_LIST_PROJECTS): GuardCallError("outage")}
    report = sim.poll(2)
    assert report.outcome is RunOutcome.GUARD_UNAVAILABLE
    assert report.low_scores == ()
    assert report.tooling_gaps == ()
    assert frozen_scores_and_tooling(sim) == before


def test_tooling_gaps_close_with_a_run_and_observed_since_stays(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    first = sim.poll(0)
    assert {gap.action_key for gap in first.tooling_gaps} == set(DEFAULT_TOOLING_AUDIT_TYPES)
    assert all(gap.last_completed_at is None for gap in first.tooling_gaps)
    assert all(gap.window_days == 60 for gap in first.tooling_gaps)

    sim.add_run(DEAD_CODE, day=1)
    second = sim.poll(1)
    assert DEAD_CODE not in {gap.action_key for gap in second.tooling_gaps}
    assert len(second.tooling_gaps) == len(DEFAULT_TOOLING_AUDIT_TYPES) - 1
    record = tooling_of(sim, DEAD_CODE)
    assert record.last_completed_at == BASE + timedelta(days=1)
    assert record.observed_since == day_clock(0)

    sim.poll(3)
    assert tooling_of(sim, DEAD_CODE).observed_since == day_clock(0)
    assert tooling_of(sim, "audit.security").observed_since == day_clock(0)


def test_tooling_window_override_decides_the_gap(tmp_path: Path) -> None:
    narrow = Sim(
        tmp_path / "narrow", CALM_CONFIG + f'projects:\n  "{PID}":\n    tooling_window_days: 7\n'
    )
    wide = Sim(tmp_path / "wide")
    for sim in (narrow, wide):
        sim.add_run(DEAD_CODE, day=0)
    narrow_gaps = {gap.action_key: gap for gap in narrow.poll(10).tooling_gaps}
    wide_gaps = {gap.action_key for gap in wide.poll(10).tooling_gaps}
    assert narrow_gaps[DEAD_CODE].age_days == 10
    assert narrow_gaps[DEAD_CODE].window_days == 7
    assert narrow_gaps[DEAD_CODE].last_completed_at == BASE
    assert DEAD_CODE not in wide_gaps


def test_resolved_log_gets_one_item_per_closed_series(tmp_path: Path) -> None:
    sim = Sim(tmp_path)
    sim.poll(0)
    sim.add_run(high=3, day=1)
    sim.poll(1)
    sim.add_run(high=0, day=2)
    sim.poll(2)
    assert sim.state().resolved == ()
    sim.add_run(high=0, day=3)
    report = sim.poll(3)
    assert [key.level for key in report.resolved] == ["high"]
    (item,) = sim.state().resolved
    assert (item.key.level, item.key.repo_id, item.key.action_key) == ("high", RID, SECURITY)
    assert item.resolved_at == day_clock(3)
    sim.poll(4)
    assert sim.state().resolved == (item,)


def test_resolved_log_records_a_project_no_longer_stale(tmp_path: Path) -> None:
    sim = Sim(tmp_path, "defaults:\n  stale_after_days: 3\n")
    sim.poll(0)
    assert sim.state().resolved == ()
    assert len(texts(sim.poll(5))) == 1
    sim.add_run(DEAD_CODE, day=6)
    fresh = sim.poll(6)
    assert [key.level for key in fresh.resolved] == ["stale"]
    (item,) = sim.state().resolved
    assert item.key.level == "stale"
    assert item.resolved_at == day_clock(6)


def test_resolved_log_is_on_disk_before_a_message_is_sent(tmp_path: Path) -> None:
    sim = Sim(tmp_path, "defaults:\n  stale_after_days: 3\n")
    sim.poll(0)
    sim.poll(5)
    seen: list[int] = []

    class Peeking:
        def send(self, message: object) -> SendOutcome:
            load = FileStateStore(sim.state_dir).load()
            assert isinstance(load, Loaded)
            seen.append(len(load.state.resolved))
            return Delivered(message_id=1)

    sim.add_run(DEAD_CODE, day=6)
    sim.add_run(high=2, day=6)
    report = sim.poll(6, notifier=Peeking())
    assert len(report.messages) == 1
    assert seen == [1]


def test_dry_run_computes_but_writes_nothing(tmp_path: Path) -> None:
    sim = Sim(tmp_path, CALM_CONFIG + THRESHOLD_50)
    sim.poll(0)
    sim.poll(1)
    store = FileStateStore(sim.state_dir)
    before = (store.state_path.read_bytes(), store.backup_path.read_bytes())

    add_scored_run(sim, AI_READINESS, 20, day=2)
    guard = InMemoryGuardTools(
        world([(PID, PROJECT)], {PID: sim.repos}, sim.runs, sim.reports, sim.failures)
    )
    report = run_once(
        Deps(
            guard=guard,
            notifier=Accepting(),
            config=sim.config,
            clock=lambda: day_clock(2),
            store=DryRunStateStore(store),
        )
    )
    assert [low.score for low in report.low_scores] == [20]
    assert report.tooling_gaps != ()
    assert (store.state_path.read_bytes(), store.backup_path.read_bytes()) == before


def test_nothing_sent_for_a_low_score_or_a_tooling_gap(tmp_path: Path) -> None:
    sim = Sim(tmp_path, CALM_CONFIG + THRESHOLD_50)
    sim.poll(0)
    add_scored_run(sim, AI_READINESS, 10, day=1)
    report = sim.poll(2)
    assert report.low_scores != ()
    assert report.tooling_gaps != ()
    assert report.messages == ()
    assert (report.delivered, report.printed, report.not_delivered) == (0, 0, 0)
    assert_quiet(report)


def _score(action_key: str, task_id: str) -> ScoreRecord:
    return ScoreRecord(
        project_id=PID,
        repo_id=RID,
        action_key=action_key,
        task_id=task_id,
        completed_at=BASE,
        score=70,
    )


def _history(action_key: str, task_id: str) -> SeriesHistory:
    point = RunPoint(task_id=task_id, completed_at=BASE, high=0)
    return SeriesHistory(project_id=PID, repo_id=RID, action_key=action_key, points=(point,))


@pytest.mark.parametrize(
    ("audit_types", "scores", "history", "action_key", "task_id", "skipped"),
    [
        # Without scores the rule is the High one only.
        (("audit.security",), None, {}, TESTS, "t-1", True),
        (None, None, {}, TESTS, "t-1", False),
        # A score audit type is read until its task_id is recorded, High scope or not.
        (("audit.security",), {}, {}, TESTS, "t-1", False),
        (("audit.security",), {TESTS: _score(TESTS, "t-0")}, {}, TESTS, "t-1", False),
        (("audit.security",), {TESTS: _score(TESTS, "t-1")}, {}, TESTS, "t-1", True),
        # Recorded as a score but still needed by the High rules: the High rule decides.
        (None, {TESTS: _score(TESTS, "t-1")}, {}, TESTS, "t-1", False),
        (None, {TESTS: _score(TESTS, "t-1")}, {TESTS: _history(TESTS, "t-1")}, TESTS, "t-1", True),
        # Not a score audit type: unchanged.
        (None, {}, {}, SECURITY, "t-1", False),
        (None, {}, {SECURITY: _history(SECURITY, "t-1")}, SECURITY, "t-1", True),
        (("audit.tests",), {}, {}, SECURITY, "t-1", True),
        (None, {}, {}, "audit.recon", "t-1", True),
    ],
)
def test_skip_report_for_with_scores_table(
    audit_types: tuple[str, ...] | None,
    scores: dict[str, ScoreRecord] | None,
    history: dict[str, SeriesHistory],
    action_key: str,
    task_id: str,
    skipped: bool,
) -> None:
    stored_scores = None if scores is None else {(PID, RID, k): v for k, v in scores.items()}
    stored_history = {(PID, RID, k): v for k, v in history.items()}
    skip = skip_report_for(PID, stored_history, audit_types, stored_scores)
    assert skip(RID, action_key, task_id) is skipped
