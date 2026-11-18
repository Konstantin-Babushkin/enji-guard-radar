from datetime import UTC, datetime, timedelta, timezone

import pytest

from enji_guard_radar.digest import DigestInput, build_digest, render_digest
from enji_guard_radar.ledger import ResolvedItem
from enji_guard_radar.messages import TELEGRAM_MAX_CHARS, telegram_length
from enji_guard_radar.model import Alert, AlertKey, AlertKind, Audience, Severity
from enji_guard_radar.score_rules import LowScore
from enji_guard_radar.tooling_rules import ToolingGap

POLL = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
TITLE = "<b>Guard radar weekly digest</b>"
POLL_LINE = "Last successful Guard poll: 2026-11-18 06:00 UTC."


def high(
    name: str = "Acme",
    *,
    pid: str = "p1",
    repo_id: str = "r1",
    action: str = "audit.security",
    count: int | None = 3,
    age: int | None = 16,
    repo: str | None = "acme/svc",
    seen_since: datetime | None = datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
    kind: AlertKind = AlertKind.HIGH_FINDINGS,
) -> Alert:
    return Alert(
        key=AlertKey(project_id=pid, repo_id=repo_id, action_key=action, level="high"),
        kind=kind,
        project_name=name,
        severity=Severity.HIGH,
        age_days=age,
        last_run_at=None,
        repo_count=1,
        count=count,
        repo_name=repo,
        seen_since=seen_since,
    )


def stale(
    name: str = "Beta",
    *,
    pid: str = "p2",
    age: int = 20,
    last: datetime = datetime(2026, 10, 29, 12, 0, tzinfo=UTC),
) -> Alert:
    return Alert(
        key=AlertKey(project_id=pid, level="stale"),
        kind=AlertKind.STALE,
        project_name=name,
        severity=Severity.MEDIUM,
        age_days=age,
        last_run_at=last,
        repo_count=1,
    )


def never_ran(name: str = "Nova", *, pid: str = "p9") -> Alert:
    return Alert(
        key=AlertKey(project_id=pid, level="never_ran"),
        kind=AlertKind.NEVER_RAN,
        project_name=name,
        severity=Severity.MEDIUM,
        age_days=None,
        last_run_at=None,
        repo_count=1,
    )


def low(
    name: str = "Gamma",
    *,
    pid: str = "p3",
    repo_id: str = "r3",
    repo: str = "acme/web",
    action: str = "ai-readiness",
    score: int = 40,
    run: datetime = datetime(2026, 11, 10, 8, 0, tzinfo=UTC),
) -> LowScore:
    return LowScore(
        project_id=pid,
        project_name=name,
        repo_id=repo_id,
        repo_name=repo,
        action_key=action,
        score=score,
        threshold=60,
        completed_at=run,
    )


def gap(
    name: str = "Delta",
    *,
    pid: str = "p4",
    repo_id: str = "r4",
    repo: str = "acme/lib",
    action: str = "dead-code",
    last: datetime | None = None,
) -> ToolingGap:
    return ToolingGap(
        project_id=pid,
        project_name=name,
        repo_id=repo_id,
        repo_name=repo,
        action_key=action,
        last_completed_at=last,
        age_days=None if last is None else (POLL - last).days,
        window_days=60,
    )


def resolved(
    level: str = "high",
    *,
    pid: str = "p1",
    action: str = "audit.security",
    at: datetime = POLL - timedelta(days=2),
) -> ResolvedItem:
    repo_id = "r1" if level == "high" else "*"
    action_key = action if level == "high" else "*"
    return ResolvedItem(
        key=AlertKey(project_id=pid, repo_id=repo_id, action_key=action_key, level=level),
        resolved_at=at,
    )


def text_of(**kwargs: object) -> str:
    kwargs.setdefault("last_poll_at", POLL)
    kwargs.setdefault("max_items", 10)
    messages = render_digest(build_digest(DigestInput(**kwargs)))
    assert len(messages) == 1
    return messages[0].text


