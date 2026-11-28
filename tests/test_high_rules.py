"""High alert rule: series facts in, counts-only alert and text out (RULE-02, RULE-03, ALERT-01)."""

from datetime import UTC, datetime, timedelta

import pytest

from enji_guard_radar.config import parse_config
from enji_guard_radar.high_rules import (
    HIGH_LEVEL,
    HighSeries,
    high_alert,
    is_high_priority,
    is_tracked,
)
from enji_guard_radar.messages import render_alert, render_messages
from enji_guard_radar.model import Alert, AlertKey, AlertKind, Severity

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
OPENED = datetime(2026, 11, 9, 23, 44, 19, tzinfo=UTC)


def series(
    *,
    high_now: int = 3,
    previous_high: int | None = 1,
    opened_at: datetime = OPENED,
    name: str = "Project A",
    repo: str = "acme/service-a",
    action_key: str = "audit.security",
) -> HighSeries:
    return HighSeries(
        project_id="p1",
        project_name=name,
        repo_id="r1",
        repo_name=repo,
        action_key=action_key,
        high_now=high_now,
        previous_high=previous_high,
        opened_at=opened_at,
        repo_count=2,
    )


def lines(alert: Alert) -> list[str]:
    return render_alert(alert).splitlines()


def test_config_to_high_rise_message() -> None:
    config = parse_config(
        "high: {clear_after_clean_runs: 3}\ndefaults: {high_aged_after_days: 30}\n",
        source="x.yaml",
    )
    alert = high_alert(
        series(name='Project A <core> & "tools"'),
        aged_after_days=config.thresholds_for("p1").high_aged_after_days,
        now=NOW,
    )
    messages = render_messages([alert], collapse_above=config.alerts.collapse_above)
    assert len(messages) == 1
    assert messages[0].keys == (
        AlertKey(project_id="p1", repo_id="r1", action_key="audit.security", level="high"),
    )
    assert messages[0].text == (
        '🔴 <b>Project A &lt;core&gt; &amp; "tools"</b>: high problems 3 (was 1), '
        "in audit security · open for 11 days"
    )
    assert config.high.clear_after_clean_runs == 3


@pytest.mark.parametrize(
    ("high_now", "previous", "expected"),
    [
        (3, None, "high problems 3"),
        (2, 0, "high problems 2"),
        (1, 1, "high problems 1 (was 1)"),
        (4, 4, "high problems 4 (was 4)"),
        (1, 3, "high problems 1 (was 3)"),
        (0, 2, "high problems 0 (was 2)"),
        (0, None, "high problems 0"),
    ],
)
def test_count_line(high_now: int, previous: int | None, expected: str) -> None:
    alert = high_alert(
        series(high_now=high_now, previous_high=previous), aged_after_days=14, now=NOW
    )
    assert lines(alert) == [
        f"🔴 <b>Project A</b>: {expected}, in audit security · open for 11 days"
    ]


def test_aged_boundary_exactly_threshold_is_not_aged() -> None:
    alert = high_alert(series(opened_at=NOW - timedelta(days=14)), aged_after_days=14, now=NOW)
    assert alert.aged is False
    assert alert.age_days == 14
    assert lines(alert) == [
        "🔴 <b>Project A</b>: high problems 3 (was 1), in audit security · open for 14 days"
    ]


def test_aged_boundary_one_second_over_is_aged() -> None:
    alert = high_alert(
        series(opened_at=NOW - timedelta(days=14, seconds=1)), aged_after_days=14, now=NOW
    )
    assert alert.aged is True
    assert alert.age_days == 14
    # an aged series reads like any other: the flag does not change the text
    assert lines(alert) == [
        "🔴 <b>Project A</b>: high problems 3 (was 1), in audit security · open for 14 days"
    ]
    assert render_alert(alert) == render_alert(alert.model_copy(update={"aged": False}))


def test_age_in_whole_days() -> None:
    alert = high_alert(
        series(opened_at=NOW - timedelta(days=1, hours=5)), aged_after_days=14, now=NOW
    )
    assert alert.age_days == 1
    assert lines(alert)[0].endswith("· open for 1 day")


def test_opened_after_now_is_age_zero_and_not_aged() -> None:
    alert = high_alert(series(opened_at=NOW + timedelta(hours=3)), aged_after_days=1, now=NOW)
    assert alert.age_days == 0
    assert alert.aged is False


def test_key_ignores_count_age_and_aged_flag() -> None:
    young = high_alert(series(high_now=2, previous_high=None), aged_after_days=14, now=NOW)
    old = high_alert(
        series(high_now=9, previous_high=2, opened_at=NOW - timedelta(days=90)),
        aged_after_days=14,
        now=NOW,
    )
    assert young.aged is False
    assert old.aged is True
    assert young.key == old.key
    assert young.key == AlertKey(
        project_id="p1", repo_id="r1", action_key="audit.security", level=HIGH_LEVEL
    )
    assert HIGH_LEVEL == "high"


