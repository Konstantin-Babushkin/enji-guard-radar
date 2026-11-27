"""Which settings a poll uses (sheet, last good copy or YAML alone) and what ops is told."""

from datetime import UTC, datetime, timedelta

import pytest
from sheet_fakes import (
    PROJECT_A,
    FakeSheetReader,
    MemorySheetStore,
    projects_grid,
    sheet,
)

from enji_guard_radar.config import RadarConfig, parse_config
from enji_guard_radar.ports import SheetFailure
from enji_guard_radar.sheet_sync import (
    ConfigSource,
    SheetOutage,
    SheetRecord,
    SheetRecovered,
    SheetSettingsSource,
    describe_source,
    sheet_ops_message,
)

T0 = datetime(2026, 11, 27, 10, tzinfo=UTC)
GOOD = sheet(projects_grid({"project_id": PROJECT_A, "mode": "paused", "stale_after_days": "5"}))
BAD = sheet(projects_grid({"project_id": PROJECT_A, "stale_after_days": "0"}))


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def base(text: str = "") -> RadarConfig:
    return parse_config(text, source="test")


def make(*answers: object) -> tuple[SheetSettingsSource, FakeSheetReader, MemorySheetStore]:
    reader = FakeSheetReader(*answers)  # type: ignore[arg-type]
    store = MemorySheetStore()
    return SheetSettingsSource(reader, store), reader, store


def test_a_valid_sheet_is_merged_and_kept_as_the_last_good_copy() -> None:
    source, _, store = make(GOOD)
    resolution = source.resolve(base(), T0)
    assert resolution.source is ConfigSource.SHEET
    assert resolution.config.mode_for(PROJECT_A) == "paused"
    assert resolution.action is None
    assert store.record.last_good is not None
    assert store.record.last_good.fetched_at == T0
    assert store.record.last_good.data == GOOD


def test_the_stored_copy_is_merged_over_the_current_yaml_not_over_the_old_one() -> None:
    source, reader, _ = make(GOOD)
    source.resolve(base(), T0)
    reader.set(SheetFailure.NETWORK)
    resolution = source.resolve(base("defaults:\n  high_aged_after_days: 40\n"), at(180))
    assert resolution.source is ConfigSource.LAST_GOOD_COPY
    assert resolution.config.mode_for(PROJECT_A) == "paused"
    assert resolution.config.defaults.high_aged_after_days == 40
    assert resolution.copy_fetched_at == T0


@pytest.mark.parametrize("answer", [SheetFailure.NETWORK, SheetFailure.DENIED, BAD])
def test_an_unreachable_or_invalid_sheet_keeps_the_last_good_copy(answer: object) -> None:
    source, reader, _ = make(GOOD)
    source.resolve(base(), T0)
    reader.set(answer)  # type: ignore[arg-type]
    resolution = source.resolve(base(), at(180))
    assert resolution.source is ConfigSource.LAST_GOOD_COPY
    assert resolution.config.mode_for(PROJECT_A) == "paused"
    assert resolution.failure == ("invalid" if answer is BAD else answer.value)  # type: ignore[attr-defined]


def test_an_invalid_sheet_is_never_applied_partly_and_never_replaces_the_copy() -> None:
    source, reader, store = make(GOOD)
    source.resolve(base(), T0)
    reader.set(BAD)
    resolution = source.resolve(base(), at(180))
    assert resolution.config.projects[PROJECT_A].stale_after_days == 5
    assert store.record.last_good is not None
    assert store.record.last_good.data == GOOD
    assert resolution.detail is not None
    assert "projects row 2 column stale_after_days" in resolution.detail


def test_no_copy_and_an_unusable_sheet_means_yaml_alone_and_an_alert_at_once() -> None:
    source, _, _ = make(SheetFailure.NETWORK)
    yaml_base = base("defaults:\n  stale_after_days: 20\n")
    resolution = source.resolve(yaml_base, T0)
    assert resolution.source is ConfigSource.YAML_FALLBACK
    assert resolution.config == yaml_base
    assert isinstance(resolution.action, SheetOutage)
    assert resolution.action.failures == 1


