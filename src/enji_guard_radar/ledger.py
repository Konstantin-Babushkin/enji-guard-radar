"""The radar's memory: what was seen, alerted, baselined or resolved. Pure, no I/O.

State holds ids, levels, flags and timestamps only (STATE-01): never names, report text
or evidence. Whether to send is decided on the key, the flags and, for a counted condition,
a rise of its count over the previous evaluation (STATE-07, D-06); never on age.
"""

import json
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

from enji_guard_radar.model import Alert, AlertKey

SCHEMA_VERSION = 2
READABLE_SCHEMA_VERSIONS = (1, 2)

# (project_id, repo_id, action_key): one audit type of one repo.
SeriesKey = tuple[str, str, str]
FailureCause = Literal["auth", "unavailable", "data"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConditionRecord(_Frozen):
    key: AlertKey
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    baseline: bool
    alerted_at: AwareDatetime | None = None
    # Last observed count; None for stale and never-ran. A rise above it re-arms an alerted or
    # baselined condition (STATE-07, D-06).
    count: StrictInt | None = Field(default=None, ge=0)


class ProjectRecord(_Frozen):
    project_id: str
    first_seen_at: AwareDatetime
    last_evaluated_at: AwareDatetime


class RunPoint(_Frozen):
    """One completed Guard run of a series: its task id, when it finished, its High count."""

    task_id: str = Field(min_length=1)
    completed_at: AwareDatetime
    high: StrictInt = Field(ge=0)


class SeriesHistory(_Frozen):
    """High history of one (project, repo, audit type), counted by Guard task_id (D-01)."""

    project_id: str = Field(min_length=1)
    repo_id: str = Field(min_length=1)
    action_key: str = Field(min_length=1)
    points: tuple[RunPoint, ...] = ()
    open: bool = False
    opened_at: AwareDatetime | None = None
    clean_runs: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _consistent_flags(self) -> "SeriesHistory":
        if self.open and self.opened_at is None:
            message = "an open series needs opened_at"
            raise ValueError(message)
        if not self.open and self.opened_at is not None:
            message = "a closed series has no opened_at"
            raise ValueError(message)
        if not self.open and self.clean_runs > 0:
            message = "a closed series has no clean runs"
            raise ValueError(message)
        return self

    def key(self) -> SeriesKey:
        return (self.project_id, self.repo_id, self.action_key)


class PollHealth(_Frozen):
    """Poll failure bookkeeping. Data only here; plan 02-07 fills it."""

    consecutive_failures: StrictInt = Field(default=0, ge=0)
    first_failure_at: AwareDatetime | None = None
    last_failure_cause: FailureCause | None = None
    last_success_at: AwareDatetime | None = None
    outage_reported_at: AwareDatetime | None = None


class State(_Frozen):
    schema_version: int = SCHEMA_VERSION
    projects: tuple[ProjectRecord, ...] = ()
    conditions: tuple[ConditionRecord, ...] = ()
    histories: tuple[SeriesHistory, ...] = ()
    health: PollHealth = Field(default_factory=PollHealth)

    @classmethod
    def empty(cls) -> "State":
        return cls()


class EvaluationOutcome(StrEnum):
    evaluated = "evaluated"
    unknown = "unknown"
    skipped = "skipped"


class ProjectEvaluation(_Frozen):
    project_id: str
    outcome: EvaluationOutcome
    alerts: tuple[Alert, ...] = ()


class Reconciliation(_Frozen):
    state: State
    to_send: tuple[Alert, ...] = ()
    baselined: tuple[Alert, ...] = ()
    resolved: tuple[AlertKey, ...] = ()
    retried: tuple[AlertKey, ...] = ()
    rearmed: tuple[AlertKey, ...] = ()
    first_seen_projects: tuple[str, ...] = ()


class StateFormatError(Exception):
    """The reason is built from error locations and types only, never from file content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CorruptStateError(Exception):
    def __init__(self, reason: str, backup_valid: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.backup_valid = backup_valid


@dataclass(frozen=True)
class Loaded:
    state: State


@dataclass(frozen=True)
class Missing:
    pass


@dataclass(frozen=True)
class Corrupt:
    reason: str
    backup_valid: bool


StateLoad = Loaded | Missing | Corrupt


class StateStore(Protocol):
    def load(self) -> StateLoad: ...

    def save(self, state: State) -> None: ...

    def lock(self) -> AbstractContextManager[bool]: ...


def _key_order(key: AlertKey) -> tuple[str, str, str, str]:
    return (key.project_id, key.repo_id, key.action_key, key.level)


def _normalized(
    projects: Iterable[ProjectRecord],
    conditions: Iterable[ConditionRecord],
    histories: Iterable[SeriesHistory],
    health: PollHealth,
) -> State:
    return State(
        projects=tuple(sorted(projects, key=lambda p: p.project_id)),
        conditions=tuple(sorted(conditions, key=lambda c: _key_order(c.key))),
        histories=tuple(sorted(histories, key=lambda h: h.key())),
        health=health,
    )


def reconcile(
    state: State, evaluations: Sequence[ProjectEvaluation], now: datetime
) -> Reconciliation:
    """Decide what to send. Only evaluated projects change; unknown and skipped are frozen."""
    if now.utcoffset() is None:
        message = "now must be timezone-aware"
        raise ValueError(message)

    projects = {p.project_id: p for p in state.projects}
    conditions = {c.key: c for c in state.conditions}
    to_send: list[Alert] = []
    baselined: list[Alert] = []
    resolved: list[AlertKey] = []
    retried: list[AlertKey] = []
    rearmed: list[AlertKey] = []
    first_seen: list[str] = []

    for evaluation in evaluations:
        if evaluation.outcome is not EvaluationOutcome.evaluated:
            continue
        pid = evaluation.project_id
        known = pid in projects
        if known:
            projects[pid] = projects[pid].model_copy(update={"last_evaluated_at": now})
        else:
            projects[pid] = ProjectRecord(project_id=pid, first_seen_at=now, last_evaluated_at=now)
            first_seen.append(pid)

        current = {alert.key: alert for alert in evaluation.alerts}
        for key, alert in current.items():
            record = conditions.get(key)
            if not known:
                conditions[key] = ConditionRecord(
                    key=key, first_seen_at=now, last_seen_at=now, baseline=True, count=alert.count
                )
                baselined.append(alert)
            elif record is None:
                conditions[key] = ConditionRecord(
                    key=key, first_seen_at=now, last_seen_at=now, baseline=False, count=alert.count
                )
                to_send.append(alert)
            elif (
                alert.count is not None
                and record.count is not None
                and alert.count > record.count
                and (record.alerted_at is not None or record.baseline)
            ):
                # Per-run rise (D-06): plan it again; apply_delivery sets alerted_at on success.
                conditions[key] = record.model_copy(
                    update={
                        "last_seen_at": now,
                        "count": alert.count,
                        "alerted_at": None,
                        "baseline": False,
                    }
                )
                to_send.append(alert)
                rearmed.append(key)
            else:
                conditions[key] = record.model_copy(
                    update={"last_seen_at": now, "count": alert.count}
                )
                if record.alerted_at is None and not record.baseline:
                    to_send.append(alert)
                    retried.append(key)
        for key in [k for k in conditions if k.project_id == pid and k not in current]:
            del conditions[key]
            resolved.append(key)

    return Reconciliation(
        state=_normalized(projects.values(), conditions.values(), state.histories, state.health),
        to_send=tuple(sorted(to_send, key=lambda a: _key_order(a.key))),
        baselined=tuple(sorted(baselined, key=lambda a: _key_order(a.key))),
        resolved=tuple(sorted(resolved, key=_key_order)),
        retried=tuple(sorted(retried, key=_key_order)),
        rearmed=tuple(sorted(rearmed, key=_key_order)),
        first_seen_projects=tuple(sorted(first_seen)),
    )


def apply_delivery(state: State, keys: Iterable[AlertKey], now: datetime) -> State:
    """Mark the keys of an acknowledged message as alerted. The only place this is set."""
    acknowledged = set(keys)
    return _normalized(
        state.projects,
        (
            c.model_copy(update={"alerted_at": now}) if c.key in acknowledged else c
            for c in state.conditions
        ),
        state.histories,
        state.health,
    )


def serialize_state(state: State) -> bytes:
    normalized = _normalized(
        state.projects, state.conditions, state.histories, state.health
    ).model_copy(update={"schema_version": state.schema_version})
    return (normalized.model_dump_json(indent=2) + "\n").encode("utf-8")


def migrate_state_v1(raw: dict[str, object]) -> dict[str, object]:
    """Pure 1 -> 2 upgrade: records are copied as they are, histories and health start empty."""
    return {**raw, "schema_version": 2, "histories": [], "health": {}}


def deserialize_state(data: bytes) -> State:
    try:
        raw = json.loads(data)
    except json.JSONDecodeError as exc:
        reason = f"not valid JSON (line {exc.lineno}, column {exc.colno})"
        raise StateFormatError(reason) from None
    except ValueError:
        reason = "not valid JSON"
        raise StateFormatError(reason) from None
    if not isinstance(raw, dict):
        reason = "top level is not an object"
        raise StateFormatError(reason)
    if "schema_version" in raw:
        version = raw["schema_version"]
        if type(version) is not int or version not in READABLE_SCHEMA_VERSIONS:
            shown = version if type(version) is int else type(version).__name__
            reason = f"unsupported schema_version {shown} (this radar reads 1 and 2)"
            raise StateFormatError(reason)
        if version == 1:
            raw = migrate_state_v1(raw)
    try:
        return State.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = first["loc"][:-1] if first["type"] == "extra_forbidden" else first["loc"]
        # An unexpected field's name comes from the file, so it is left out of the reason.
        where = ".".join(str(part) for part in loc) or "<root>"
        reason = (
            f"does not match the state schema: {exc.error_count()} error(s), "
            f"first at {where} ({first['type']})"
        )
        raise StateFormatError(reason) from None
