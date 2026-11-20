"""Triage cost record, its two log lines and the cost file store. No Claude, no network."""

import logging
import stat
from datetime import date, timedelta
from pathlib import Path

import pytest

from enji_guard_radar.triage import TriageFailed, TriageFailure, TriageReply
from enji_guard_radar.triage_cost import (
    KEEP_DAYS,
    CostDay,
    add_call,
    call_line,
    daily_line,
    deserialize_days,
    serialize_days,
)
from enji_guard_radar.triage_cost_store import (
    COST_FILE,
    FileTriageCostStore,
    ReadOnlyTriageCostStore,
)

DAY = date(2026, 11, 19)
OK = TriageReply(text="x", seconds=4.2, cost_usd=0.003, input_tokens=900, output_tokens=40)
UNKNOWN = TriageReply(text="x", seconds=1.0)
TIMEOUT = TriageFailed(reason=TriageFailure.TIMEOUT, seconds=60)


def test_add_one_call() -> None:
    days = add_call((), OK, today=DAY)
    assert len(days) == 1
    assert days[0].day == DAY
    assert (days[0].calls, days[0].failures) == (1, 0)
    assert days[0].cost_usd == 0.003
    assert call_line(2, OK) == (
        "triage call: items 2, outcome ok, seconds 4.2, cost_usd 0.003, "
        "input_tokens 900, output_tokens 40"
    )
    assert daily_line(days[0]) == "triage today (UTC): 1 calls, 0 failed, $0.003"


def test_unknown_cost_and_tokens_lines() -> None:
    assert call_line(1, UNKNOWN) == (
        "triage call: items 1, outcome ok, seconds 1.0, cost_usd unknown, "
        "input_tokens unknown, output_tokens unknown"
    )
    days = add_call((), UNKNOWN, today=DAY)
    assert days[0].unknown_cost_calls == 1
    assert daily_line(days[0]) == "triage today (UTC): 1 calls, 0 failed, $unknown"


def test_unknown_mixed_sums_known() -> None:
    known = TriageReply(text="x", seconds=1.0, cost_usd=0.01)
    days = add_call(add_call((), known, today=DAY), UNKNOWN, today=DAY)
    assert (days[0].calls, days[0].unknown_cost_calls) == (2, 1)
    assert daily_line(days[0]) == "triage today (UTC): 2 calls, 0 failed, $0.01"


def test_failed_timeout_counts() -> None:
    assert call_line(3, TIMEOUT) == (
        "triage call: items 3, outcome timeout, seconds 60.0, cost_usd unknown, "
        "input_tokens unknown, output_tokens unknown"
    )
    days = add_call(add_call((), UNKNOWN, today=DAY), TIMEOUT, today=DAY)
    assert (days[0].calls, days[0].failures, days[0].seconds) == (2, 1, 61.0)
    assert daily_line(days[0]) == "triage today (UTC): 2 calls, 1 failed, $unknown"


def test_failed_call_with_reported_cost_adds_it() -> None:
    failed = TriageFailed(
        reason=TriageFailure.EXIT_NONZERO, seconds=2.0, cost_usd=0.5, input_tokens=10
    )
    days = add_call((), failed, today=DAY)
    assert (days[0].failures, days[0].cost_usd, days[0].unknown_cost_calls) == (1, 0.5, 0)
    assert (days[0].input_tokens, days[0].output_tokens) == (10, 0)


@pytest.mark.parametrize(
    ("cost", "text"),
    [(0.003, "0.003"), (0.0, "0"), (0.1 + 0.2, "0.3"), (12.0, "12"), (1.5, "1.5")],
)
def test_money_format_rows(cost: float, text: str) -> None:
    reply = TriageReply(text="x", seconds=1.0, cost_usd=cost)
    assert f"cost_usd {text}," in call_line(1, reply)
    assert daily_line(add_call((), reply, today=DAY)[0]).endswith(f"${text}")


def test_days_new_day_starts_new_record() -> None:
    first = add_call((), OK, today=DAY)
    both = add_call(first, OK, today=DAY + timedelta(days=1))
    assert [d.day for d in both] == [DAY, DAY + timedelta(days=1)]
    assert [d.calls for d in both] == [1, 1]


def test_days_same_day_accumulates() -> None:
    days = add_call(add_call((), OK, today=DAY), OK, today=DAY)
    assert len(days) == 1
    assert (days[0].calls, days[0].seconds, days[0].input_tokens) == (2, 8.4, 1800)
    assert days[0].cost_usd == pytest.approx(0.006)


def test_days_keep_newest_35_of_40() -> None:
    days: tuple[CostDay, ...] = ()
    start = date(2026, 1, 1)
    for i in range(40):
        days = add_call(days, OK, today=start + timedelta(days=i))
    assert KEEP_DAYS == 35
    assert len(days) == 35
    assert (days[0].day, days[-1].day) == (date(2026, 1, 6), date(2026, 2, 9))