def test_a_stored_copy_that_no_longer_validates_counts_as_no_copy() -> None:
    """For example after an upgrade that tightened a range."""
    source, reader, store = make(GOOD)
    source.resolve(base(), T0)
    stored = store.record.last_good
    assert stored is not None
    store.record = store.record.model_copy(
        update={"last_good": stored.model_copy(update={"data": BAD})}
    )
    reader.set(SheetFailure.NETWORK)
    resolution = source.resolve(base(), at(180))
    assert resolution.source is ConfigSource.YAML_FALLBACK
    assert isinstance(resolution.action, SheetOutage)


def test_with_a_copy_the_ops_chat_hears_only_after_the_threshold_and_once() -> None:
    source, reader, _ = make(GOOD)
    config = base("ops:\n  failures_before_alert: 3\n")
    source.resolve(config, T0)
    reader.set(SheetFailure.NETWORK)
    first = source.resolve(config, at(10))
    second = source.resolve(config, at(20))
    third = source.resolve(config, at(30))
    assert first.action is None
    assert second.action is None
    assert isinstance(third.action, SheetOutage)
    assert third.action.failures == 3
    assert third.action.since == at(10)
    source.acknowledge(third, at(30))
    fourth = source.resolve(config, at(40))
    assert fourth.action is None


def test_an_unacknowledged_outage_message_is_retried_next_poll() -> None:
    source, _, _ = make(SheetFailure.NETWORK)
    first = source.resolve(base(), T0)
    assert isinstance(first.action, SheetOutage)
    second = source.resolve(base(), at(10))
    assert isinstance(second.action, SheetOutage)
    assert second.action.failures == 2
    assert second.action.since == T0


def test_one_recovery_message_after_the_first_good_read_and_retried_until_acknowledged() -> None:
    source, reader, store = make(SheetFailure.NETWORK)
    down = source.resolve(base(), T0)
    source.acknowledge(down, T0)
    reader.set(GOOD)
    back = source.resolve(base(), at(10))
    assert isinstance(back.action, SheetRecovered)
    assert back.action.since == T0
    again = source.resolve(base(), at(20))
    assert isinstance(again.action, SheetRecovered)
    source.acknowledge(again, at(20))
    assert store.record.health.outage_reported_at is None
    assert source.resolve(base(), at(30)).action is None


def test_a_streak_that_never_reached_the_ops_chat_ends_silently() -> None:
    source, reader, store = make(GOOD)
    config = base("ops:\n  failures_before_alert: 3\n")
    source.resolve(config, T0)
    reader.set(SheetFailure.NETWORK)
    source.resolve(config, at(10))
    reader.set(GOOD)
    back = source.resolve(config, at(20))
    assert back.action is None
    assert store.record.health.consecutive_failures == 0


class UnwritableStore(MemorySheetStore):
    def save(self, record: SheetRecord) -> None:
        raise OSError


def test_a_store_that_cannot_write_does_not_stop_the_poll() -> None:
    source = SheetSettingsSource(FakeSheetReader(GOOD), UnwritableStore())
    assert source.resolve(base(), T0).source is ConfigSource.SHEET


def test_the_source_line_says_where_the_settings_came_from() -> None:
    source, reader, _ = make(GOOD)
    assert describe_source(source.resolve(base(), T0)) == "live sheet over YAML"
    reader.set(SheetFailure.DENIED)
    copy_line = describe_source(source.resolve(base(), at(10)))
    assert copy_line.startswith("last good sheet copy from 2026-11-27 10:00 UTC")
    assert "denied" in copy_line
    fresh, _, _ = make(BAD)
    assert describe_source(fresh.resolve(base(), T0)).startswith("YAML only, no usable sheet copy")


@pytest.mark.parametrize("language", ["en", "ru"])
def test_ops_messages_name_the_reason_and_never_a_cell_value(language: str) -> None:
    detail = "projects row 4 column tests_below: Input should be less than or equal to 100"
    outage = SheetOutage("invalid", detail, T0, 3)
    message = sheet_ops_message(outage, language=language, copy_fetched_at=at(-60))  # type: ignore[arg-type]
    assert "projects row 4 column tests_below" in message.text
    assert "2026-11-27 09:00" in message.text
    assert message.audience.value == "ops"
    no_copy = sheet_ops_message(SheetOutage("network", None, T0, 1), language=language)  # type: ignore[arg-type]
    assert "radar.yaml" in no_copy.text
    recovered = sheet_ops_message(SheetRecovered(T0), language=language)  # type: ignore[arg-type]
    assert "2026-11-27 10:00" in recovered.text
