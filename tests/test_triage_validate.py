"""validate_reply: Claude's reply in, accepted triage per alert out. Offline, no Guard data."""

import json
import logging
import random

import pytest
from triage_fakes import reply_for

from enji_guard_radar.model import AlertKey, Severity
from enji_guard_radar.triage import (
    BuiltRequest,
    TriageCandidate,
    TriageFailure,
    TriageValidation,
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


def _one_bad_cause(cause: str) -> tuple[BuiltRequest, str]:
    return built_for(), reply(("i1", "p1", cause), ("i2", "p2", "Weak password hashing"))


@pytest.mark.parametrize(
    "cause",
    [
        "",
        "   ",
        "x" * 61,
        " " + "x" * 61,
        "a\nb",
        "a\rb",
        "a b",
        "a\u0007b",
        "a\tb",
        "x · y",
        "see http x",
        "HTTP x",
        "www x",
        "WWW.x",
        "t.me x",
        "T.ME/x",
        "@team",
        "<b>x",
        "a > b",
        "3 findings",
        "level 2",
        "٣",
        "१ issue",
    ],
)
def test_cause_rejected_alone(cause: str) -> None:
    built, text = _one_bad_cause(cause)
    result = validate_reply(built, text)
    assert result.failure is None
    assert set(result.accepted) == {built.ids["i2"]}
    assert result.rejected == {TriageFailure.ITEM_REJECTED: 1}


@pytest.mark.parametrize(
    ("cause", "stored"),
    [
        ("x" * 60, "x" * 60),
        ("  padded cause  ", "padded cause"),
        ("Внедрение SQL в поиске заказов", "Внедрение SQL в поиске заказов"),
        ("В" * 60, "В" * 60),
        ("tab-free, plain: ok!", "tab-free, plain: ok!"),
    ],
)
def test_cause_accepted_and_stored_trimmed(cause: str, stored: str) -> None:
    built, text = _one_bad_cause(cause)
    result = validate_reply(built, text)
    assert result.rejected == {}
    assert result.accepted[built.ids["i1"]].cause == stored


def test_cause_both_rejected_counts_two() -> None:
    built = built_for()
    result = validate_reply(built, reply(("i1", "p1", ""), ("i2", "p1", "@x")))
    assert result.accepted == {}
    assert result.failure is None
    assert result.rejected == {TriageFailure.ITEM_REJECTED: 2}


def test_missing_item_stays_rule_only() -> None:
    built = built_for()
    result = validate_reply(built, reply(("i1", "p1", "SQL injection in order search")))
    assert set(result.accepted) == {built.ids["i1"]}
    assert result.failure is None
    assert result.rejected == {}


def test_missing_all_items_is_empty_without_failure() -> None:
    result = validate_reply(built_for(), '{"items":[]}')
    assert result.accepted == {}
    assert result.failure is None
    assert result.rejected == {}


def test_never_raises_on_random_text() -> None:
    rng = random.Random(42)
    built = built_for()
    for _ in range(200):
        text = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200))).decode(
            "utf-8", "replace"
        )
        result = validate_reply(built, text)
        assert isinstance(result, TriageValidation)
        assert result.failure is not None
    assert validate_reply(built, "\ud800").failure is TriageFailure.NOT_JSON


def test_never_raises_on_one_megabyte() -> None:
    result = validate_reply(built_for(), "a" * 1_000_000)
    assert result.failure is TriageFailure.NOT_JSON


def test_never_raises_on_deep_nesting_objects() -> None:
    result = validate_reply(built_for(), '{"items":' * 20000 + "1")
    assert result.failure is TriageFailure.NOT_JSON


def _triage_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "enji_guard_radar.triage"]


def test_log_counts_without_cause_text(caplog: pytest.LogCaptureFixture) -> None:
    built = built_for()
    text = reply(("i1", "p1", "SECRETCAUSE 7"), ("i2", "p2", "Weak password hashing"))
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.triage"):
        validate_reply(built, text)
    records = _triage_records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()
    assert records[0].levelno == logging.INFO
    assert "accepted=1 of 2" in message
    assert "item_rejected=1" in message
    assert "SECRETCAUSE" not in message
    assert "Weak password" not in message
    assert "Broken access" not in message


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("nope", "not_json"),
        ('{"items":[],"summary":"x"}', "schema_invalid"),
        (reply(("i9", "p1", "LEAKME")), "unknown_or_duplicate_ids"),
    ],
)
def test_log_names_whole_reply_reason(
    caplog: pytest.LogCaptureFixture, text: str, reason: str
) -> None:
    with caplog.at_level(logging.INFO, logger="enji_guard_radar.triage"):
        validate_reply(built_for(), text)
    records = _triage_records(caplog)
    assert len(records) == 1
    assert reason in records[0].getMessage()
    assert "LEAKME" not in records[0].getMessage()