def test_mixed_digest_text() -> None:
    inp = DigestInput(
        last_poll_at=POLL,
        max_items=10,
        high=(high(),),
        recurring_runs={("p1", "r1", "audit.security"): 4},
        stale=(stale(),),
        low_scores=(low(),),
        tooling_gaps=(gap(),),
        resolved=(resolved("high"), resolved("stale", pid="p2", at=POLL - timedelta(days=1))),
        project_names={"p1": "Acme", "p2": "Beta"},
    )
    messages = render_digest(build_digest(inp))
    assert len(messages) == 1
    message = messages[0]
    assert message.text.split("\n") == [
        TITLE,
        POLL_LINE,
        "Open items: 4.",
        "- High: <b>Acme</b>: 3 High findings in audit.security (acme/svc). "
        "Seen by the radar since 2026-11-02 (16 days). "
        "Recurring: High findings in each of the last 4 runs.",
        "- Stale: <b>Beta</b>: no completed Guard run for 20 days (last: 2026-10-29).",
        "- Low score: <b>Gamma</b>: ai-readiness score 40/100 in acme/web (run 2026-11-10).",
        "- Tooling gap: <b>Delta</b>: no completed dead-code run in the last 60 days "
        "in acme/lib (none seen).",
        "Resolved since the last digest (2):",
        "- <b>Acme</b>: High findings in audit.security are closed.",
        "- <b>Beta</b>: no longer stale.",
    ]
    assert telegram_length(message.text) < TELEGRAM_MAX_CHARS
    assert message.keys == ()
    assert message.audience is Audience.GROUP


def lines_of(**kwargs: object) -> list[str]:
    return text_of(**kwargs).split("\n")


def item_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.startswith("- ") and ": <b>" in line[:20]]


def test_tier_order_is_worst_first() -> None:
    lines = lines_of(
        tooling_gaps=(gap(),),
        low_scores=(low(),),
        stale=(stale(),),
        high=(high(),),
    )
    kinds = [line.split(":")[0] for line in lines if line.startswith("- ")]
    assert kinds == ["- High", "- Stale", "- Low score", "- Tooling gap"]


def test_sort_high_count_then_age_then_name() -> None:
    alerts = (
        high("Old three", pid="a", count=3, age=30),
        high("New three", pid="b", count=3, age=10),
        high("Five", pid="c", count=5, age=1),
        high("alpha three", pid="d", count=3, age=10),
    )
    lines = item_lines(text_of(high=alerts))
    names = [line.split("<b>")[1].split("</b>")[0] for line in lines]
    assert names == ["Five", "Old three", "alpha three", "New three"]


def test_sort_stale_age_then_never_ran_last() -> None:
    alerts = (
        stale("Twenty", pid="a", age=20),
        never_ran("Never"),
        stale("Thirty", pid="b", age=30),
    )
    names = [line.split("<b>")[1].split("</b>")[0] for line in item_lines(text_of(stale=alerts))]
    assert names == ["Thirty", "Twenty", "Never"]
    assert "- Stale: <b>Never</b>: no completed Guard run found." in text_of(stale=alerts)


def test_sort_low_score_and_tooling_gap() -> None:
    lows = (
        low("Newer", pid="a", run=datetime(2026, 11, 12, tzinfo=UTC), score=10),
        low("Lower", pid="b", run=datetime(2026, 11, 10, tzinfo=UTC), score=20),
        low("Older high score", pid="c", run=datetime(2026, 11, 10, tzinfo=UTC), score=50),
        low("Oldest", pid="d", run=datetime(2026, 11, 1, tzinfo=UTC), score=55),
    )
    names = [line.split("<b>")[1].split("</b>")[0] for line in item_lines(text_of(low_scores=lows))]
    assert names == ["Oldest", "Lower", "Older high score", "Newer"]
    gaps = (
        gap("Recent", pid="a", last=datetime(2026, 10, 1, tzinfo=UTC)),
        gap("Never", pid="b", last=None),
        gap("Older", pid="c", last=datetime(2026, 8, 1, tzinfo=UTC)),
    )
    text = text_of(tooling_gaps=gaps)
    names = [line.split("<b>")[1].split("</b>")[0] for line in item_lines(text)]
    assert names == ["Never", "Older", "Recent"]
    assert "(last run: 2026-08-01)." in text


def test_sort_is_independent_of_input_order() -> None:
    highs = tuple(high(f"P{i}", pid=f"p{i}", count=1 + i % 3, age=i) for i in range(6))
    stales = (*(stale(f"S{i}", pid=f"s{i}", age=5 + i) for i in range(4)), never_ran())
    expected = text_of(high=highs, stale=stales)
    assert text_of(high=highs[::-1], stale=stales[::-1]) == expected
    assert text_of(high=highs[3:] + highs[:3], stale=stales[2:] + stales[:2]) == expected


def test_recurring_marker_once_inside_high_tier() -> None:
    marked = high("Marked", pid="a")
    plain = high("Plain", pid="b", count=2)
    text = text_of(high=(marked, plain), recurring_runs={("a", "r1", "audit.security"): 3})
    assert text.count("Recurring: High findings in each of the last 3 runs.") == 1
    marked_line = next(line for line in item_lines(text) if "Marked" in line)
    assert marked_line.startswith("- High: ")
    assert marked_line.endswith("Recurring: High findings in each of the last 3 runs.")
    assert "Recurring" not in next(line for line in item_lines(text) if "Plain" in line)
    assert text.count("Marked") == 1
    assert "Open items: 2." in text
    assert "- Recurring" not in text


