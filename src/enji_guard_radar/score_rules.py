"""RULE-05 / RULE-06: the newest ai-readiness and tests score per repository against a threshold.

Pure: stored records and thresholds come in as arguments. A score the reader could not read, or
did not read this poll, never flags and never clears (D-08); this module never makes an alert.
"""

import logging
from collections.abc import Mapping

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt

from enji_guard_radar.config import AI_READINESS_ACTION_KEY, TESTS_ACTION_KEY
from enji_guard_radar.guard import ProjectOk, newest_counting_runs_by_action_key
from enji_guard_radar.guard_report import AuditReport
from enji_guard_radar.ledger import ScoreRecord, SeriesKey

logger = logging.getLogger(__name__)

SCORE_ACTION_KEYS: tuple[str, ...] = (AI_READINESS_ACTION_KEY, TESTS_ACTION_KEY)


class LowScore(BaseModel):
    """A score strictly below its threshold: the score, the threshold and the run date only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    project_name: str
    repo_id: str
    repo_name: str
    action_key: str
    score: StrictInt = Field(ge=0, le=100)
    threshold: StrictInt
    completed_at: AwareDatetime


class ScoreEvaluation(BaseModel):
    """One project's score records after this poll, and the low ones among them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: tuple[ScoreRecord, ...]
    low: tuple[LowScore, ...]
    observed: StrictInt = Field(ge=0)
    unknown_reports: StrictInt = Field(ge=0)


def score_known(record: ScoreRecord | None, task_id: str) -> bool:
    """True when the stored record is for exactly that run (its report need not be read again)."""
    return record is not None and record.task_id == task_id


def evaluate_scores(
    project: ProjectOk,
    stored: Mapping[SeriesKey, ScoreRecord],
    *,
    thresholds: Mapping[str, int],
) -> ScoreEvaluation:
    """Fold the newest completed run of each score audit type into the stored records.

    `thresholds` maps an audit type to its strict upper bound; a type without one is off (its
    record is still kept). Unknown reports and runs whose report was not read leave the stored
    record exactly as it was.
    """
    unexpected = sorted(set(thresholds) - set(SCORE_ACTION_KEYS))
    if unexpected:
        message = f"no score rule for audit type: {', '.join(unexpected)}"
        raise ValueError(message)

    pid = project.project.id
    repo_names = {repo.repository.id: repo.repository.name for repo in project.repositories}
    mine = {key: record for key, record in stored.items() if key[0] == pid}
    for key in sorted(mine):
        if key[1] not in repo_names or key[2] not in SCORE_ACTION_KEYS:
            logger.info(
                "score record dropped (repository removed or out of scope): %s", "|".join(key)
            )
            del mine[key]

    observed = unknown_reports = 0
    for repo in project.repositories:
        reports = {report.task_id: report for report in repo.reports}
        for run in newest_counting_runs_by_action_key(repo.runs):
            if run.action_key not in SCORE_ACTION_KEYS or run.completed_at is None:
                continue
            key = (pid, repo.repository.id, run.action_key)
            report = reports.get(run.task_id)
            if report is None:
                logger.debug(
                    "report of run %s not read; score record %s unchanged", run.task_id, key
                )
                continue
            if not (
                isinstance(report, AuditReport)
                and report.layout == "scored"
                and report.score is not None
            ):
                unknown_reports += 1
                logger.debug(
                    "score of run %s is unknown; score record %s unchanged", run.task_id, key
                )
                continue
            observed += 1
            current = mine.get(key)
            if current is None or (run.completed_at, run.task_id) > (
                current.completed_at,
                current.task_id,
            ):
                mine[key] = ScoreRecord(
                    project_id=pid,
                    repo_id=repo.repository.id,
                    action_key=run.action_key,
                    task_id=run.task_id,
                    completed_at=run.completed_at,
                    score=report.score,
                )

    low: list[LowScore] = []
    for key in sorted(mine):
        record = mine[key]
        threshold = thresholds.get(record.action_key)
        if threshold is None or record.score >= threshold:
            continue
        logger.info(
            "score below threshold: %s/%s/%s %d < %d",
            pid,
            record.repo_id,
            record.action_key,
            record.score,
            threshold,
        )
        low.append(
            LowScore(
                project_id=pid,
                project_name=project.project.name,
                repo_id=record.repo_id,
                repo_name=repo_names[record.repo_id],
                action_key=record.action_key,
                score=record.score,
                threshold=threshold,
                completed_at=record.completed_at,
            )
        )
    low.sort(
        key=lambda item: (
            item.completed_at,
            item.score,
            item.project_name.casefold(),
            item.repo_id,
            item.action_key,
        )
    )
    return ScoreEvaluation(
        records=tuple(mine[key] for key in sorted(mine)),
        low=tuple(low),
        observed=observed,
        unknown_reports=unknown_reports,
    )
