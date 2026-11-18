"""Poll health (OPS-02): classification, the failure-count state machine and the ops texts."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest
from fake_oauth import (
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    ROTATED_ACCESS_TOKEN,
    ROTATED_REFRESH_TOKEN,
)
from guard_fakes import InMemoryGuardTools, Key, Response, call_key, projects_page, repos_page

from enji_guard_radar.guard import (
    ARG_PROJECT_ID,
    ARG_REPOSITORY_ID,
    TOOL_LIST_AUDIT_RUNS,
    TOOL_LIST_PROJECTS,
    TOOL_LIST_REPOSITORIES,
    GuardProject,
    ListingOk,
    ListingUnknown,
    ProjectUnknown,
    fetch_project,
    list_projects,
)
from enji_guard_radar.guard_auth import AUTH_FAILURE_TEXT
from enji_guard_radar.health import (
    NoOpsAction,
    OpsAction,
    Outage,
    PollFailed,
    PollOk,
    PollResult,
    Recovered,
    advance_health,
    classify_poll,
    mark_ops_delivered,
    ops_message,
)
from enji_guard_radar.ledger import PollHealth
from enji_guard_radar.model import Audience
from enji_guard_radar.ports import AuthFailureKind, GuardAuthError, GuardCallError, GuardFailure
from enji_guard_radar.secret_guard import find_secret

T0 = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
PROJECT = GuardProject(id="p1", name="Project One")
FAIL = PollFailed(cause=GuardFailure.UNAVAILABLE)
OK = PollOk()
LISTING = ListingOk(projects=(PROJECT,))


def at(step: int) -> datetime:
    return T0 + timedelta(hours=3 * step)


def unknown(cause: GuardFailure, kind: AuthFailureKind | None = None) -> ProjectUnknown:
    return ProjectUnknown(project=PROJECT, reason="x", cause=cause, auth_kind=kind)


# ---- guard: cause plumbing -------------------------------------------------------------------


def listing_failure(error: Exception) -> ListingUnknown:
    tools = InMemoryGuardTools({call_key(TOOL_LIST_PROJECTS): error})
    result = list_projects(tools)
    assert isinstance(result, ListingUnknown)
    return result


def test_guard_auth_error_on_listing_carries_cause_and_kind() -> None:
    result = listing_failure(GuardAuthError(AuthFailureKind.AUTH_EXPIRED, "expired"))
    assert (result.cause, result.auth_kind) == (GuardFailure.AUTH, AuthFailureKind.AUTH_EXPIRED)


def test_guard_plain_call_error_on_listing_is_unavailable() -> None:
    result = listing_failure(GuardCallError("boom"))
    assert (result.cause, result.auth_kind) == (GuardFailure.UNAVAILABLE, None)


def test_guard_call_error_with_data_cause_on_listing_is_data() -> None:
    result = listing_failure(GuardCallError("odd", cause=GuardFailure.DATA))
    assert (result.cause, result.auth_kind) == (GuardFailure.DATA, None)


def test_guard_malformed_page_and_empty_listing_are_data() -> None:
    malformed = InMemoryGuardTools({call_key(TOOL_LIST_PROJECTS): {"nonsense": True}})
    result = list_projects(malformed)
    assert isinstance(result, ListingUnknown)
    assert result.cause is GuardFailure.DATA
    empty = InMemoryGuardTools({call_key(TOOL_LIST_PROJECTS): projects_page()})
    result = list_projects(empty)
    assert isinstance(result, ListingUnknown)
    assert (result.cause, result.auth_kind) == (GuardFailure.DATA, None)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (TOOL_LIST_REPOSITORIES, {ARG_PROJECT_ID: "p1"}),
        (TOOL_LIST_AUDIT_RUNS, {ARG_REPOSITORY_ID: "r1"}),
    ],
)
@pytest.mark.parametrize(
    ("error", "cause", "kind"),
    [
        (
            GuardAuthError(AuthFailureKind.AUTH_REJECTED, "rejected"),
            GuardFailure.AUTH,
            AuthFailureKind.AUTH_REJECTED,
        ),
        (GuardCallError("boom"), GuardFailure.UNAVAILABLE, None),
        (GuardCallError("odd", cause=GuardFailure.DATA), GuardFailure.DATA, None),
    ],
)
def test_guard_project_unknown_carries_cause_and_kind(
    tool: str,
    args: dict[str, str],
    error: Exception,
    cause: GuardFailure,
    kind: AuthFailureKind | None,
) -> None:
    responses: dict[Key, Response] = {
        call_key(TOOL_LIST_REPOSITORIES, **{ARG_PROJECT_ID: "p1"}): repos_page(
            "p1", ("r1", "repo")
        ),
        call_key(tool, **args): error,
    }
    tools = InMemoryGuardTools(responses)
    result = fetch_project(tools, PROJECT)
    assert isinstance(result, ProjectUnknown)
    assert (result.cause, result.auth_kind) == (cause, kind)


# ---- classify_poll ---------------------------------------------------------------------------


def test_classify_unknown_listing_is_failed_with_its_cause() -> None:
    listing = ListingUnknown(
        reason="x", cause=GuardFailure.AUTH, auth_kind=AuthFailureKind.AUTH_CLIENT_GONE
    )
    assert classify_poll(listing, (), 0) == PollFailed(
        GuardFailure.AUTH, AuthFailureKind.AUTH_CLIENT_GONE
    )


def test_classify_all_active_unknown_takes_auth_first() -> None:
    unknowns = (
        unknown(GuardFailure.UNAVAILABLE),
        unknown(GuardFailure.AUTH, AuthFailureKind.AUTH_EXPIRED),
        unknown(GuardFailure.DATA),
    )
    assert classify_poll(LISTING, unknowns, 3) == PollFailed(
        GuardFailure.AUTH, AuthFailureKind.AUTH_EXPIRED
    )


def test_classify_all_active_unknown_prefers_unavailable_over_data() -> None:
    unknowns = (unknown(GuardFailure.DATA), unknown(GuardFailure.UNAVAILABLE))
    assert classify_poll(LISTING, unknowns, 2) == PollFailed(GuardFailure.UNAVAILABLE, None)


def test_classify_all_data_unknown_is_data() -> None:
    unknowns = (unknown(GuardFailure.DATA), unknown(GuardFailure.DATA))
    assert classify_poll(LISTING, unknowns, 2) == PollFailed(GuardFailure.DATA, None)


def test_classify_some_unknown_is_not_a_failed_poll() -> None:
    unknowns = (unknown(GuardFailure.UNAVAILABLE), unknown(GuardFailure.AUTH))
    assert classify_poll(LISTING, unknowns, 3) == PollOk()


def test_classify_no_active_projects_is_ok() -> None:
    assert classify_poll(LISTING, (), 0) == PollOk()


# ---- advance_health --------------------------------------------------------------------------


def run(
    polls: list[PollResult], k: int = 3, health: PollHealth | None = None, *, ack: bool = True
) -> tuple[PollHealth, list[OpsAction]]:
    """Feed polls one by one; return the final health and every action taken."""
    current = health or PollHealth()
    actions: list[OpsAction] = []
    for step, poll in enumerate(polls):
        result = advance_health(current, poll, at(step), failures_before_alert=k)
        current = result.health
        actions.append(result.action)
        if ack:
            current = mark_ops_delivered(current, result.action, at(step))
    return current, actions


def test_k_minus_one_failures_send_nothing() -> None:
    health, actions = run([FAIL, FAIL])
    assert actions == [NoOpsAction(), NoOpsAction()]
    assert health.consecutive_failures == 2
    assert health.outage_reported_at is None


def test_third_failure_sends_one_outage_with_the_first_failure_time() -> None:
    _, actions = run([FAIL, FAIL, FAIL, FAIL])
    assert actions[2] == Outage(GuardFailure.UNAVAILABLE, None, at(0), 3)
    assert actions[3] == NoOpsAction()


def test_k_of_one_alerts_on_the_first_failure() -> None:
    _, actions = run([FAIL], k=1)
    assert actions == [Outage(GuardFailure.UNAVAILABLE, None, at(0), 1)]


def test_cause_change_during_an_outage_sends_no_second_message() -> None:
    auth = PollFailed(GuardFailure.AUTH, AuthFailureKind.AUTH_EXPIRED)
    health, actions = run([FAIL, FAIL, FAIL, auth, auth])
    assert [a for a in actions if isinstance(a, Outage)] == [actions[2]]
    assert health.last_failure_cause == "auth"


def test_first_good_poll_after_a_reported_outage_sends_one_recovery() -> None:
    health, actions = run([FAIL, FAIL, FAIL, OK, OK])
    assert actions[3] == Recovered(since=at(0))
    assert actions[4] == NoOpsAction()
    assert health.outage_reported_at is None
    assert health.first_failure_at is None
    assert health.last_failure_cause is None
    assert health.consecutive_failures == 0
    assert health.last_success_at == at(3)


def test_good_poll_after_an_unreported_streak_resets_silently() -> None:
    health, actions = run([FAIL, FAIL, OK])
    assert actions == [NoOpsAction()] * 3
    assert health == PollHealth(last_success_at=at(2))


def test_good_polls_without_failures_leave_health_empty() -> None:
    health, actions = run([OK, OK])
    assert actions == [NoOpsAction(), NoOpsAction()]
    assert health == PollHealth()


def test_unacknowledged_outage_is_sent_again_on_the_next_failure() -> None:
    health, actions = run([FAIL, FAIL, FAIL, FAIL], ack=False)
    assert [isinstance(a, Outage) for a in actions] == [False, False, True, True]
    assert actions[3] == Outage(GuardFailure.UNAVAILABLE, None, at(0), 4)
    assert health.outage_reported_at is None


def test_unacknowledged_recovery_is_sent_again_on_the_next_good_poll() -> None:
    reported, _ = run([FAIL, FAIL, FAIL])
    health, actions = run([OK, OK], health=reported, ack=False)
    assert actions == [Recovered(since=at(0)), Recovered(since=at(0))]
    assert health.outage_reported_at is not None


def test_failure_after_an_unacknowledged_recovery_sends_no_new_outage() -> None:
    reported, _ = run([FAIL, FAIL, FAIL])
    after_ok = advance_health(reported, OK, at(10), failures_before_alert=3)
    assert isinstance(after_ok.action, Recovered)
    health, actions = run([FAIL, FAIL, FAIL], health=after_ok.health)
    assert actions == [NoOpsAction()] * 3
    assert health.outage_reported_at is not None


@pytest.mark.parametrize("k", [1, 2, 3, 4])
@pytest.mark.parametrize("length", range(1, 9))
def test_sequence_property_one_outage_and_one_recovery_per_streak(k: int, length: int) -> None:
    for pattern in itertools.product((False, True), repeat=length):
        polls: list[PollResult] = [FAIL if failed else OK for failed in pattern]
        _, actions = run(polls, k=k)
        outages = sum(isinstance(a, Outage) for a in actions)
        recoveries = sum(isinstance(a, Recovered) for a in actions)
        groups = [(failed, len(list(g))) for failed, g in itertools.groupby(pattern)]
        long_streaks = 0
        recovered = 0
        for position, (failed, size) in enumerate(groups):
            if failed and size >= k:
                long_streaks += 1
                if position + 1 < len(groups):
                    recovered += 1
        assert outages == long_streaks
        assert recoveries == recovered


# ---- ops_message -----------------------------------------------------------------------------


def outage(cause: GuardFailure, kind: AuthFailureKind | None = None) -> Outage:
    return Outage(cause=cause, auth_kind=kind, since=T0, failures=3)


@pytest.mark.parametrize("kind", list(AuthFailureKind))
def test_auth_outage_text_names_the_auth_command_and_kind_text(kind: AuthFailureKind) -> None:
    message = ops_message(outage(GuardFailure.AUTH, kind))
    assert message.audience is Audience.OPS
    assert "enji-guard-radar auth" in message.text
    assert message.text.startswith(f"Guard radar cannot read Guard: {AUTH_FAILURE_TEXT[kind]}.")


def test_auth_outage_without_a_kind_still_names_the_command() -> None:
    assert "enji-guard-radar auth" in ops_message(outage(GuardFailure.AUTH)).text


def test_unavailable_and_data_outage_texts_never_ask_for_reauth() -> None:
    for cause in (GuardFailure.UNAVAILABLE, GuardFailure.DATA):
        assert "auth" not in ops_message(outage(cause)).text
    assert ops_message(outage(GuardFailure.UNAVAILABLE)).text == (
        "Guard radar cannot read Guard: Guard is unreachable or failing. "
        "Failing since 2026-11-16 00:00 UTC (3 failed polls in a row). "
        "The radar keeps polling and reports here once when it recovers."
    )
    assert "check the radar log" in ops_message(outage(GuardFailure.DATA)).text


def test_recovery_text_names_the_outage_start_and_no_count() -> None:
    message = ops_message(Recovered(since=T0))
    assert message.text == (
        "Guard radar recovered: Guard polls work again (failing since 2026-11-16 00:00 UTC)."
    )
    assert message.audience is Audience.OPS


def test_ops_texts_carry_no_url_token_or_secret() -> None:
    secrets = (FAKE_ACCESS_TOKEN, FAKE_REFRESH_TOKEN, ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)
    actions: list[Outage | Recovered] = [Recovered(since=T0)]
    actions += [outage(GuardFailure.AUTH, kind) for kind in AuthFailureKind]
    actions += [outage(GuardFailure.UNAVAILABLE), outage(GuardFailure.DATA)]
    for action in actions:
        text = ops_message(action).text
        assert "http" not in text.lower()
        assert "bearer" not in text.lower()
        assert not any(secret in text for secret in secrets)
        assert find_secret(text, secrets) is None
