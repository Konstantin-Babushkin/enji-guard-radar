"""The Claude line is an extra on one item: it never changes order, keys, counts or the set."""

import logging

import pytest
from test_messages import high, never_ran, stale

from enji_guard_radar.messages import render_messages
from enji_guard_radar.triage import ItemTriage


def item(cause: str = "SQL injection in order search", priority: str = "p1") -> ItemTriage:
    return ItemTriage.model_validate({"item_id": "i1", "priority": priority, "cause": cause})


def test_single_alert_with_cause() -> None:
    alert = high()
    messages = render_messages([alert], collapse_above=3, triage={alert.key: item()})
    assert [m.text for m in messages] == [
        "🔴 <b>Project A</b> · security High 0→1 · 8d\n   P1 · SQL injection in order search"
    ]
    assert messages[0].keys == (alert.key,)
