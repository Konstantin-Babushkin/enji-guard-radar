"""One radar run: list projects, read each, apply the stale rule, render, notify.

Pure orchestration: every effect (Guard, notifier, clock) is injected through `Deps`.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from enji_guard_radar.config import RadarConfig
from enji_guard_radar.guard import ListingUnknown, ProjectUnknown, fetch_project, list_projects
from enji_guard_radar.messages import render_messages
from enji_guard_radar.model import Alert, OutboundMessage
from enji_guard_radar.ports import (
    Clock,
    Delivered,
    GuardTools,
    NotDelivered,
    Notifier,
    Printed,
)
from enji_guard_radar.stale import StaleResult, alert_for, evaluate_stale

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Deps:
    guard: GuardTools
    notifier: Notifier
    config: RadarConfig
    clock: Clock


class ProjectStatus(StrEnum):
    EVALUATED = "evaluated"
    UNKNOWN = "unknown"
    PAUSED = "paused"


class ProjectResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    project_name: str
    status: ProjectStatus
    stale: StaleResult | None
    note: str


class RunOutcome(StrEnum):
    COMPLETED = "completed"
    ATTENTION = "attention"
    GUARD_UNAVAILABLE = "guard_unavailable"


EXIT_CODES = {
    RunOutcome.COMPLETED: 0,
    RunOutcome.ATTENTION: 1,
    RunOutcome.GUARD_UNAVAILABLE: 1,
}


class RunReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    outcome: RunOutcome
    results: tuple[ProjectResult, ...]
    messages: tuple[OutboundMessage, ...]
    delivered: int
    printed: int
    not_delivered: int

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]


def run_once(deps: Deps) -> RunReport:
    now = deps.clock()
    listing = list_projects(deps.guard)
    if isinstance(listing, ListingUnknown):
        logger.error("run ended: Guard unavailable (%s)", listing.reason)
        return RunReport(
            now=now,
            outcome=RunOutcome.GUARD_UNAVAILABLE,
            results=(),
            messages=(),
            delivered=0,
            printed=0,
            not_delivered=0,
        )

    listed_ids = {project.id for project in listing.projects}
    for configured_id in deps.config.projects:
        if configured_id not in listed_ids:
            logger.warning("config names project id %s, which Guard did not list", configured_id)

    results: list[ProjectResult] = []
    alerts: list[Alert] = []
    for project in sorted(listing.projects, key=lambda p: (p.name.casefold(), p.id)):
        mode = deps.config.mode_for(project.id)
        if mode == "excluded":
            logger.debug("project %s excluded in config", project.id)
            continue
        if mode == "paused":
            logger.info("project %s paused in config; not checked", project.id)
            results.append(
                ProjectResult(
                    project_id=project.id,
                    project_name=project.name,
                    status=ProjectStatus.PAUSED,
                    stale=None,
                    note="paused in config",
                )
            )
            continue
        data = fetch_project(deps.guard, project)
        threshold = deps.config.thresholds_for(project.id).stale_after_days
        stale = evaluate_stale(data, threshold, now)
        if stale.future_run:
            logger.warning(
                "newest Guard run of project %s is in the future (%s); treated as fresh",
                project.id,
                stale.newest_run_at,
            )
        if isinstance(data, ProjectUnknown):
            status, note = ProjectStatus.UNKNOWN, f"Guard data unavailable ({data.reason})"
        else:
            status, note = ProjectStatus.EVALUATED, ""
        results.append(
            ProjectResult(
                project_id=project.id,
                project_name=project.name,
                status=status,
                stale=stale,
                note=note,
            )
        )
        alert = alert_for(stale)
        if alert is not None:
            alerts.append(alert)

    messages = render_messages(alerts, collapse_above=deps.config.alerts.collapse_above)
    delivered = printed = not_delivered = 0
    for message in messages:
        outcome = deps.notifier.send(message)
        if isinstance(outcome, Delivered):
            delivered += 1
        elif isinstance(outcome, Printed):
            printed += 1
        elif isinstance(outcome, NotDelivered):
            not_delivered += 1
            logger.warning("message not delivered: %s (%s)", outcome.error, outcome.detail)
            if outcome.operator_action is not None:
                logger.error("operator action needed: %s", outcome.operator_action)
            if outcome.stop_sending:
                logger.warning("stopping delivery after a fatal send error")
                break

    run_outcome = RunOutcome.ATTENTION if not_delivered else RunOutcome.COMPLETED
    logger.info(
        "run finished: %d projects, %d alerts, %d delivered, %d printed, %d not delivered",
        len(results),
        len(messages),
        delivered,
        printed,
        not_delivered,
    )
    return RunReport(
        now=now,
        outcome=run_outcome,
        results=tuple(results),
        messages=tuple(messages),
        delivered=delivered,
        printed=printed,
        not_delivered=not_delivered,
    )
