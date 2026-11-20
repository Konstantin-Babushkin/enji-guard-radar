"""Triage cost record, its two log lines and the cost file store. No Claude, no network."""

from datetime import date

from enji_guard_radar.triage import TriageReply
from enji_guard_radar.triage_cost import add_call, call_line, daily_line

DAY = date(2026, 11, 19)


def test_add_one_call() -> None:
    reply = TriageReply(text="x", seconds=4.2, cost_usd=0.003, input_tokens=900, output_tokens=40)
    days = add_call((), reply, today=DAY)
    assert len(days) == 1
    assert days[0].day == DAY
    assert (days[0].calls, days[0].failures) == (1, 0)
    assert days[0].cost_usd == 0.003
    assert call_line(2, reply) == (
        "triage call: items 2, outcome ok, seconds 4.2, cost_usd 0.003, "
        "input_tokens 900, output_tokens 40"
    )
    assert daily_line(days[0]) == "triage today (UTC): 1 calls, 0 failed, $0.003"
