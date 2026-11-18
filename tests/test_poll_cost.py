"""Poll-cost counting and the exact log line."""

from collections.abc import Mapping

import pytest

from enji_guard_radar.poll_cost import CountingGuardTools, PollCost, format_poll_cost
from enji_guard_radar.ports import GuardCallError


class Ticks:
    """A fake monotonic clock that advances a fixed step on every read."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class Scripted:
    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.failing = failing
        self.error = GuardCallError("simulated")

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        if tool in self.failing:
            raise self.error
        return {"tool": tool, **args}


def test_counts_calls_per_tool_and_returns_the_answer() -> None:
    counting = CountingGuardTools(Scripted(), Ticks(0.5))
    assert counting.call("list_projects", {}) == {"tool": "list_projects"}
    counting.call("list_repositories", {"project_id": "p"})
    counting.call("list_repositories", {"project_id": "q"})
    cost = counting.cost()
    assert dict(cost.calls) == {"list_projects": 1, "list_repositories": 2}
    assert dict(cost.failed) == {}
    assert cost.seconds == pytest.approx(1.5)


def test_failure_is_reraised_unchanged_and_counted() -> None:
    inner = Scripted(frozenset({"list_audit_runs"}))
    counting = CountingGuardTools(inner, Ticks(1.0))
    with pytest.raises(GuardCallError) as caught:
        counting.call("list_audit_runs", {})
    assert caught.value is inner.error
    cost = counting.cost()
    assert dict(cost.calls) == {"list_audit_runs": 1}
    assert dict(cost.failed) == {"list_audit_runs": 1}
    assert cost.seconds == pytest.approx(1.0)


def test_nothing_called_is_an_empty_cost() -> None:
    cost = CountingGuardTools(Scripted(), Ticks(1.0)).cost()
    assert cost == PollCost(calls={}, failed={}, seconds=0.0)
    assert (
        format_poll_cost(cost, projects=0, repositories=0)
        == "poll cost: 0 calls (), 0 failed, 0.0 s, 0 projects, 0 repositories"
    )


def test_format_is_exact_and_sorted_by_tool() -> None:
    cost = PollCost(
        calls={"list_repositories": 8, "list_projects": 1, "get_audit_report": 3},
        failed={"list_repositories": 1, "get_audit_report": 1},
        seconds=12.34,
    )
    assert format_poll_cost(cost, projects=8, repositories=12) == (
        "poll cost: 12 calls (get_audit_report 3, list_projects 1, list_repositories 8), "
        "2 failed, 12.3 s, 8 projects, 12 repositories"
    )
