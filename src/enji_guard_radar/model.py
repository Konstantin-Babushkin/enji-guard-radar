"""Shared domain types: alert identity, alert, outbound message."""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


# Worst first; an unrecognised level is never sorted below a known one.
SEVERITY_ORDER = (Severity.HIGH, Severity.UNKNOWN, Severity.MEDIUM, Severity.LOW)


class AlertKind(StrEnum):
    STALE = "stale"
    NEVER_RAN = "never_ran"


class Audience(StrEnum):
    GROUP = "group"
    OPS = "ops"


class AlertKey(BaseModel):
    """Identity only (STATE-01): never ages, counts or names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str = Field(min_length=1)
    repo_id: str = "*"
    action_key: str = "*"
    level: str = Field(min_length=1)

    def as_text(self) -> str:
        """For log lines and sorting only."""
        return "|".join((self.project_id, self.repo_id, self.action_key, self.level))


class Alert(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: AlertKey
    kind: AlertKind
    project_name: str
    severity: Severity
    age_days: int | None
    last_run_at: AwareDatetime | None
    repo_count: int
    # Last observed count of a count-based condition; None for stale and never ran.
    count: int | None = Field(default=None, ge=0)


class OutboundMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    keys: tuple[AlertKey, ...] = ()
    audience: Audience = Audience.GROUP