def test_days_input_not_mutated() -> None:
    before = add_call((), OK, today=DAY)
    snapshot = tuple(before)
    add_call(before, OK, today=DAY)
    assert before == snapshot
    assert before[0].calls == 1


def test_round_trip() -> None:
    days = add_call(add_call((), OK, today=DAY), TIMEOUT, today=DAY + timedelta(days=1))
    assert deserialize_days(serialize_days(days)) == days
    assert deserialize_days(serialize_days(())) == ()


GOOD_DAY = (
    '{"day": "2026-11-19", "calls": 1, "failures": 0, "seconds": 1.0, "cost_usd": 0.0, '
    '"unknown_cost_calls": 1, "input_tokens": 0, "output_tokens": 0'
)


@pytest.mark.parametrize(
    "document",
    [
        b'{"version": 2, "days": []}',
        b'{"days": []}',
        b'{"version": 1}',
        b'{"version": 1, "days": [], "extra": 1}',
        b'{"version": 1, "days": [{"day": "2026-11-19", "calls": 1}]}',
        b'{"version": 1, "days": [' + GOOD_DAY.encode() + b', "extra": 1}]}',
        b"{",
        b"\xff\xfe",
        b"[]",
    ],
)
def test_round_trip_rejects_bad_documents(document: bytes) -> None:
    with pytest.raises(ValueError, match=r"."):
        deserialize_days(document)


def test_round_trip_accepts_the_good_day() -> None:
    document = b'{"version": 1, "days": [' + GOOD_DAY.encode() + b"}]}"
    assert deserialize_days(document)[0].day == DAY


def test_store_save_then_load(tmp_path: Path) -> None:
    store = FileTriageCostStore(tmp_path)
    days = add_call((), OK, today=DAY)
    store.save(days)
    assert store.load() == days


def test_store_creates_missing_directory(tmp_path: Path) -> None:
    store = FileTriageCostStore(tmp_path / "state")
    store.save(add_call((), OK, today=DAY))
    assert (tmp_path / "state" / COST_FILE).exists()


def test_store_file_mode_0600(tmp_path: Path) -> None:
    FileTriageCostStore(tmp_path).save(add_call((), OK, today=DAY))
    assert stat.S_IMODE((tmp_path / COST_FILE).stat().st_mode) == 0o600


def test_store_missing_file_loads_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        assert FileTriageCostStore(tmp_path).load() == ()
    assert caplog.records == []
    assert list(tmp_path.iterdir()) == []


def test_store_corrupt_file_warns_once_and_loads_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / COST_FILE).write_text("{")
    store = FileTriageCostStore(tmp_path)
    with caplog.at_level(logging.DEBUG):
        assert store.load() == ()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage().startswith("triage cost file unreadable, starting a new one: ")
    days = add_call((), OK, today=DAY)
    store.save(days)
    assert store.load() == days


def test_store_unreadable_file_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / COST_FILE).mkdir()
    with caplog.at_level(logging.WARNING):
        assert FileTriageCostStore(tmp_path).load() == ()
    assert [r.getMessage() for r in caplog.records] == [
        "triage cost file unreadable, starting a new one: IsADirectoryError"
    ]


def test_store_save_failure_warns_with_error_class(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    store = FileTriageCostStore(blocker / "state")
    with caplog.at_level(logging.WARNING):
        store.save(add_call((), OK, today=DAY))
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert messages[0].startswith("triage cost not written: ")
    assert messages[0].endswith("Error")


def test_store_writes_only_the_cost_file(tmp_path: Path) -> None:
    FileTriageCostStore(tmp_path).save(add_call((), OK, today=DAY))
    assert sorted(p.name for p in tmp_path.iterdir()) == [COST_FILE]


def test_store_read_only_save_keeps_absent_file_absent(tmp_path: Path) -> None:
    ReadOnlyTriageCostStore(FileTriageCostStore(tmp_path)).save(add_call((), OK, today=DAY))
    assert list(tmp_path.iterdir()) == []


def test_store_read_only_save_keeps_existing_bytes(tmp_path: Path) -> None:
    real = FileTriageCostStore(tmp_path)
    real.save(add_call((), OK, today=DAY))
    before = (tmp_path / COST_FILE).read_bytes()
    ReadOnlyTriageCostStore(real).save(())
    assert (tmp_path / COST_FILE).read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [COST_FILE]


def test_store_read_only_load_delegates(tmp_path: Path) -> None:
    real = FileTriageCostStore(tmp_path)
    days = add_call((), OK, today=DAY)
    real.save(days)
    assert ReadOnlyTriageCostStore(real).load() == days


def test_store_read_only_logs_debug(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        ReadOnlyTriageCostStore(FileTriageCostStore(tmp_path)).save(())
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, "dry-run: triage cost not written")
    ]
