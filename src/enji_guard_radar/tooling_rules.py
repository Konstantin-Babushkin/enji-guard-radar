"""RULE-07 / D-10: a (repo, tooling audit type) with no recent completed run is a tooling gap.

Pure: the project's runs, the stored records and the clock come in as arguments (AGENTS.md).
Guard's run list is truncated, so a missing run is only a gap when this poll read the repository's
whole run list or the radar has watched the pair for a full window (ASSUMPTION, D-10 leaves the
bridging rule to Claude's Discretion); otherwise the pair is undecided and flags nothing.
"""

import logging
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from enji_guard_radar.guard import ProjectOk, newest_counting_runs_by_action_key
from enji_guard_radar.ledger import SeriesKey, ToolingRecord

logger = logging.getLogger(__name__)


class ToolingGap(BaseModel):
    """One (repo, audit type) with no completed run in the window; ids and dates only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    project_name: str
    repo_id: str
    repo_name: str
    action_key: str
    # None when the radar never saw a completed run of this type.
    last_completed_at: AwareDatetime | None
    age_days: StrictInt | None = Field(ge=0)
    window_days: StrictInt = Field(ge=1)


class ToolingEvaluation(BaseModel):
    """One project's tooling records after this poll, its gaps and the pairs left undecided."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[ToolingRecord, ...]
    gaps: tuple[ToolingGap, ...]
    undecided: StrictInt = Field(ge=0)


def _gap_order(gap: ToolingGap) -> tuple[bool, float, str, str, str]:
    """Never seen first, then the oldest last run, then names."""
    last = gap.last_completed_at
    return (
        last is not None,
        0.0 if last is None else last.timestamp(),
        gap.project_name.casefold(),
        gap.repo_name,
        gap.action_key,
    )


def evaluate_tooling(
    project: ProjectOk,
    stored: Mapping[SeriesKey, ToolingRecord],
    *,
    audit_types: Collection[str],
    window_days: int,
    now: datetime,
) -> ToolingEvaluation:
    """Fold this poll's runs into the stored records and name the gaps the evidence supports.

    `last` is the later of the stored last_completed_at and this poll's newest counting run. It is
    a gap when it is missing or at least window_days old, and either the run list was read to its
    end or the pair has been watched for window_days. A run completed after `now` is recent.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        message = "now must be timezone-aware"
        raise ValueError(message)
    if window_days < 1:
        message = "window_days must be at least 1"
        raise ValueError(message)
    window = timedelta(days=window_days)
    pid = project.project.id
    records: list[ToolingRecord] = []
    gaps: list[ToolingGap] = []
    undecided = 0
    for repo in project.repositories:
        rid = repo.repository.id
        newest = {run.action_key: run for run in newest_counting_runs_by_action_key(repo.runs)}
        for action_key in sorted(audit_types):
            previous = stored.get((pid, rid, action_key))
            observed_since = previous.observed_since if previous is not None else now
            seen = newest[action_key].completed_at if action_key in newest else None
            known = [t for t in (previous.last_completed_at if previous else None, seen) if t]
            last = max(known) if known else None
            records.append(
                ToolingRecord(
                    project_id=pid,
                    repo_id=rid,
                    action_key=action_key,
                    observed_since=observed_since,
                    last_completed_at=last,
                )
            )
            if last is not None and now - last < window:
                continue
            if not (repo.runs_complete or now - observed_since >= window):
                undecided += 1
                continue
            age_days = None if last is None else max((now - last).days, 0)
            gaps.append(
                ToolingGap(
                    project_id=pid,
                    project_name=project.project.name,
                    repo_id=rid,
                    repo_name=repo.repository.name,
                    action_key=action_key,
                    last_completed_at=last,
                    age_days=age_days,
                    window_days=window_days,
                )
            )
            logger.info(
                "tooling gap: %s/%s/%s last=%s",
                pid,
                rid,
                action_key,
                "none" if last is None else last.date().isoformat(),
            )
    if undecided:
        logger.info(
            "tooling: %d pair(s) undecided: run history not complete and watched less than %d days",
            undecided,
            window_days,
        )
    return ToolingEvaluation(
        records=tuple(sorted(records, key=lambda record: record.key())),
        gaps=tuple(sorted(gaps, key=_gap_order)),
        undecided=undecided,
    )
