"""Which settings does this poll use: the sheet, its last good copy, or the YAML alone?

Core: the reader and the store are ports; this module does no I/O of its own and reads no clock.
The sheet is read on every poll. A failed read (unreachable or invalid) keeps the last good copy
in use; with no usable copy the YAML alone is used. After `ops.failures_before_alert` failed reads
in a row, or at once when no copy can be used, one message goes to the ops chat, and one more
when the sheet works again. A mark is set only after the ops chat acknowledged the message, so an
unacknowledged one is retried. Logs carry ids and failure classes, never a key or a cell.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from enji_guard_radar.config import RadarConfig
from enji_guard_radar.messages import escape
from enji_guard_radar.model import Audience, OutboundMessage
from enji_guard_radar.ports import SheetReader, SheetUnavailable
from enji_guard_radar.sheet_settings import SheetData, SheetInvalid, apply_sheet
from enji_guard_radar.texts import DEFAULT_LANGUAGE, Language, counted, text

logger = logging.getLogger(__name__)

FailureClass = Literal["key", "network", "denied", "not_found", "response", "invalid"]
_TIME_FORMAT = "%Y-%m-%d %H:%M"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SheetCopy(_Frozen):
    """The last sheet that was valid, as read: it is merged over the current YAML on every use."""

    fetched_at: AwareDatetime
    data: SheetData


class SheetHealth(_Frozen):
    consecutive_failures: StrictInt = Field(default=0, ge=0)
    first_failure_at: AwareDatetime | None = None
    last_failure_class: FailureClass | None = None
    # Tab, row and column of an invalid sheet; never a cell value.
    last_failure_detail: str | None = None
    outage_reported_at: AwareDatetime | None = None


class SheetRecord(_Frozen):
    schema_version: int = 1
    last_good: SheetCopy | None = None
    health: SheetHealth = Field(default_factory=SheetHealth)


class SheetStore(Protocol):
    def load(self) -> SheetRecord:
        """An unreadable or missing record is an empty one."""
        ...

    def save(self, record: SheetRecord) -> None:
        """Raise OSError when the record cannot be written."""
        ...


class ConfigSource(StrEnum):
    YAML_ONLY = "yaml_only"
    SHEET = "sheet"
    LAST_GOOD_COPY = "last_good_copy"
    YAML_FALLBACK = "yaml_fallback"


@dataclass(frozen=True)
class SheetOutage:
    failure: FailureClass
    detail: str | None
    since: datetime
    failures: int


@dataclass(frozen=True)
class SheetRecovered:
    since: datetime


SheetOpsAction = SheetOutage | SheetRecovered | None


@dataclass(frozen=True)
class SheetResolution:
    config: RadarConfig
    source: ConfigSource
    record: SheetRecord
    action: SheetOpsAction
    failure: FailureClass | None = None
    detail: str | None = None
    copy_fetched_at: datetime | None = None


@dataclass(frozen=True)
class _Failed:
    failure: FailureClass
    detail: str | None


def describe_source(resolution: SheetResolution) -> str:
    """One English line saying where the settings came from."""
    if resolution.source is ConfigSource.YAML_ONLY:
        return "YAML only (sheet disabled)"
    if resolution.source is ConfigSource.SHEET:
        return "live sheet over YAML"
    reason = _failure_text(resolution)
    if resolution.source is ConfigSource.LAST_GOOD_COPY and resolution.copy_fetched_at is not None:
        return f"last good sheet copy from {_utc_text(resolution.copy_fetched_at)} UTC ({reason})"
    return f"YAML only, no usable sheet copy ({reason})"


def _failure_text(resolution: SheetResolution) -> str:
    detail = f": {resolution.detail}" if resolution.detail else ""
    return f"sheet {resolution.failure}{detail}"


@dataclass(frozen=True)
class SheetSettingsSource:
    reader: SheetReader
    store: SheetStore

    def resolve(self, base: RadarConfig, now: datetime) -> SheetResolution:
        record = self.store.load()
        attempt = self._read(base)
        if isinstance(attempt, _Failed):
            return self._fall_back(base, record, attempt, now)
        config, data = attempt
        health, action = _advance(
            record.health, None, now, base.ops.failures_before_alert, has_copy=True
        )
        record = SheetRecord(last_good=SheetCopy(fetched_at=now, data=data), health=health)
        self._persist(record)
        logger.info("sheet fetch ok: settings come from the sheet over the YAML")
        return SheetResolution(config, ConfigSource.SHEET, record, action)

    def acknowledge(self, resolution: SheetResolution, now: datetime) -> None:
        """Call only after the ops chat acknowledged the message of `resolution.action`."""
        health = _mark_delivered(resolution.record.health, resolution.action, now)
        self._persist(resolution.record.model_copy(update={"health": health}))

    def _read(self, base: RadarConfig) -> tuple[RadarConfig, SheetData] | _Failed:
        try:
            data = self.reader.read()
        except SheetUnavailable as error:
            logger.warning("sheet fetch unreachable (%s)", error.cause.value)
            return _Failed(error.cause.value, None)
        try:
            return apply_sheet(base, data), data
        except SheetInvalid as error:
            logger.warning(
                "sheet fetch invalid: %d problem(s), first: %s",
                len(error.problems),
                error.problems[0].text(),
            )
            return _Failed("invalid", str(error))

    def _fall_back(
        self, base: RadarConfig, record: SheetRecord, failed: _Failed, now: datetime
    ) -> SheetResolution:
        usable = self._usable_copy(base, record)
        health, action = _advance(
            record.health,
            failed,
            now,
            base.ops.failures_before_alert,
            has_copy=usable is not None,
        )
        record = record.model_copy(update={"health": health})
        self._persist(record)
        if usable is None:
            logger.error("sheet unusable and no usable last good copy: YAML alone is in use")
            return SheetResolution(
                config=base,
                source=ConfigSource.YAML_FALLBACK,
                record=record,
                action=action,
                failure=failed.failure,
                detail=failed.detail,
            )
        config, fetched_at = usable
        logger.warning("sheet unusable: last good copy from %s is in use", fetched_at.isoformat())
        return SheetResolution(
            config=config,
            source=ConfigSource.LAST_GOOD_COPY,
            record=record,
            action=action,
            failure=failed.failure,
            detail=failed.detail,
            copy_fetched_at=fetched_at,
        )

    def _usable_copy(
        self, base: RadarConfig, record: SheetRecord
    ) -> tuple[RadarConfig, datetime] | None:
        if record.last_good is None:
            return None
        try:
            return apply_sheet(base, record.last_good.data), record.last_good.fetched_at
        except SheetInvalid as error:
            logger.warning("last good sheet copy no longer fits the YAML: %s", error)
            return None

    def _persist(self, record: SheetRecord) -> None:
        try:
            self.store.save(record)
        except OSError as error:
            logger.error("sheet record not saved: %s", type(error).__name__)  # noqa: TRY400


def _advance(
    health: SheetHealth,
    failed: _Failed | None,
    now: datetime,
    failures_before_alert: int,
    *,
    has_copy: bool,
) -> tuple[SheetHealth, SheetOpsAction]:
    if failed is None:
        if health.outage_reported_at is None:
            return SheetHealth(), None
        since = health.first_failure_at if health.first_failure_at is not None else now
        return health.model_copy(update={"consecutive_failures": 0}), SheetRecovered(since)
    failures = health.consecutive_failures + 1
    since = health.first_failure_at if health.first_failure_at is not None else now
    stepped = health.model_copy(
        update={
            "consecutive_failures": failures,
            "first_failure_at": since,
            "last_failure_class": failed.failure,
            "last_failure_detail": failed.detail,
        }
    )
    threshold = failures_before_alert if has_copy else 1
    if failures >= threshold and health.outage_reported_at is None:
        return stepped, SheetOutage(failed.failure, failed.detail, since, failures)
    return stepped, None


def _mark_delivered(health: SheetHealth, action: SheetOpsAction, now: datetime) -> SheetHealth:
    if isinstance(action, SheetOutage):
        return health.model_copy(update={"outage_reported_at": now})
    if isinstance(action, SheetRecovered):
        return SheetHealth()
    return health


def sheet_ops_message(
    action: SheetOutage | SheetRecovered,
    *,
    language: Language = DEFAULT_LANGUAGE,
    copy_fetched_at: datetime | None = None,
) -> OutboundMessage:
    if isinstance(action, SheetRecovered):
        body = text(language, "ops.sheet.recovered", since=_utc_text(action.since))
        return OutboundMessage(text=escape(body), audience=Audience.OPS)
    reason_key = f"ops.sheet.reason.{action.failure}"
    reason = (
        text(language, reason_key, detail=action.detail or "")
        if action.failure == "invalid"
        else text(language, reason_key)
    )
    fallback = (
        text(language, "ops.sheet.fallback_copy", fetched=_utc_text(copy_fetched_at))
        if copy_fetched_at is not None
        else text(language, "ops.sheet.fallback_yaml")
    )
    body = text(
        language,
        "ops.sheet.outage",
        reason=reason,
        since=_utc_text(action.since),
        failures=counted(language, "noun.failed_read", action.failures),
        fallback=fallback,
    )
    return OutboundMessage(text=escape(body), audience=Audience.OPS)


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TIME_FORMAT)
