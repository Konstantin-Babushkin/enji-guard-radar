"""One radar run: lock, load state, read Guard, evaluate, reconcile, save, notify, mark.

Pure orchestration: every effect (Guard, notifier, store, clock) is injected through `Deps`.
An alert is marked as sent only after the notifier acknowledged its message, and the state is
saved right after that mark (at-least-once delivery).
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from enji_guard_radar.config import RadarConfig
from enji_guard_radar.guard import (
    ListingOk,
    ListingUnknown,
    ProjectOk,
    ProjectUnknown,
    fetch_project,
    list_projects,
)
from enji_guard_radar.health import (
    NoOpsAction,
    OpsAction,
    Outage,
    PollFailed,
    advance_health,
    classify_poll,
    mark_ops_delivered,
    ops_message,
)
from enji_guard_radar.high_rules import evaluate_high, skip_report_for
from enji_guard_radar.ledger import (
    Corrupt,
    EvaluationOutcome,
    Loaded,
    ProjectEvaluation,
    ScoreRecord,
    SeriesHistory,
    SeriesKey,
    State,
    StateStore,
    ToolingRecord,
    append_resolved,
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
from enji_guard_radar.score_rules import SCORE_ACTION_KEYS, LowScore, evaluate_scores
from enji_guard_radar.stale import StaleResult, alert_for, evaluate_stale
from enji_guard_radar.tooling_rules import ToolingGap, evaluate_tooling

logger = logging.getLogger(__name__)

# Named in the log when the ops chat is not configured (the settings module is shell code).
OPS_CHAT_ENV = "TELEGRAM_OPS_CHAT_ID"


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
    poll_failed: bool = False
    ops_sent: int = 0
    # What this poll found for the digest; nothing here is ever sent as an alert (SC1, SC3).
    low_scores: tuple[LowScore, ...] = ()
    tooling_gaps: tuple[ToolingGap, ...] = ()

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]


def _empty_report(
    now: datetime,
    outcome: RunOutcome,
    halt_reason: str | None = None,
    *,
    poll_failed: bool = False,
    ops_sent: int = 0,
) -> RunReport:
    return RunReport(
        now=now,
        outcome=outcome,
        results=(),
        messages=(),
        delivered=0,
        printed=0,
        not_delivered=0,
        halt_reason=halt_reason,
        poll_failed=poll_failed,
        ops_sent=ops_sent,
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
    k = deps.config.ops.failures_before_alert
    if isinstance(listing, ListingUnknown):
        logger.error("run ended: Guard unavailable (%s)", listing.reason)
        step = advance_health(
            state.health,
            classify_poll(listing, (), 0),
            now,
            failures_before_alert=k,
        )
        state = state.model_copy(update={"health": step.health})
        _log_poll(step.health.consecutive_failures, True, listing.cause.value)
        deps.store.save(state)
        _, sent = _send_ops_action(deps, state, step.action, now)
        return _empty_report(now, RunOutcome.GUARD_UNAVAILABLE, poll_failed=True, ops_sent=sent)

    polled = _evaluate_projects(deps, listing, now, state)
    results = polled.results
    active = sum(1 for r in results if r.status is not ProjectStatus.PAUSED)
    poll = classify_poll(listing, polled.unknown, active)
    step = advance_health(state.health, poll, now, failures_before_alert=k)
    poll_failed = isinstance(poll, PollFailed)
    _log_poll(
        step.health.consecutive_failures,
        poll_failed,
        poll.cause.value if isinstance(poll, PollFailed) else "",
    )
    state = state.model_copy(
        update={
            "histories": tuple(sorted(polled.histories.values(), key=lambda h: h.key())),
            "scores": tuple(sorted(polled.scores.values(), key=lambda r: r.key())),
            "tooling": tuple(sorted(polled.tooling.values(), key=lambda r: r.key())),
            "health": step.health,
        }
    )
    rec = reconcile(state, polled.evaluations, now)
    # Everything that closed between polls is remembered for the digest, on disk before any send.
    rec = rec.model_copy(update={"state": append_resolved(rec.state, rec.resolved, now)})
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
    actions: list[str] = []
    alert_delivery = _deliver(
        deps,
        messages,
        rec.state,
        now,
        actions,
        lambda current, message: apply_delivery(current, message.keys, now),
    )
    state = alert_delivery.state
    delivered, printed, not_delivered = (
        alert_delivery.delivered,
        alert_delivery.printed,
        alert_delivery.not_delivered,
    )
    unauthorized = alert_delivery.unauthorized

    _notify_ops_of_actions(deps, actions, unauthorized=unauthorized)
    state, ops_sent = _send_ops_action(deps, state, step.action, now)
    if poll_failed:
        run_outcome = RunOutcome.GUARD_UNAVAILABLE
    elif not_delivered or actions:
        run_outcome = RunOutcome.ATTENTION
    else:
        run_outcome = RunOutcome.COMPLETED
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
        poll_failed=poll_failed,
        ops_sent=ops_sent,
        low_scores=polled.low_scores,
        tooling_gaps=polled.tooling_gaps,
    )


def _log_poll(failures: int, failed: bool, cause: str) -> None:
    if failed:
        logger.warning("poll failed (%s): %d in a row", cause, failures)
    else:
        logger.info("poll ok")


def _send_ops_action(
    deps: Deps, state: State, action: OpsAction, now: datetime
) -> tuple[State, int]:
    """Tell the ops chat about an outage or a recovery; mark it only once it was acknowledged."""
    if isinstance(action, NoOpsAction):
        return state, 0
    what = "outage" if isinstance(action, Outage) else "recovery"
    if deps.ops_notifier is None:
        logger.error("ops chat not configured (%s): cannot report the Guard %s", OPS_CHAT_ENV, what)
        return state, 0
    outcome = deps.ops_notifier.send(ops_message(action))
    if not isinstance(outcome, Delivered):
        logger.warning(
            "ops message about the Guard %s not delivered (%s); retried next run",
            what,
            type(outcome).__name__,
        )
        return state, 0
    state = state.model_copy(update={"health": mark_ops_delivered(state.health, action, now)})
    deps.store.save(state)
    logger.info("ops chat told: Guard %s", what)
    return state, 1


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


@dataclass(frozen=True)
class _Delivery:
    """What `_deliver` did: the state after the acknowledged marks and the fate of each send."""

    state: State
    delivered: int = 0
    printed: int = 0
    not_delivered: int = 0
    unauthorized: bool = False
    fatal: bool = False
    last_error: str | None = None


def _deliver(
    deps: Deps,
    messages: Sequence[OutboundMessage],
    state: State,
    now: datetime,
    actions: list[str],
    mark: Callable[[State, OutboundMessage], State],
) -> _Delivery:
    """Send each message; after an acknowledgement apply `mark` and save before the next send."""
    delivered = printed = not_delivered = 0
    unauthorized = fatal = False
    last_error: str | None = None
    for message in messages:
        outcome = deps.notifier.send(message)
        if isinstance(outcome, Delivered):
            state = mark(state, message)
            deps.store.save(state)
            delivered += 1
        elif isinstance(outcome, Printed):
            printed += 1
        elif isinstance(outcome, NotDelivered):
            not_delivered += 1
            unauthorized = unauthorized or outcome.error is DeliveryError.UNAUTHORIZED
            last_error = outcome.error.name
            logger.warning("message not delivered: %s (%s)", outcome.error, outcome.detail)
        _note_action(outcome, actions)
        if isinstance(outcome, NotDelivered) and outcome.stop_sending:
            logger.warning("stopping delivery after a fatal send error")
            fatal = True
            break
    return _Delivery(state, delivered, printed, not_delivered, unauthorized, fatal, last_error)


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


@dataclass(frozen=True)
class PollData:
    """What one poll read: per-project results, the new stored facts and what to digest."""

    results: tuple[ProjectResult, ...]
    evaluations: tuple[ProjectEvaluation, ...]
    histories: dict[SeriesKey, SeriesHistory]
    scores: dict[SeriesKey, ScoreRecord]
    tooling: dict[SeriesKey, ToolingRecord]
    unknown: tuple[ProjectUnknown, ...]
    low_scores: tuple[LowScore, ...]
    tooling_gaps: tuple[ToolingGap, ...]


def _evaluate_projects(
    deps: Deps,
    listing: ListingOk,
    now: datetime,
    state: State,
) -> PollData:
    listed_ids = {project.id for project in listing.projects}
    for configured_id in deps.config.projects:
        if configured_id not in listed_ids:
            logger.warning("config names project id %s, which Guard did not list", configured_id)

    results: list[ProjectResult] = []
    evaluations: list[ProjectEvaluation] = []
    histories = {h.key(): h for h in state.histories}
    scores = {r.key(): r for r in state.scores}
    tooling = {r.key(): r for r in state.tooling}
    low_scores: list[LowScore] = []
    tooling_gaps: list[ToolingGap] = []
    unknown: list[ProjectUnknown] = []
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
        skip = skip_report_for(project.id, histories, deps.config.high.audit_types, scores)
        data = fetch_project(deps.guard, project, skip_report=skip)
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
            unknown.append(data)
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
            _fold_scores_and_tooling(
                deps, data, now, scores=scores, tooling=tooling, low=low_scores, gaps=tooling_gaps
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
    return PollData(
        results=tuple(results),
        evaluations=tuple(evaluations),
        histories=histories,
        scores=scores,
        tooling=tooling,
        unknown=tuple(unknown),
        low_scores=tuple(low_scores),
        tooling_gaps=tuple(tooling_gaps),
    )


def _fold_scores_and_tooling(
    deps: Deps,
    data: ProjectOk,
    now: datetime,
    *,
    scores: dict[SeriesKey, ScoreRecord],
    tooling: dict[SeriesKey, ToolingRecord],
    low: list[LowScore],
    gaps: list[ToolingGap],
) -> None:
    """Replace this project's score and tooling records with the poll's and collect the findings."""
    pid = data.project.id
    thresholds = {
        key: value
        for key in SCORE_ACTION_KEYS
        if (value := deps.config.score_threshold_for(pid, key)) is not None
    }
    scored = evaluate_scores(data, scores, thresholds=thresholds)
    for key in [k for k in scores if k[0] == pid]:
        del scores[key]
    scores.update({record.key(): record for record in scored.records})
    low.extend(scored.low)
    logger.info(
        "scores of %s: %d recorded, %d low, %d unknown report(s)",
        pid,
        len(scored.records),
        len(scored.low),
        scored.unknown_reports,
    )

    tooled = evaluate_tooling(
        data,
        tooling,
        audit_types=deps.config.tooling_gaps.audit_types,
        window_days=deps.config.tooling_window_for(pid),
        now=now,
    )
    for key in [k for k in tooling if k[0] == pid]:
        del tooling[key]
    tooling.update({record.key(): record for record in tooled.records})
    gaps.extend(tooled.gaps)
    logger.info("tooling of %s: %d gap(s), %d undecided", pid, len(tooled.gaps), tooled.undecided)
