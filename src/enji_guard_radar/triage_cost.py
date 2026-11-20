"""What triage costs per UTC day (D-14, TRI-05). Core module: pure, the day is injected.

One record per UTC day, the last KEEP_DAYS kept. The file that holds them is
`triage_cost_store`; this module only builds the records, the two log lines and the document.
"""

from datetime import date
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from enji_guard_radar.triage import TriageFailed, TriageOutcome

KEEP_DAYS = 35


class CostDay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    day: date
    calls: int
    failures: int
    seconds: float
    cost_usd: float
    unknown_cost_calls: int
    input_tokens: int
    output_tokens: int


class TriageCostStore(Protocol):
    def load(self) -> tuple[CostDay, ...]: ...

    def save(self, days: tuple[CostDay, ...]) -> None: ...


class _Document(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    days: tuple[CostDay, ...]


def add_call(
    days: tuple[CostDay, ...], outcome: TriageOutcome, *, today: date
) -> tuple[CostDay, ...]:
    """Add one call to `today`'s record; keep the newest KEEP_DAYS records, oldest first."""
    current = next(
        (d for d in days if d.day == today),
        CostDay(
            day=today,
            calls=0,
            failures=0,
            seconds=0.0,
            cost_usd=0.0,
            unknown_cost_calls=0,
            input_tokens=0,
            output_tokens=0,
        ),
    )
    updated = current.model_copy(
        update={
            "calls": current.calls + 1,
            "failures": current.failures + (1 if isinstance(outcome, TriageFailed) else 0),
            "seconds": current.seconds + outcome.seconds,
            "cost_usd": current.cost_usd + (outcome.cost_usd or 0.0),
            "unknown_cost_calls": current.unknown_cost_calls
            + (1 if outcome.cost_usd is None else 0),
            "input_tokens": current.input_tokens + (outcome.input_tokens or 0),
            "output_tokens": current.output_tokens + (outcome.output_tokens or 0),
        }
    )
    others = [d for d in days if d.day != today]
    return tuple(sorted([*others, updated], key=lambda d: d.day)[-KEEP_DAYS:])


def _money(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _known(value: float | int | None, text: str) -> str:
    return "unknown" if value is None else text


def call_line(items: int, outcome: TriageOutcome) -> str:
    """The per-call INFO line; a value the CLI did not report reads `unknown`."""
    result = outcome.reason.value if isinstance(outcome, TriageFailed) else "ok"
    cost = _known(outcome.cost_usd, _money(outcome.cost_usd or 0.0))
    return (
        f"triage call: items {items}, outcome {result}, seconds {outcome.seconds:.1f}, "
        f"cost_usd {cost}, "
        f"input_tokens {_known(outcome.input_tokens, str(outcome.input_tokens))}, "
        f"output_tokens {_known(outcome.output_tokens, str(outcome.output_tokens))}"
    )


def daily_line(day: CostDay) -> str:
    """`$unknown` when no call that day reported a cost."""
    cost = "unknown" if day.unknown_cost_calls == day.calls else _money(day.cost_usd)
    return f"triage today (UTC): {day.calls} calls, {day.failures} failed, ${cost}"


def serialize_days(days: tuple[CostDay, ...]) -> bytes:
    return (_Document(version=1, days=days).model_dump_json(indent=2) + "\n").encode("utf-8")


def deserialize_days(data: bytes) -> tuple[CostDay, ...]:
    """Raises ValueError on a bad document: bad JSON, version other than 1, missing or extra field."""
    return _Document.model_validate_json(data).days
