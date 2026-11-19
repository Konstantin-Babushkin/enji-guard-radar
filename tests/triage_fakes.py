"""Shared fake for every Phase 4 plan: scripted triage outcomes, no network, no `claude` binary."""

from collections.abc import Callable
from typing import Literal

from enji_guard_radar.triage import (
    ItemTriage,
    TriageOutcome,
    TriageRequest,
    TriageResult,
)

DEFAULT_CAUSE = "SQL injection in order search"


class FakeTriager:
    def __init__(
        self, outcomes: list[TriageOutcome] | Callable[[TriageRequest], TriageOutcome]
    ) -> None:
        self._outcomes = list(outcomes) if isinstance(outcomes, list) else outcomes
        self.requests: list[TriageRequest] = []
        self.calls = 0

    def triage(self, request: TriageRequest) -> TriageOutcome:
        self.calls += 1
        self.requests.append(request)
        if callable(self._outcomes):
            return self._outcomes(request)
        if not self._outcomes:
            message = "FakeTriager script ran out: unexpected extra call"
            raise AssertionError(message)
        return self._outcomes.pop(0)


def reply_for(
    request: TriageRequest,
    priority: Literal["p1", "p2", "p3"] = "p1",
    cause: str = DEFAULT_CAUSE,
) -> str:
    """A valid reply text: one entry per request item."""
    result = TriageResult(
        items=[
            ItemTriage(item_id=item.item_id, priority=priority, cause=cause)
            for item in request.items
        ]
    )
    return result.model_dump_json()
