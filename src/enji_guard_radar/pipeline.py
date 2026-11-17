"""One radar run: lock, load state, read Guard, evaluate, reconcile, save, notify, mark.

Pure orchestration: every effect (Guard, notifier, store, clock) is injected through `Deps`.
An alert is marked as sent only after the notifier acknowledged its message, and the state is
saved right after that mark (at-least-once delivery).
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from enji_guard_radar.config import RadarConfig
from enji_guard_radar.guard import (
    ListingOk,
    ListingUnknown,
    ProjectUnknown,
    fetch_project,
    list_projects,
)
from enji_guard_radar.high_rules import evaluate_high
from enji_guard_radar.ledger import (
    Corrupt,
    EvaluationOutcome,
    Loaded,
    ProjectEvaluation,
    SeriesHistory,
    SeriesKey,
    State,
    StateStore,
    apply_delivery,
    reconcile,
)
from enji_guard_radar.messages import escape, render_messages
from enji_guard_radar.model import Alert, AlertKey, Audience, OutboundMessage
from enji_guard_radar.ports import (
    Clock,
    Delivered,
    DeliveryError,
    GuardTools,
    NotDelivered,
    Notifier,
    Printed,
    SendOutcome,
)
from enji_guard_radar.stale import StaleResult, alert_for, evaluate_stale

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Deps:
    guard: GuardTools
    notifier: Notifier
    config: RadarConfig
    clock: Clock
    store: StateStore
    ops_notifier: Notifier | None = None


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
    LOCKED = "locked"
    HALTED = "halted"


EXIT_CODES = {
    RunOutcome.COMPLETED: 0,
    RunOutcome.ATTENTION: 1,
    RunOutcome.GUARD_UNAVAILABLE: 1,
    RunOutcome.LOCKED: 0,
    RunOutcome.HALTED: 2,
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
    baselined: tuple[Alert, ...] = ()
    resolved: tuple[AlertKey, ...] = ()
    operator_actions: tuple[str, ...] = ()
    halt_reason: str | None = None

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]


def _empty_report(now: datetime, outcome: RunOutcome, halt_reason: str | None = None) -> RunReport:
    return RunReport(
        now=now,
        outcome=outcome,
        results=(),
        messages=(),
        delivered=0,
        printed=0,
        not_delivered=0,
        halt_reason=halt_reason,
    )


def ops_message_for_halt(reason: str, backup_valid: bool) -> OutboundMessage:
    backup = "yes" if backup_valid else "no"
    text = (
        f"Guard radar halted: the state file is corrupt ({escape(reason)}). "
        "Nothing was sent and nothing was reset. "
        f"Backup valid: {backup}. "
        "Check the file and restore the backup by hand only if it is right."
    )
    return OutboundMessage(text=text, audience=Audience.OPS)


def ops_message_for_actions(actions: tuple[str, ...]) -> OutboundMessage:
    lines = ["Guard radar needs attention:", *(f"- {escape(action)}" for action in actions)]
    return OutboundMessage(text="\n".join(lines), audience=Audience.OPS)


def run_once(deps: Deps) -> RunReport:
    now = deps.clock()
    with deps.store.lock() as acquired:
        if not acquired:
            logger.info("another radar run holds the state lock; exiting without doing anything")
            return _empty_report(now, RunOutcome.LOCKED)
        return _run_locked(deps, now)


def _run_locked(deps: Deps, now: datetime) -> RunReport:
    load = deps.store.load()
    if isinstance(load, Corrupt):
        return _halt(deps, now, load)
    if isinstance(load, Loaded):
        state = load.state
    else:
        logger.info("no state file yet: first run, current conditions are baselined")
        state = State.empty()

    listing = list_projects(deps.guard)
    if isinstance(listing, ListingUnknown):
        logger.error("run ended: Guard unavailable (%s)", listing.reason)
        return _empty_report(now, RunOutcome.GUARD_UNAVAILABLE)

    results, evaluations, histories = _evaluate_projects(
        deps, listing, now, {h.key(): h for h in state.histories}
    )
    state = state.model_copy(
        update={"histories": tuple(sorted(histories.values(), key=lambda h: h.key()))}
    )
    rec = reconcile(state, evaluations, now)
    logger.info(
        "planned %d, baselined %d, retried %d, rearmed %d, resolved %d",
        len(rec.to_send),
        len(rec.baselined),
        len(rec.retried),
        len(rec.rearmed),
        len(rec.resolved),
    )
    deps.store.save(rec.state)

    messages = render_messages(rec.to_send, collapse_above=deps.config.alerts.collapse_above)
    delivered = printed = not_delivered = 0
    actions: list[str] = []
    unauthorized = False
    state = rec.state
    for message in messages:
        outcome = deps.notifier.send(message)
        if isinstance(outcome, Delivered):
            state = apply_delivery(state, message.keys, now)
            deps.store.save(state)
            delivered += 1
        elif isinstance(outcome, Printed):
            printed += 1
        elif isinstance(outcome, NotDelivered):
            not_delivered += 1
            unauthorized = unauthorized or outcome.error is DeliveryError.UNAUTHORIZED
            logger.warning("message not delivered: %s (%s)", outcome.error, outcome.detail)
        _note_action(outcome, actions)
        if isinstance(outcome, NotDelivered) and outcome.stop_sending:
            logger.warning("stopping delivery after a fatal send error")
            break

    _notify_ops_of_actions(deps, actions, unauthorized=unauthorized)
    run_outcome = RunOutcome.ATTENTION if not_delivered or actions else RunOutcome.COMPLETED
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
        baselined=rec.baselined,
        resolved=rec.resolved,
        operator_actions=tuple(actions),
    )


def _halt(deps: Deps, now: datetime, load: Corrupt) -> RunReport:
    logger.error(
        "state file is corrupt: %s (backup valid: %s); the run is halted and nothing is reset",
        load.reason,
        load.backup_valid,
    )
    if deps.ops_notifier is not None:
        outcome = deps.ops_notifier.send(ops_message_for_halt(load.reason, load.backup_valid))
        logger.info("ops message about the corrupt state: %s", type(outcome).__name__)
    return _empty_report(now, RunOutcome.HALTED, halt_reason=load.reason)


def _note_action(outcome: SendOutcome, actions: list[str]) -> None:
    action = None if isinstance(outcome, Printed) else outcome.operator_action
    if action is not None:
        actions.append(action)
        logger.error("operator action needed: %s", action)


def _notify_ops_of_actions(deps: Deps, actions: list[str], *, unauthorized: bool) -> None:
    # With a rejected bot token the ops chat uses the same token, so a message cannot arrive.
    if not actions or deps.ops_notifier is None or unauthorized:
        return
    outcome = deps.ops_notifier.send(ops_message_for_actions(tuple(actions)))
    logger.info("ops message about operator actions: %s", type(outcome).__name__)


def _evaluate_projects(
    deps: Deps,
    listing: ListingOk,
    now: datetime,
    stored: dict[SeriesKey, SeriesHistory],
) -> tuple[list[ProjectResult], list[ProjectEvaluation], dict[SeriesKey, SeriesHistory]]:
    listed_ids = {project.id for project in listing.projects}
    for configured_id in deps.config.projects:
        if configured_id not in listed_ids:
            logger.warning("config names project id %s, which Guard did not list", configured_id)

    results: list[ProjectResult] = []
    evaluations: list[ProjectEvaluation] = []
    histories = dict(stored)
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
            evaluations.append(
                ProjectEvaluation(project_id=project.id, outcome=EvaluationOutcome.skipped)
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
            evaluations.append(
                ProjectEvaluation(project_id=project.id, outcome=EvaluationOutcome.unknown)
            )
        else:
            status, note = ProjectStatus.EVALUATED, ""
            stale_alert = alert_for(stale)
            high = evaluate_high(
                data,
                histories,
                clear_after=deps.config.high.clear_after_clean_runs,
                audit_types=deps.config.high.audit_types,
                aged_after_days=deps.config.thresholds_for(project.id).high_aged_after_days,
                now=now,
            )
            for key in [k for k in histories if k[0] == project.id]:
                del histories[key]
            histories.update({h.key(): h for h in high.histories})
            if high.histories or high.dropped:
                logger.info(
                    "high series of %s: %d observed, %d unknown report(s), %d open",
                    project.id,
                    high.observed,
                    high.unknown_reports,
                    len(high.alerts),
                )
            evaluations.append(
                ProjectEvaluation(
                    project_id=project.id,
                    outcome=EvaluationOutcome.evaluated,
                    alerts=((stale_alert,) if stale_alert is not None else ()) + high.alerts,
                )
            )
        results.append(
            ProjectResult(
                project_id=project.id,
                project_name=project.name,
                status=status,
                stale=stale,
                note=note,
            )
        )
    return results, evaluations, histories
