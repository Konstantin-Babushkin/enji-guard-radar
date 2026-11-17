"""Radar configuration: thresholds with a default and per-project overrides.

Pure: the shell reads the file and passes the text in (AGENTS.md).
"""

import logging
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Names the source and the failing location, never the offending value."""


class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_after_days: int = Field(default=14, ge=1)


ProjectMode = Literal["active", "paused", "excluded"]


class ProjectSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stale_after_days: int | None = Field(default=None, ge=1)
    mode: ProjectMode = "active"


class RadarConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    defaults: Thresholds = Field(default_factory=Thresholds)
    projects: dict[str, ProjectSettings] = Field(default_factory=dict)

    def mode_for(self, project_id: str) -> ProjectMode:
        """SRC-05: paused and excluded projects are never read from Guard."""
        entry = self.projects.get(project_id)
        return "active" if entry is None else entry.mode

    def thresholds_for(self, project_id: str) -> Thresholds:
        """The project override merged over the defaults."""
        override = self.projects.get(project_id)
        if override is None or override.stale_after_days is None:
            return self.defaults
        return self.defaults.model_copy(update={"stale_after_days": override.stale_after_days})


def parse_config(text: str, *, source: str) -> RadarConfig:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        logger.error("config %s is not valid YAML", source)  # noqa: TRY400
        message = f"{source}: not valid YAML"
        raise ConfigError(message) from None
    if data is None:
        return RadarConfig()
    try:
        return RadarConfig.model_validate(data)
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "(top level)"
        logger.error("config %s is invalid at %s", source, where)  # noqa: TRY400
        message = f"{source}: invalid at {where}: {first['msg']}"
        raise ConfigError(message) from None
