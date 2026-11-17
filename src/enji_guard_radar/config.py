"""Radar configuration: thresholds with a default and per-project overrides.

Pure: the shell reads the file and passes the text in (AGENTS.md).
"""

import logging
import re
from collections.abc import Hashable
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

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
AUDIT_TYPE_PATTERN = re.compile(r"audit\.[a-z0-9-]+")


class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_after_days: StrictInt = Field(default=14, ge=STALE_DAYS_MIN, le=STALE_DAYS_MAX)
    # A High series open for longer than this many days is flagged long-standing (RULE-02).
    high_aged_after_days: StrictInt = Field(
        default=14, ge=HIGH_AGED_DAYS_MIN, le=HIGH_AGED_DAYS_MAX
    )


ProjectMode = Literal["active", "paused", "excluded"]


class ProjectSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_after_days: StrictInt | None = Field(default=None, ge=STALE_DAYS_MIN, le=STALE_DAYS_MAX)
    high_aged_after_days: StrictInt | None = Field(
        default=None, ge=HIGH_AGED_DAYS_MIN, le=HIGH_AGED_DAYS_MAX
    )
    mode: ProjectMode = "active"


class HighSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # A High series clears only after this many consecutive completed runs with zero High.
    clear_after_clean_runs: StrictInt = Field(default=2, ge=CLEAR_AFTER_MIN, le=CLEAR_AFTER_MAX)
    # None tracks every audit type with findings; a list narrows to exactly those types.
    audit_types: tuple[str, ...] | None = None

    @field_validator("audit_types")
    @classmethod
    def _audit_types_well_formed(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
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


class AlertSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    collapse_above: StrictInt = Field(default=3, ge=COLLAPSE_MIN, le=COLLAPSE_MAX)


class RadarConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    defaults: Thresholds = Field(default_factory=Thresholds)
    alerts: AlertSettings = Field(default_factory=AlertSettings)
    high: HighSettings = Field(default_factory=HighSettings)
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
