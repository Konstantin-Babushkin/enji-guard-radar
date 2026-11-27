"""Radar configuration: thresholds with a default and per-project overrides.

Pure: the shell reads the file and passes the text in (AGENTS.md).
"""

import logging
import re
from collections.abc import Hashable
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from enji_guard_radar.schedule import MINUTES_PER_DAY
from enji_guard_radar.texts import DEFAULT_LANGUAGE, Language

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Names the source and the failing location, never the offending value."""


STALE_DAYS_MIN = 1
STALE_DAYS_MAX = 365
HIGH_AGED_DAYS_MIN = 1
HIGH_AGED_DAYS_MAX = 365
CLEAR_AFTER_MIN = 1
CLEAR_AFTER_MAX = 10
COLLAPSE_MIN = 1
COLLAPSE_MAX = 50
FAILURES_MIN = 1
FAILURES_MAX = 20
DIGEST_HOUR_MIN = 0
DIGEST_HOUR_MAX = 23
GRACE_HOURS_MIN = 1
GRACE_HOURS_MAX = 168
MAX_ITEMS_MIN = 3
MAX_ITEMS_MAX = 50
REMINDER_DAYS_MIN = 1
REMINDER_DAYS_MAX = 90
SCORE_MIN = 0
SCORE_MAX = 100
RECURRING_AFTER_MIN = 2
RECURRING_AFTER_MAX = 20
TOOLING_WINDOW_MIN = 7
TOOLING_WINDOW_MAX = 365
SCHEDULE_MINUTES_MIN = 1
TRIAGE_ITEMS_MIN = 1
TRIAGE_ITEMS_MAX = 100
TRIAGE_TIMEOUT_MIN = 5
TRIAGE_TIMEOUT_MAX = 300
TRIAGE_COST_MIN = 0.01
TRIAGE_COST_MAX = 5.0
# A short token: it later becomes a CLI argument, so no spaces or shell characters (T-04-10).
# The id in the sheet's address: letters, digits, dash, underscore.
SPREADSHEET_ID_PATTERN = r"^[A-Za-z0-9_-]{10,128}$"
TRIAGE_MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
AI_READINESS_ACTION_KEY = "audit.ai-readiness"
TESTS_ACTION_KEY = "audit.tests"
DEFAULT_TOOLING_AUDIT_TYPES = (
    "audit.dead-code",
    "audit.implementation-unification",
    "audit.security",
    "audit.dependency-hygiene",
)
AUDIT_TYPE_PATTERN = re.compile(r"audit\.[a-z0-9-]+")
LEGACY_AUDIT_TYPES_KEY = "audit_types"
NEW_AUDIT_TYPES_KEY = "high_priority_audit_types"


class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_after_days: StrictInt = Field(default=14, ge=STALE_DAYS_MIN, le=STALE_DAYS_MAX)
    # A High series open for longer than this many days is flagged long-standing (RULE-02).
    high_aged_after_days: StrictInt = Field(
        default=14, ge=HIGH_AGED_DAYS_MIN, le=HIGH_AGED_DAYS_MAX
    )


def _check_audit_types(value: tuple[str, ...]) -> tuple[str, ...]:
    """Shared shape rule for every configured audit-type list (RULE-10)."""
    if not value:
        message = "must be null or a non-empty list"
        raise ValueError(message)
    if any(AUDIT_TYPE_PATTERN.fullmatch(item) is None for item in value):
        message = "every audit type must look like audit.<name> (lowercase, digits, dashes)"
        raise ValueError(message)
    if len(set(value)) != len(value):
        message = "audit types must not repeat"
        raise ValueError(message)
    return value


ProjectMode = Literal["active", "paused", "excluded"]
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class ProjectSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_after_days: StrictInt | None = Field(default=None, ge=STALE_DAYS_MIN, le=STALE_DAYS_MAX)
    high_aged_after_days: StrictInt | None = Field(
        default=None, ge=HIGH_AGED_DAYS_MIN, le=HIGH_AGED_DAYS_MAX
    )
    mode: ProjectMode = "active"
    # Score rules (RULE-05/06): a score strictly below this flags; 0 disables the rule here.
    ai_readiness_below: StrictInt | None = Field(default=None, ge=SCORE_MIN, le=SCORE_MAX)
    tests_below: StrictInt | None = Field(default=None, ge=SCORE_MIN, le=SCORE_MAX)
    # Tooling-gap window in days for this project (RULE-08/D-10).
    tooling_window_days: StrictInt | None = Field(
        default=None, ge=TOOLING_WINDOW_MIN, le=TOOLING_WINDOW_MAX
    )
    # None = the global `high.high_priority_audit_types`; a list = exactly those alert here.
    high_priority_audit_types: tuple[str, ...] | None = None
    # None = the global `reminders.every_days`.
    reminder_every_days: StrictInt | None = Field(
        default=None, ge=REMINDER_DAYS_MIN, le=REMINDER_DAYS_MAX
    )

    @field_validator("high_priority_audit_types")
    @classmethod
    def _audit_types_well_formed(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if value is None else _check_audit_types(value)


class HighSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # A High series clears only after this many consecutive completed runs with zero High.
    clear_after_clean_runs: StrictInt = Field(default=2, ge=CLEAR_AFTER_MIN, le=CLEAR_AFTER_MAX)
    # None = every audit type is high priority (alerts, reminders, triage). A list = exactly
    # those alert; every other type is tracked and shown only in the weekly digest.
    high_priority_audit_types: tuple[str, ...] | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_audit_types(cls, data: object) -> object:
        """`audit_types` is the deprecated name of `high_priority_audit_types`."""
        if not isinstance(data, dict) or LEGACY_AUDIT_TYPES_KEY not in data:
            return data
        if NEW_AUDIT_TYPES_KEY in data:
            message = (
                f"high.{LEGACY_AUDIT_TYPES_KEY} and high.{NEW_AUDIT_TYPES_KEY} are both set; "
                f"keep only high.{NEW_AUDIT_TYPES_KEY}"
            )
            raise ValueError(message)
        logger.warning(
            "config: high.%s is deprecated; use high.%s",
            LEGACY_AUDIT_TYPES_KEY,
            NEW_AUDIT_TYPES_KEY,
        )
        moved = {k: v for k, v in data.items() if k != LEGACY_AUDIT_TYPES_KEY}
        moved[NEW_AUDIT_TYPES_KEY] = data[LEGACY_AUDIT_TYPES_KEY]
        return moved

    @field_validator("high_priority_audit_types")
    @classmethod
    def _audit_types_well_formed(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if value is None else _check_audit_types(value)


class AlertSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    collapse_above: StrictInt = Field(default=3, ge=COLLAPSE_MIN, le=COLLAPSE_MAX)


class OpsSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # After this many failed Guard polls in a row one message goes to the ops chat (OPS-02).
    failures_before_alert: StrictInt = Field(default=3, ge=FAILURES_MIN, le=FAILURES_MAX)


class MessageSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Language of every Telegram message, group and ops chat.
    # Logs, doctor and CLI errors stay English.
    language: Language = DEFAULT_LANGUAGE


class DigestSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # The weekly digest is planned for this weekday and hour (UTC) (D-01).
    weekday: Weekday = "wednesday"
    hour_utc: StrictInt = Field(default=5, ge=DIGEST_HOUR_MIN, le=DIGEST_HOUR_MAX)
    # A digest not acknowledged within this many hours of its planned moment is skipped (D-02).
    grace_hours: StrictInt = Field(default=24, ge=GRACE_HOURS_MIN, le=GRACE_HOURS_MAX)
    # Items shown in one digest before the rest is summarised (D-06).
    max_items: StrictInt = Field(default=10, ge=MAX_ITEMS_MIN, le=MAX_ITEMS_MAX)

    @property
    def weekday_number(self) -> int:
        """Monday is 0, like datetime.weekday()."""
        return WEEKDAY_NAMES.index(self.weekday)


class ScheduleSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # `serve` polls at multiples of this many minutes from 00:00 UTC; it must divide a day.
    every_minutes: StrictInt = Field(default=180, ge=SCHEDULE_MINUTES_MIN, le=MINUTES_PER_DAY)

    @field_validator("every_minutes")
    @classmethod
    def _divides_a_day(cls, value: int) -> int:
        if MINUTES_PER_DAY % value:
            message = f"must divide {MINUTES_PER_DAY} (a whole number of slots per day)"
            raise ValueError(message)
        return value


class ReminderSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # An unresolved finding is reminded about every this many days (D-11).
    every_days: StrictInt = Field(default=7, ge=REMINDER_DAYS_MIN, le=REMINDER_DAYS_MAX)


class ScoreSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # None = rule off (D-08); a number flags scores strictly below it.
    ai_readiness_below: StrictInt | None = Field(default=None, ge=SCORE_MIN, le=SCORE_MAX)
    tests_below: StrictInt | None = Field(default=None, ge=SCORE_MIN, le=SCORE_MAX)


class RecurringSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # A finding present in this many consecutive completed runs is recurring (D-09).
    after_runs: StrictInt = Field(default=3, ge=RECURRING_AFTER_MIN, le=RECURRING_AFTER_MAX)


class ToolingGapSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Days without a completed run of a configured audit type before it is a gap (D-10).
    window_days: StrictInt = Field(default=60, ge=TOOLING_WINDOW_MIN, le=TOOLING_WINDOW_MAX)
    audit_types: tuple[str, ...] = DEFAULT_TOOLING_AUDIT_TYPES

    @field_validator("audit_types")
    @classmethod
    def _audit_types_well_formed(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _check_audit_types(value)


class SheetSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Settings from a Google Sheet override this file; ships off. The key is a secret in the env.
    enabled: StrictBool = False
    spreadsheet_id: str | None = Field(default=None, pattern=SPREADSHEET_ID_PATTERN)

    @model_validator(mode="after")
    def _enabled_needs_an_id(self) -> "SheetSettings":
        if self.enabled and self.spreadsheet_id is None:
            message = "spreadsheet_id is required when enabled is true"
            raise ValueError(message)
        return self


# Only the local Claude CLI; no T3 or API value until a route is verified (D-01).
TriageBackend = Literal["claude_cli"]


class TriageSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Triage ships off (D-03): the operator turns it on by config only.
    enabled: StrictBool = False
    backend: TriageBackend = "claude_cli"
    # ASSUMPTION: haiku is the default model until plan 04-01 confirms or changes it.
    model: str = Field(default="haiku", pattern=TRIAGE_MODEL_PATTERN)
    # One call per run: at most this many items, this long, this expensive (D-13).
    max_items_per_call: StrictInt = Field(default=30, ge=TRIAGE_ITEMS_MIN, le=TRIAGE_ITEMS_MAX)
    timeout_seconds: StrictInt = Field(default=60, ge=TRIAGE_TIMEOUT_MIN, le=TRIAGE_TIMEOUT_MAX)
    max_cost_usd_per_call: StrictFloat = Field(default=0.50, ge=TRIAGE_COST_MIN, le=TRIAGE_COST_MAX)


class RadarConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    defaults: Thresholds = Field(default_factory=Thresholds)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    high: HighSettings = Field(default_factory=HighSettings)
    ops: OpsSettings = Field(default_factory=OpsSettings)
    messages: MessageSettings = Field(default_factory=MessageSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    digest: DigestSettings = Field(default_factory=DigestSettings)
    reminders: ReminderSettings = Field(default_factory=ReminderSettings)
    scores: ScoreSettings = Field(default_factory=ScoreSettings)
    recurring: RecurringSettings = Field(default_factory=RecurringSettings)
    tooling_gaps: ToolingGapSettings = Field(default_factory=ToolingGapSettings)
    triage: TriageSettings = Field(default_factory=TriageSettings)
    sheet: SheetSettings = Field(default_factory=SheetSettings)
    projects: dict[str, ProjectSettings] = Field(default_factory=dict)

    def mode_for(self, project_id: str) -> ProjectMode:
        """SRC-05: paused and excluded projects are never read from Guard."""
        entry = self.projects.get(project_id)
        return "active" if entry is None else entry.mode

    def thresholds_for(self, project_id: str) -> Thresholds:
        """The project override merged over the defaults."""
        override = self.projects.get(project_id)
        if override is None:
            return self.defaults
        update = {
            name: value
            for name, value in (
                ("stale_after_days", override.stale_after_days),
                ("high_aged_after_days", override.high_aged_after_days),
            )
            if value is not None
        }
        return self.defaults.model_copy(update=update) if update else self.defaults

    def high_types_for(self, project_id: str) -> tuple[str, ...] | None:
        """The project's high-priority audit types over the global list; None = every type."""
        override = self.projects.get(project_id)
        if override is not None and override.high_priority_audit_types is not None:
            return override.high_priority_audit_types
        return self.high.high_priority_audit_types

    def reminder_days_for(self, project_id: str) -> int:
        """The project's reminder interval over the global one."""
        override = self.projects.get(project_id)
        if override is not None and override.reminder_every_days is not None:
            return override.reminder_every_days
        return self.reminders.every_days

    def score_threshold_for(self, project_id: str, action_key: str) -> int | None:
        """The score below which the rule flags; None when off or not a score audit (D-08)."""
        override = self.projects.get(project_id)
        if action_key == AI_READINESS_ACTION_KEY:
            project_value = None if override is None else override.ai_readiness_below
            return project_value if project_value is not None else self.scores.ai_readiness_below
        if action_key == TESTS_ACTION_KEY:
            project_value = None if override is None else override.tests_below
            return project_value if project_value is not None else self.scores.tests_below
        return None

    def tooling_window_for(self, project_id: str) -> int:
        """The project's tooling-gap window over the section value (D-10)."""
        override = self.projects.get(project_id)
        if override is not None and override.tooling_window_days is not None:
            return override.tooling_window_days
        return self.tooling_gaps.window_days


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping key written twice instead of keeping the last one."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        self.flatten_mapping(node)
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if isinstance(key, Hashable) and key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicated key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _yaml_error(source: str, error: yaml.YAMLError) -> str:
    mark = getattr(error, "problem_mark", None)
    problem = getattr(error, "problem", None) or "not valid YAML"
    where = f" line {mark.line + 1}:" if mark is not None else ""
    return f"{source}:{where} {problem}"


def parse_config(text: str, *, source: str) -> RadarConfig:
    """Strict parse: unknown or duplicated keys and out-of-range values are errors (RULE-10)."""
    try:
        data = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.YAMLError as error:
        message = _yaml_error(source, error)
        logger.error("config rejected: %s", message)  # noqa: TRY400
        raise ConfigError(message) from None
    if data is None:
        logger.info("config %s is empty; using defaults", source)
        return RadarConfig()
    if not isinstance(data, dict):
        message = f"{source}: the top level must be a mapping"
        logger.error("config rejected: %s", message)
        raise ConfigError(message)
    try:
        return RadarConfig.model_validate(data)
    except ValidationError as error:
        problems = [
            f"{source}: {'.'.join(str(part) for part in e['loc']) or '(top level)'}: {e['msg']}"
            for e in error.errors()
        ]
        message = "\n".join(problems)
        logger.error("config rejected: %s", message)  # noqa: TRY400
        raise ConfigError(message) from None
