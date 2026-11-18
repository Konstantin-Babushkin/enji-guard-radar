"""What one poll cost: Guard calls per tool, failures and time. Pure: the clock is injected."""

from collections import Counter
from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict

from enji_guard_radar.ports import GuardTools


class PollCost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    calls: Mapping[str, int]
    failed: Mapping[str, int]
    seconds: float


class CountingGuardTools:
    """Counts calls, failed calls and time spent inside the wrapped source."""

    def __init__(self, inner: GuardTools, monotonic: Callable[[], float]) -> None:
        self._inner = inner
        self._monotonic = monotonic
        self._calls: Counter[str] = Counter()
        self._failed: Counter[str] = Counter()
        self._seconds = 0.0

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        self._calls[tool] += 1
        started = self._monotonic()
        try:
            return self._inner.call(tool, args)
        except Exception:
            self._failed[tool] += 1
            raise
        finally:
            self._seconds += self._monotonic() - started

    def cost(self) -> PollCost:
        return PollCost(calls=dict(self._calls), failed=dict(self._failed), seconds=self._seconds)


def format_poll_cost(cost: PollCost, *, projects: int, repositories: int) -> str:
    per_tool = ", ".join(f"{tool} {count}" for tool, count in sorted(cost.calls.items()))
    return (
        f"poll cost: {sum(cost.calls.values())} calls ({per_tool}), "
        f"{sum(cost.failed.values())} failed, {cost.seconds:.1f} s, "
        f"{projects} projects, {repositories} repositories"
    )