def test_same_project_stale_and_high_appear_in_both_tiers_once_each() -> None:
    both = text_of(high=(high("Acme", pid="p1"),), stale=(stale("Acme", pid="p1"),))
    assert len(item_lines(both)) == 2
    duplicate = text_of(high=(high(), high()), stale=(stale(), stale()))
    assert len(item_lines(duplicate)) == 2
    assert "Open items: 2." in duplicate


def test_cap_hidden_count_and_total() -> None:
    stales = tuple(stale(f"S{i:02d}", pid=f"s{i}", age=30 - i) for i in range(12))
    text = text_of(stale=stales, max_items=10)
    assert len(item_lines(text)) == 10
    assert "Open items: 12." in text
    assert text.split("\n")[-1] == "+2 more open items not shown."
    assert "S10" not in text
    one = text_of(stale=stales[:11], max_items=10)
    assert "+1 more open item not shown." in one
    assert "+1 more open items" not in one
    few = text_of(stale=stales[:3], max_items=10)
    assert "more open" not in few
    assert len(item_lines(few)) == 3
    exact = text_of(stale=stales[:10], max_items=10)
    assert "more open" not in exact


def test_cap_cuts_lower_tiers_first() -> None:
    text = text_of(high=(high(),), stale=(stale(),), low_scores=(low(),), max_items=1)
    assert "- High:" in text
    assert "- Stale:" not in text
    assert "+2 more open items not shown." in text


def test_all_clear_is_one_line() -> None:
    assert text_of() == f"{TITLE}: nothing open. {POLL_LINE}"


def test_all_clear_with_unread_projects() -> None:
    assert lines_of(unread_projects=2) == [
        f"{TITLE}: nothing open in the projects read. {POLL_LINE}",
        "2 projects could not be read in that poll.",
    ]


def test_all_clear_followed_by_resolved_block() -> None:
    lines = lines_of(resolved=(resolved("never_ran", pid="p5"),), project_names={"p5": "Nova"})
    assert lines == [
        f"{TITLE}: nothing open. {POLL_LINE}",
        "Resolved since the last digest (1):",
        "- <b>Nova</b>: now has completed Guard runs.",
    ]


def test_unread_note_sits_between_count_and_items() -> None:
    lines = lines_of(stale=(stale(),), unread_projects=1)
    assert lines[2] == "Open items: 1."
    assert lines[3] == "1 project could not be read in that poll."
    assert lines[4].startswith("- Stale:")


def test_resolved_caps_at_five_lines_with_more_line() -> None:
    items = tuple(
        resolved("stale", pid=f"p{i}", at=POLL - timedelta(days=10 - i)) for i in range(8)
    )
    names = {f"p{i}": f"Proj{i}" for i in range(8)}
    lines = lines_of(resolved=items, project_names=names)
    assert lines[1] == "Resolved since the last digest (8):"
    assert lines[2:7] == [f"- <b>Proj{i}</b>: no longer stale." for i in range(5)]
    assert lines[7] == "+3 more resolved."
    assert len(lines) == 8


def test_resolved_levels_and_unnamed_fallback() -> None:
    items = (
        resolved("high", pid="p1", action="audit.security", at=POLL - timedelta(days=3)),
        resolved("stale", pid="p2", at=POLL - timedelta(days=2)),
        resolved("never_ran", pid="p3", at=POLL - timedelta(days=1)),
        resolved("mystery", pid="p1", at=POLL),
    )
    lines = lines_of(resolved=items, project_names={"p1": "Acme", "p2": "Beta"})
    assert lines[1:] == [
        "Resolved since the last digest (4):",
        "- <b>Acme</b>: High findings in audit.security are closed.",
        "- <b>Beta</b>: no longer stale.",
        "- <b>(unnamed project)</b>: now has completed Guard runs.",
        "- <b>Acme</b>: no longer open.",
    ]
    assert "p3" not in "\n".join(lines)


def test_resolved_order_is_stable_for_equal_times() -> None:
    items = tuple(resolved("stale", pid=f"p{i}", at=POLL) for i in (3, 1, 2))
    names = {"p1": "One", "p2": "Two", "p3": "Three"}
    forward = text_of(resolved=items, project_names=names)
    assert text_of(resolved=items[::-1], project_names=names) == forward
    assert forward.index("One") < forward.index("Two") < forward.index("Three")


