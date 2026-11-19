"""Poll health: is the radar itself able to read Guard? (OPS-02)

Pure: no I/O and no clock reads (callers pass `now`). Every poll is classified ok or failed with
a cause; consecutive failures are counted in `PollHealth`. After K failures in a row one outage
message goes to the ops chat, and one recovery message after the first good poll. The marks
(`outage_reported_at` and its clearing) are set by `mark_ops_delivered`, which the caller applies
only after the ops chat acknowledged the message, so an unacknowledged message is retried.
No text built here carries a token, code or URL.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from enji_guard_radar.guard import ListingUnknown, ProjectListing, ProjectUnknown
from enji_guard_radar.guard_auth import AUTH_COMMAND
from enji_guard_radar.ledger import FailureCause, PollHealth
from enji_guard_radar.messages import escape
from enji_guard_radar.model import Audience, OutboundMessage
from enji_guard_radar.ports import AuthFailureKind, GuardFailure
from enji_guard_radar.texts import DEFAULT_LANGUAGE, Language, counted, text

# When several projects fail for different reasons, the one the operator can act on wins.
_CAUSE_PRECEDENCE = (GuardFailure.AUTH, GuardFailure.UNAVAILABLE, GuardFailure.DATA)
# The English texts; the catalog holds them for every language.
_UNAVAILABLE_TEXT = text("en", "ops.reason.unavailable")
_DATA_TEXT = text("en", "ops.reason.data")
_AUTH_FALLBACK_TEXT = text("en", "ops.reason.auth_fallback", command=AUTH_COMMAND)
_TIME_FORMAT = "%Y-%m-%d %H:%M"


@dataclass(frozen=True)
class PollOk:
    pass


@dataclass(frozen=True)
class PollFailed:
    cause: GuardFailure
    auth_kind: AuthFailureKind | None = None


PollResult = PollOk | PollFailed


@dataclass(frozen=True)
class Outage:
    cause: GuardFailure
    auth_kind: AuthFailureKind | None
    since: datetime
    failures: int


@dataclass(frozen=True)
class Recovered:
    since: datetime


@dataclass(frozen=True)
class NoOpsAction:
    pass


OpsAction = Outage | Recovered | NoOpsAction


@dataclass(frozen=True)
class HealthStep:
    health: PollHealth
    action: OpsAction


def classify_poll(
    listing: ProjectListing, unknown_projects: Sequence[ProjectUnknown], active_count: int
) -> PollResult:
    """Failed: the listing is unknown, or there are active projects and all are unknown.

    Some projects unknown is not a failed poll; neither is having no active project.
    """
    if isinstance(listing, ListingUnknown):
        return PollFailed(cause=listing.cause, auth_kind=listing.auth_kind)
    if active_count == 0 or len(unknown_projects) < active_count:
        return PollOk()
    for cause in _CAUSE_PRECEDENCE:
        matching = [u for u in unknown_projects if u.cause is cause]
        if matching:
            kinds = [u.auth_kind for u in matching if u.auth_kind is not None]
            return PollFailed(cause=cause, auth_kind=kinds[0] if kinds else None)
    return PollFailed(cause=GuardFailure.DATA)  # unreachable: every cause is listed above


def advance_health(
    health: PollHealth, poll: PollResult, now: datetime, *, failures_before_alert: int
) -> HealthStep:
    if isinstance(poll, PollOk):
        if health.outage_reported_at is None:
            # A streak that never reached the ops chat ends silently. last_success_at is stamped
            # only when a streak ends, so a radar that never failed keeps an empty health block.
            ended = PollHealth(last_success_at=now) if health.consecutive_failures else health
            return HealthStep(ended, NoOpsAction())
        since = health.first_failure_at if health.first_failure_at is not None else now
        reset = health.model_copy(update={"consecutive_failures": 0, "last_success_at": now})
        return HealthStep(reset, Recovered(since=since))
    failures = health.consecutive_failures + 1
    since = health.first_failure_at if health.first_failure_at is not None else now
    stepped = health.model_copy(
        update={
            "consecutive_failures": failures,
            "first_failure_at": since,
            "last_failure_cause": _cause_value(poll.cause),
        }
    )
    if failures >= failures_before_alert and health.outage_reported_at is None:
        return HealthStep(
            stepped,
            Outage(cause=poll.cause, auth_kind=poll.auth_kind, since=since, failures=failures),
        )
    return HealthStep(stepped, NoOpsAction())


def mark_ops_delivered(health: PollHealth, action: OpsAction, now: datetime) -> PollHealth:
    """Call only after the ops chat acknowledged the message of `action`."""
    if isinstance(action, Outage):
        return health.model_copy(update={"outage_reported_at": now})
    if isinstance(action, Recovered):
        return health.model_copy(
            update={
                "outage_reported_at": None,
                "first_failure_at": None,
                "last_failure_cause": None,
            }
        )
    return health


def ops_message(
    action: Outage | Recovered, *, language: Language = DEFAULT_LANGUAGE
) -> OutboundMessage:
    if isinstance(action, Recovered):
        recovered = text(language, "ops.recovered", since=_utc_text(action.since))
        return OutboundMessage(text=escape(recovered), audience=Audience.OPS)
    outage = text(
        language,
        "ops.outage",
        reason=_reason_text(action, language),
        since=_utc_text(action.since),
        failures=counted(language, "noun.failed_poll", action.failures),
    )
    return OutboundMessage(text=escape(outage), audience=Audience.OPS)


def _reason_text(action: Outage, language: Language) -> str:
    if action.cause is GuardFailure.AUTH:
        key = "auth_fallback" if action.auth_kind is None else action.auth_kind.value
        return text(language, f"ops.reason.{key}", command=AUTH_COMMAND)
    if action.cause is GuardFailure.UNAVAILABLE:
        return text(language, "ops.reason.unavailable")
    return text(language, "ops.reason.data")


def _utc_text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_TIME_FORMAT)


def _cause_value(cause: GuardFailure) -> FailureCause:
    if cause is GuardFailure.AUTH:
        return "auth"
    if cause is GuardFailure.UNAVAILABLE:
        return "unavailable"
    return "data"