def test_alert_fields() -> None:
    alert = high_alert(series(), aged_after_days=14, now=NOW)
    assert alert.kind is AlertKind.HIGH_FINDINGS
    assert alert.severity is Severity.HIGH
    assert alert.count == 3
    assert alert.previous_count == 1
    assert alert.repo_name == "acme/service-a"
    assert alert.seen_since == OPENED
    assert alert.last_run_at is None


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        high_alert(series(), aged_after_days=14, now=datetime(2026, 11, 21, 12, 0))  # noqa: DTZ001


@pytest.mark.parametrize(
    ("action_key", "tracked"),
    [("audit.recon", False), ("audit.tests", True), ("audit.security", True)],
)
def test_is_tracked(action_key: str, tracked: bool) -> None:
    assert is_tracked(action_key) is tracked


@pytest.mark.parametrize(
    ("action_key", "high_types", "expected"),
    [
        ("audit.recon", None, False),
        ("audit.tests", None, True),
        ("audit.security", None, True),
        ("audit.recon", ("audit.recon",), False),
        ("audit.security", ("audit.security",), True),
        # Outside the list: still tracked, but not high priority.
        ("audit.tests", ("audit.security",), False),
        (
            "audit.public-api-access-safety",
            ("audit.security", "audit.public-api-access-safety"),
            True,
        ),
    ],
)
def test_is_high_priority(
    action_key: str, high_types: tuple[str, ...] | None, expected: bool
) -> None:
    assert is_high_priority(action_key, high_types) is expected
    if high_types is not None and action_key != "audit.recon":
        assert is_tracked(action_key) is True


def test_dynamic_text_is_escaped_and_cleaned() -> None:
    long_name = "Line one\nline two " + "x" * 120
    alert = high_alert(
        series(name=long_name, repo="acme/<script>", action_key="audit.x&y"),
        aged_after_days=14,
        now=NOW,
    )
    text = render_alert(alert)
    assert "<script>" not in text
    assert "acme" not in text
    assert "x&amp;y" in text
    assert "\nline two" not in text
    assert "...</b>" in text
    assert len(text.splitlines()) == 1
    (message,) = render_messages([alert], collapse_above=3)
    assert "<script>" not in message.text
    assert "acme" not in message.text
    assert "\n" not in message.text


def test_alert_model_fields_carry_no_evidence() -> None:
    forbidden = {"title", "titles", "finding_id", "finding_ids", "evidence", "path", "paths"}
    assert forbidden.isdisjoint(Alert.model_fields)
    assert forbidden.isdisjoint(HighSeries.model_fields)


def test_project_line_first_sight_and_rise() -> None:
    first = high_alert(series(previous_high=None), aged_after_days=14, now=NOW)
    rise = high_alert(series(), aged_after_days=14, now=NOW)
    assert render_messages([first], collapse_above=3)[0].text == (
        "🔴 <b>Project A</b>: high problems 3, in audit security · open for 11 days"
    )
    assert render_messages([rise], collapse_above=3)[0].text == (
        "🔴 <b>Project A</b>: high problems 3 (was 1), in audit security · open for 11 days"
    )


def test_collapse_puts_high_first() -> None:
    high = high_alert(series(), aged_after_days=14, now=NOW)
    stale = [
        Alert(
            key=AlertKey(project_id=f"s{i}", level="stale"),
            kind=AlertKind.STALE,
            project_name=f"Stale {i}",
            severity=Severity.MEDIUM,
            age_days=30 + i,
            last_run_at=NOW - timedelta(days=30 + i),
            repo_count=1,
        )
        for i in range(3)
    ]
    messages = render_messages([*stale, high], collapse_above=3)
    assert len(messages) == 1
    body = messages[0].text.splitlines()
    assert body[0] == "Radar · problems: 4 projects"
    assert (
        body[1]
        == "🔴 <b>Project A</b>: high problems 3 (was 1), in audit security · open for 11 days"
    )
    assert messages[0].keys[0] == high.key


def test_high_alert_without_repo_name_is_rejected_in_a_single_line() -> None:
    alert = high_alert(series(), aged_after_days=14, now=NOW).model_copy(update={"repo_name": None})
    # No line shows a repo, so no line needs the name.
    assert render_alert(alert).startswith("🔴 <b>Project A</b>")
    assert render_messages([alert], collapse_above=3)[0].text.startswith("🔴 <b>Project A</b>")


@pytest.mark.parametrize("missing", ["count", "seen_since", "age_days"])
def test_high_alert_missing_fact_is_rejected(missing: str) -> None:
    alert = high_alert(series(), aged_after_days=14, now=NOW).model_copy(update={missing: None})
    with pytest.raises(ValueError, match=missing):
        render_alert(alert)
