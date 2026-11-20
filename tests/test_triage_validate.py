"""validate_reply: Claude's reply in, accepted triage per alert out. Offline, no Guard data."""

import json

import pytest
from triage_fakes import reply_for

from enji_guard_radar.model import AlertKey, Severity
from enji_guard_radar.triage import (
    BuiltRequest,
    TriageCandidate,
    TriageFailure,
    build_request,
    validate_reply,
)

HIGH = Severity.HIGH


def cand(n: int) -> TriageCandidate:
    key = AlertKey(project_id=f"proj-{n}", repo_id=f"repo-{n}", level="high", action_key="audit.x")
    return TriageCandidate(
        key=key,
        action_key="audit.security",
        high_count=n,
        age_days=n,
        titles=((HIGH, f"Broken access control {n}"),),
    )


def built_for(count: int = 2) -> BuiltRequest:
    return build_request([cand(n) for n in range(1, count + 1)], language="en", max_items=30)


def reply(*items: tuple[str, str, str]) -> str:
    return json.dumps({"items": [{"item_id": i, "priority": p, "cause": c} for i, p, c in items]})


def test_good_reply_accepted() -> None:
    built = built_for()
    text = (
        '{"items":[{"item_id":"i1","priority":"P1","cause":"SQL injection in order search"},'
        '{"item_id":"i2","priority":"p3","cause":"Weak password hashing"}]}'
    )
    result = validate_reply(built, text)
    assert set(result.accepted) == set(built.ids.values())
    assert result.accepted[built.ids["i1"]].priority == "p1"
    assert result.accepted[built.ids["i2"]].priority == "p3"
    assert result.accepted[built.ids["i1"]].cause == "SQL injection in order search"
    assert result.failure is None
    assert result.rejected == {}


def test_good_reply_from_shared_fake() -> None:
    built = built_for(3)
    result = validate_reply(built, reply_for(built.request, priority="p2"))
    assert len(result.accepted) == 3
    assert {item.priority for item in result.accepted.values()} == {"p2"}


@pytest.mark.parametrize("text", ["", "nope", "{", "[]", '"x"', "5", "null", "x" * (64 * 1024 + 1)])
def test_not_json(text: str) -> None:
    result = validate_reply(built_for(), text)
    assert result.failure is TriageFailure.NOT_JSON
    assert result.accepted == {}
    assert result.rejected == {}


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        '{"summary":"x"}',
        '{"items":[],"summary":"x"}',
        '{"items":{}}',
        '{"items":[5]}',
        '{"items":[{"item_id":"i1","priority":"p1","cause":"ok","extra":1}]}',
        '{"items":[{"item_id":"i1","priority":"p4","cause":"ok"}]}',
        '{"items":[{"item_id":"i1","priority":1,"cause":"ok"}]}',
        '{"items":[{"item_id":"i1","priority":"p1","cause":null}]}',
        '{"items":[{"item_id":"i1","priority":"p1"}]}',
    ],
)
def test_schema_invalid(text: str) -> None:
    result = validate_reply(built_for(), text)
    assert result.failure is TriageFailure.SCHEMA_INVALID
    assert result.accepted == {}
    assert result.rejected == {}


@pytest.mark.parametrize(
    "text",
    [
        reply(("i9", "p1", "ok")),
        reply(("i1", "p1", "ok"), ("i9", "p1", "ok")),
        reply(("i1", "p1", "ok"), ("i1", "p2", "ok")),
        reply(("i1", "p1", "ok"), ("i2", "p1", "ok"), ("i1", "p1", "ok")),
        reply(("i1", "p1", "ok"), ("i2", "p1", "ok"), ("i3", "p1", "ok")),
    ],
)
def test_ids_unknown_or_duplicate(text: str) -> None:
    result = validate_reply(built_for(), text)
    assert result.failure is TriageFailure.UNKNOWN_OR_DUPLICATE_IDS
    assert result.accepted == {}
    assert result.rejected == {}


def test_ids_one_bad_id_rejects_the_whole_reply_even_if_causes_are_bad_too() -> None:
    result = validate_reply(built_for(), reply(("i1", "p1", "3 findings"), ("i9", "p1", "ok")))
    assert result.failure is TriageFailure.UNKNOWN_OR_DUPLICATE_IDS
    assert result.rejected == {}


def test_schema_priority_is_case_insensitive_and_stored_lowercase() -> None:
    built = built_for()
    result = validate_reply(built, reply(("i1", "P1", "ok"), ("i2", "p2", "ok")))
    assert [item.priority for item in result.accepted.values()] == ["p1", "p2"]


def test_not_json_deep_nesting_is_caught() -> None:
    result = validate_reply(built_for(), "[" * (64 * 1024))
    assert result.failure is TriageFailure.NOT_JSON