def test_escaping_of_names_and_repos() -> None:
    nasty = '<b>x</b> & "q" <script>alert(1)</script>'
    text = text_of(
        high=(high(nasty, repo="a<i>/b&c"),),
        stale=(stale(nasty, pid="p7"),),
        low_scores=(low(nasty, repo="<u>r</u>"),),
        tooling_gaps=(gap(nasty, repo="<s>t</s>"),),
        resolved=(resolved("stale", pid="p8"),),
        project_names={"p8": nasty},
    )
    assert "<script" not in text
    assert "&lt;script&gt;" in text
    assert "a&lt;i&gt;/b&amp;c" in text
    assert "&lt;u&gt;r&lt;/u&gt;" in text
    assert "<i>" not in text
    assert "<s>" not in text


def test_escaping_cuts_long_names() -> None:
    text = text_of(stale=(stale("N" * 200),))
    line = item_lines(text)[0]
    assert "N" * 80 not in line
    assert "N" * 77 + "..." in line


def test_counts_only_no_ids_paths_or_placeholders() -> None:
    text = text_of(
        high=(high(),),
        stale=(stale(),),
        low_scores=(low(),),
        tooling_gaps=(gap(),),
        resolved=(resolved(),),
    )
    assert "F-001" not in text
    assert "src/" not in text
    assert "Placeholder" not in text
    assert "p1" not in text.replace("<b>", "")


def test_utc_poll_time_and_dates() -> None:
    plus_six = datetime(2026, 11, 18, 6, 0, tzinfo=UTC).astimezone(timezone(timedelta(hours=6)))
    text = text_of(last_poll_at=plus_six.replace(hour=1, minute=30))
    assert "Last successful Guard poll: 2026-11-17 19:30 UTC." in text
    late = datetime(2026, 10, 29, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert "(last: 2026-10-30)" in text_of(stale=(stale(last=late),))


def test_utc_rejects_naive_poll_time() -> None:
    with pytest.raises(ValueError, match="timezone"):
        DigestInput(last_poll_at=datetime(2026, 11, 18, 6, 0), max_items=10)  # noqa: DTZ001


def test_split_into_numbered_messages_at_item_boundaries() -> None:
    alerts = tuple(high("N" * 80, pid=f"p{i:02d}", count=100 - i, repo="R" * 80) for i in range(50))
    digest = build_digest(DigestInput(last_poll_at=POLL, max_items=50, high=alerts))
    messages = render_digest(digest)
    assert len(messages) > 1
    total = len(messages)
    for number, message in enumerate(messages, start=1):
        assert telegram_length(message.text) <= 4000
        first, second = message.text.split("\n")[:2]
        assert first == f"<b>Guard radar weekly digest ({number}/{total})</b>"
        assert second == POLL_LINE
    sent = [
        line for message in messages for line in message.text.split("\n") if line.startswith("- ")
    ]
    assert sent == [item.line for item in digest.items]
    assert len(set(sent)) == 50


def test_split_leaves_single_message_unnumbered() -> None:
    messages = render_digest(build_digest(DigestInput(last_poll_at=POLL, max_items=3)))
    assert len(messages) == 1
    assert "(1/1)" not in messages[0].text


def test_invalid_inputs_are_rejected() -> None:
    bad_high = (
        high(count=0),
        high(kind=AlertKind.STALE),
        high(repo=None),
        high(seen_since=None),
        high(age=None),
    )
    for alert in bad_high:
        with pytest.raises(ValueError, match="digest high alerts"):
            build_digest(DigestInput(last_poll_at=POLL, max_items=3, high=(alert,)))
    with pytest.raises(ValueError, match="STALE or NEVER_RAN"):
        build_digest(DigestInput(last_poll_at=POLL, max_items=3, stale=(high(),)))
    with pytest.raises(ValueError, match="stale digest alert"):
        build_digest(
            DigestInput(
                last_poll_at=POLL,
                max_items=3,
                stale=(stale(age=0).model_copy(update={"last_run_at": None}),),
            )
        )
    for bad in (0, -1):
        with pytest.raises(ValueError, match="max_items"):
            DigestInput(last_poll_at=POLL, max_items=bad)
    with pytest.raises(ValueError, match="unread_projects"):
        DigestInput(last_poll_at=POLL, max_items=3, unread_projects=-1)


def test_invalid_max_chars_is_rejected() -> None:
    digest = build_digest(DigestInput(last_poll_at=POLL, max_items=3))
    for bad in (999, 4097):
        with pytest.raises(ValueError, match="max_chars"):
            render_digest(digest, max_chars=bad)
