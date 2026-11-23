from datetime import UTC, datetime, timedelta, timezone

import pytest

from enji_guard_radar.digest import DigestInput, build_digest, render_digest
from enji_guard_radar.high_rules import LowSeries
from enji_guard_radar.ledger import ResolvedItem
from enji_guard_radar.messages import TELEGRAM_MAX_CHARS, telegram_length
from enji_guard_radar.model import Alert, AlertKey, AlertKind, Audience, Severity
from enji_guard_radar.score_rules import LowScore
from enji_guard_radar.texts import Language
from enji_guard_radar.tooling_rules import ToolingGap

POLL = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
POLL_LINE = "Last poll: 2026-11-18 06:00 UTC"
FOOTER = "Ages count from the radar's first sight."
CLEAR = "<b>Radar weekly</b> · nothing open · last poll 2026-11-18 06:00 UTC"


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
    action: str = "audit.dead-code",
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


def text_of(language: Language = "en", **kwargs: object) -> str:
    kwargs.setdefault("last_poll_at", POLL)
    kwargs.setdefault("max_items", 10)
    messages = render_digest(build_digest(DigestInput(**kwargs), language=language))
    assert len(messages) == 1
    return messages[0].text


def lines_of(language: Language = "en", **kwargs: object) -> list[str]:
    return text_of(language, **kwargs).split("\n")


def item_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line.startswith(("🔴", "🟡"))]


def names_of(text: str) -> list[str]:
    return [line.split("<b>")[1].split("</b>")[0] for line in item_lines(text)]


def test_short_digest_text() -> None:
    poll = datetime(2026, 11, 19, 5, 0, tzinfo=UTC)
    inp = DigestInput(
        last_poll_at=poll,
        max_items=10,
        high=(
            high(
                "Project A",
                count=2,
                age=8,
                seen_since=datetime(2026, 11, 11, 5, 0, tzinfo=UTC),
            ),
        ),
        recurring_runs={("p1", "r1", "audit.security"): 3},
        stale=(stale("Project B", pid="p2", age=28),),
        tooling_gaps=(
            gap("Project C", pid="p3", repo_id="r9", repo="repo-x", action="audit.dead-code"),
            gap(
                "Project C",
                pid="p3",
                repo_id="r9",
                repo="repo-x",
                action="audit.security",
                last=datetime(2026, 5, 1, tzinfo=UTC),
            ),
        ),
    )
    messages = render_digest(build_digest(inp))
    assert len(messages) == 1
    message = messages[0]
    assert message.text == (
        "<b>Radar weekly · 3 open</b>\n"
        "Last poll: 2026-11-19 05:00 UTC\n"
        "🔴 <b>Project A</b> · security High 2 · 8d · recurring 3 runs\n"
        "🟡 <b>Project B</b> · no scan 28d\n"
        "🟡 <b>Project C</b> · repo-x · no runs 60d: dead-code, security\n"
        "Ages count from the radar's first sight."
    )
    assert telegram_length(message.text) < TELEGRAM_MAX_CHARS
    assert message.keys == ()
    assert message.audience is Audience.GROUP


def test_mixed_digest_has_one_line_per_item_in_tier_order() -> None:
    lines = lines_of(
        high=(high("Acme"),),
        stale=(stale("Beta"),),
        low_scores=(low("Gamma"),),
        tooling_gaps=(gap("Delta"),),
    )
    assert lines == [
        "<b>Radar weekly · 4 open</b>",
        POLL_LINE,
        "🔴 <b>Acme</b> · security High 3 · 16d",
        "🟡 <b>Beta</b> · no scan 20d",
        "🟡 <b>Gamma</b> · ai-readiness 40/100 · 8d",
        "🟡 <b>Delta</b> · acme/lib · no runs 60d: dead-code",
        FOOTER,
    ]


def test_tier_order_is_worst_first() -> None:
    text = text_of(
        tooling_gaps=(gap(),),
        low_scores=(low(),),
        stale=(stale(),),
        high=(high(),),
    )
    assert names_of(text) == ["Acme", "Beta", "Gamma", "Delta"]


def test_sort_high_count_then_age_then_name() -> None:
    alerts = (
        high("Old three", pid="a", count=3, age=30),
        high("New three", pid="b", count=3, age=10),
        high("Five", pid="c", count=5, age=1),
        high("alpha three", pid="d", count=3, age=10),
    )
    assert names_of(text_of(high=alerts)) == ["Five", "Old three", "alpha three", "New three"]


def test_sort_stale_age_then_never_ran_last() -> None:
    alerts = (
        stale("Twenty", pid="a", age=20),
        never_ran("Never"),
        stale("Thirty", pid="b", age=30),
    )
    text = text_of(stale=alerts)
    assert names_of(text) == ["Thirty", "Twenty", "Never"]
    assert "🟡 <b>Never</b> · no scan yet" in text.split("\n")


def test_sort_low_score_and_tooling_gap() -> None:
    lows = (
        low("Newer", pid="a", run=datetime(2026, 11, 12, tzinfo=UTC), score=10),
        low("Lower", pid="b", run=datetime(2026, 11, 10, tzinfo=UTC), score=20),
        low("Older high score", pid="c", run=datetime(2026, 11, 10, tzinfo=UTC), score=50),
        low("Oldest", pid="d", run=datetime(2026, 11, 1, tzinfo=UTC), score=55),
    )
    assert names_of(text_of(low_scores=lows)) == ["Oldest", "Lower", "Older high score", "Newer"]
    gaps = (
        gap("Recent", pid="a", last=datetime(2026, 10, 1, tzinfo=UTC)),
        gap("Never", pid="b", last=None),
        gap("Older", pid="c", last=datetime(2026, 8, 1, tzinfo=UTC)),
    )
    assert names_of(text_of(tooling_gaps=gaps)) == ["Never", "Older", "Recent"]


def test_sort_is_independent_of_input_order() -> None:
    highs = tuple(high(f"P{i}", pid=f"p{i}", count=1 + i % 3, age=i) for i in range(6))
    stales = (*(stale(f"S{i}", pid=f"s{i}", age=5 + i) for i in range(4)), never_ran())
    gaps = tuple(gap(f"G{i}", pid=f"g{i}", repo_id=f"gr{i}") for i in range(3))
    expected = text_of(high=highs, stale=stales, tooling_gaps=gaps)
    assert text_of(high=highs[::-1], stale=stales[::-1], tooling_gaps=gaps[::-1]) == expected
    assert text_of(high=highs[3:] + highs[:3], stale=stales[2:] + stales[:2]) == text_of(
        high=highs, stale=stales
    )


def test_recurring_marker_once_inside_high_tier() -> None:
    marked = high("Marked", pid="a")
    plain = high("Plain", pid="b", count=2)
    text = text_of(high=(marked, plain), recurring_runs={("a", "r1", "audit.security"): 3})
    assert text.count("recurring 3 runs") == 1
    marked_line = next(line for line in item_lines(text) if "Marked" in line)
    assert marked_line.startswith("🔴 ")
    assert marked_line.endswith(" · recurring 3 runs")
    assert "recurring" not in next(line for line in item_lines(text) if "Plain" in line)
    assert text.count("Marked") == 1
    assert "<b>Radar weekly · 2 open</b>" in text


def test_high_line_shows_the_repo_only_when_two_repos_share_project_and_audit() -> None:
    one = text_of(high=(high(), high("Other", pid="p2", repo="acme/o")))
    assert "(acme" not in one
    two = text_of(
        high=(
            high(repo_id="r1", repo="acme/a"),
            high(repo_id="r2", repo="acme/b", count=2),
            high(repo_id="r3", repo="acme/c", action="audit.tests"),
        )
    )
    lines = item_lines(two)
    assert "security (acme/a) High 3" in lines[0]
    assert lines[1].endswith("tests High 3 · 16d")
    assert "security (acme/b) High 2" in lines[2]


def test_same_project_stale_and_high_appear_in_both_tiers_once_each() -> None:
    both = text_of(high=(high("Acme", pid="p1"),), stale=(stale("Acme", pid="p1"),))
    assert len(item_lines(both)) == 2
    duplicate = text_of(high=(high(), high()), stale=(stale(), stale()))
    assert len(item_lines(duplicate)) == 2
    assert "<b>Radar weekly · 2 open</b>" in duplicate


def test_group_four_gaps_of_one_repo_are_one_line_with_sorted_names() -> None:
    gaps = tuple(
        gap("Delta", action=f"audit.{name}")
        for name in ("security", "dead-code", "tests", "dependency-hygiene")
    )
    text = text_of(tooling_gaps=gaps)
    assert item_lines(text) == [
        "🟡 <b>Delta</b> · acme/lib · no runs 60d: dead-code, dependency-hygiene, security, tests"
    ]
    assert "<b>Radar weekly · 1 open</b>" in text
    assert "audit." not in text


def test_group_two_repos_of_one_project_are_two_lines() -> None:
    gaps = (
        gap("Delta", repo_id="r1", repo="acme/a", action="audit.tests"),
        gap("Delta", repo_id="r1", repo="acme/a", action="audit.security"),
        gap("Delta", repo_id="r2", repo="acme/b"),
    )
    text = text_of(tooling_gaps=gaps)
    assert item_lines(text) == [
        "🟡 <b>Delta</b> · acme/a · no runs 60d: security, tests",
        "🟡 <b>Delta</b> · acme/b · no runs 60d: dead-code",
    ]
    assert "<b>Radar weekly · 2 open</b>" in text


def test_group_order_key_is_the_oldest_member() -> None:
    gaps = (
        gap("Alpha", pid="a", repo_id="ra", last=datetime(2026, 10, 1, tzinfo=UTC)),
        gap("Beta", pid="b", repo_id="rb", last=datetime(2026, 9, 1, tzinfo=UTC)),
        gap(
            "Beta",
            pid="b",
            repo_id="rb",
            action="audit.tests",
            last=datetime(2026, 10, 9, tzinfo=UTC),
        ),
        gap("Zed", pid="z", repo_id="rz", last=datetime(2026, 11, 1, tzinfo=UTC)),
        gap("Zed", pid="z", repo_id="rz", action="audit.tests", last=None),
    )
    assert names_of(text_of(tooling_gaps=gaps)) == ["Zed", "Beta", "Alpha"]


def test_group_duplicate_gaps_collapse_and_window_comes_from_the_gap() -> None:
    gaps = (
        gap("Delta"),
        gap("Delta"),
        gap("Delta", action="audit.tests").model_copy(update={"window_days": 30}),
    )
    assert item_lines(text_of(tooling_gaps=gaps)) == [
        "🟡 <b>Delta</b> · acme/lib · no runs 30d: dead-code, tests"
    ]


def test_group_counts_as_one_item_for_the_cap() -> None:
    gaps = tuple(gap(f"G{i:02d}", pid=f"g{i}", repo_id=f"r{i}") for i in range(3))
    many = tuple(gap("Many", pid="m", repo_id="rm", action=f"audit.a{i}") for i in range(6))
    text = text_of(tooling_gaps=(*gaps, *many), max_items=3)
    assert len(item_lines(text)) == 3
    assert "<b>Radar weekly · 4 open</b>" in text
    assert text.split("\n")[-1] == "+1 more open item not shown."


def test_cap_hidden_count_and_total() -> None:
    stales = tuple(stale(f"S{i:02d}", pid=f"s{i}", age=30 - i) for i in range(12))
    text = text_of(stale=stales, max_items=10)
    assert len(item_lines(text)) == 10
    assert "<b>Radar weekly · 12 open</b>" in text
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
    assert names_of(text) == ["Acme"]
    assert "+2 more open items not shown." in text


def test_all_clear_is_one_line() -> None:
    assert text_of() == CLEAR


def test_all_clear_with_unread_projects() -> None:
    assert lines_of(unread_projects=2) == [
        "<b>Radar weekly</b> · nothing open in the projects read · last poll 2026-11-18 06:00 UTC",
        "2 projects could not be read in that poll.",
    ]


def test_all_clear_followed_by_resolved_block() -> None:
    lines = lines_of(resolved=(resolved("never_ran", pid="p5"),), project_names={"p5": "Nova"})
    assert lines == [CLEAR, "Resolved · 1", "<b>Nova</b> · no scan yet closed"]


def test_unread_note_sits_right_under_the_poll_line() -> None:
    lines = lines_of(stale=(stale(),), unread_projects=1)
    assert lines[0] == "<b>Radar weekly · 1 open</b>"
    assert lines[1] == POLL_LINE
    assert lines[2] == "1 project could not be read in that poll."
    assert lines[3].startswith("🟡")


def test_footer_only_when_a_high_line_is_shown() -> None:
    assert text_of(high=(high(),)).endswith(FOOTER)
    assert text_of(high=(high(),)).count(FOOTER) == 1
    assert FOOTER not in text_of(stale=(stale(),), low_scores=(low(),), tooling_gaps=(gap(),))
    assert FOOTER not in text_of()
    # High is the first tier, so a cap of 1 still shows it; without High there is no footer.
    assert build_digest(
        DigestInput(last_poll_at=POLL, max_items=1, high=(high(),), stale=(stale(),))
    ).has_high
    assert not build_digest(DigestInput(last_poll_at=POLL, max_items=3, stale=(stale(),))).has_high


def test_footer_ends_the_digest_after_the_resolved_block() -> None:
    lines = lines_of(
        high=(high(),),
        resolved=(resolved("stale", pid="p2"),),
        project_names={"p2": "Beta"},
    )
    assert lines[-3:] == ["Resolved · 1", "<b>Beta</b> · no scan closed", FOOTER]


def test_low_score_age_counts_utc_days_from_the_run_to_the_poll() -> None:
    run = datetime(2026, 11, 11, 23, 0, tzinfo=UTC)
    poll = datetime(2026, 11, 19, 1, 0, tzinfo=UTC)
    text = text_of(last_poll_at=poll, low_scores=(low("Project D", run=run, score=40),))
    assert item_lines(text) == ["🟡 <b>Project D</b> · ai-readiness 40/100 · 8d"]
    future = text_of(low_scores=(low(run=POLL + timedelta(days=2)),))
    assert item_lines(future)[0].endswith("· 0d")


def test_low_score_shows_the_repo_when_two_repos_share_project_and_audit() -> None:
    lows = (
        low("Gamma", repo_id="r1", repo="acme/a", score=30),
        low("Gamma", repo_id="r2", repo="acme/b", score=45),
        low("Gamma", repo_id="r3", repo="acme/c", action="audit.tests", score=10),
    )
    lines = item_lines(text_of(low_scores=lows))
    assert any("ai-readiness (acme/a) 30/100" in line for line in lines)
    assert any("ai-readiness (acme/b) 45/100" in line for line in lines)
    assert any("· tests 10/100 ·" in line for line in lines)


def test_resolved_caps_at_five_lines_with_more_line() -> None:
    items = tuple(
        resolved("stale", pid=f"p{i}", at=POLL - timedelta(days=10 - i)) for i in range(8)
    )
    names = {f"p{i}": f"Proj{i}" for i in range(8)}
    lines = lines_of(resolved=items, project_names=names)
    assert lines[1] == "Resolved · 8"
    assert lines[2:7] == [f"<b>Proj{i}</b> · no scan closed" for i in range(5)]
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
        "Resolved · 4",
        "<b>Acme</b> · security closed",
        "<b>Beta</b> · no scan closed",
        "<b>(unnamed project)</b> · no scan yet closed",
        "<b>Acme</b> · item closed",
    ]
    assert "p3" not in "\n".join(lines)


def test_resolved_order_is_stable_for_equal_times() -> None:
    items = tuple(resolved("stale", pid=f"p{i}", at=POLL) for i in (3, 1, 2))
    names = {"p1": "One", "p2": "Two", "p3": "Three"}
    forward = text_of(resolved=items, project_names=names)
    assert text_of(resolved=items[::-1], project_names=names) == forward
    assert forward.index("One") < forward.index("Two") < forward.index("Three")


def test_russian_digest_uses_the_russian_entries() -> None:
    text = text_of(
        "ru",
        high=(high("Acme"),),
        stale=(stale("Beta"), never_ran("Nova")),
        low_scores=(low("Gamma"),),
        tooling_gaps=(gap("Delta"),),
        resolved=(resolved("stale", pid="p2"),),
        project_names={"p2": "Beta"},
        unread_projects=1,
    )
    assert text.split("\n") == [
        "<b>Радар · дайджест · открыто: 5</b>",
        "Последний опрос: 2026-11-18 06:00 UTC",
        "В этом опросе не удалось прочитать: 1 проект.",
        "🔴 <b>Acme</b> · security высокие 3 · 16 д",
        "🟡 <b>Beta</b> · нет проверок 20 д",
        "🟡 <b>Nova</b> · проверок ещё не было",
        "🟡 <b>Gamma</b> · ai-readiness 40/100 · 8 д",
        "🟡 <b>Delta</b> · acme/lib · нет запусков 60 д: dead-code",
        "Закрыто · 1",
        "<b>Beta</b> · нет проверок: закрыто",
        "Возраст считается с первого появления в радаре.",
    ]
    clear = text_of("ru")
    assert (
        clear
        == "<b>Радар · дайджест</b> · ничего не открыто · последний опрос 2026-11-18 06:00 UTC"
    )


def test_russian_has_the_same_items_in_the_same_order() -> None:
    kwargs = {
        "high": (high("Acme"), high("Big", pid="p9", count=9)),
        "stale": (stale("Beta"),),
        "tooling_gaps": (gap("Delta"), gap("Delta", repo_id="r8", repo="acme/x")),
    }
    english = names_of(text_of("en", **kwargs))
    assert english == names_of(text_of("ru", **kwargs))


def test_no_cause_line_in_the_digest() -> None:
    text = text_of(
        high=(high(),),
        stale=(stale(),),
        low_scores=(low(),),
        tooling_gaps=(gap(),),
        resolved=(resolved(),),
    )
    assert "P1 ·" not in text
    assert not any(line.startswith("   ") for line in text.split("\n"))


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
    assert "&lt;s&gt;t&lt;/s&gt;" in text
    assert "<i>" not in text
    assert "<s>" not in text
    assert "<u>" not in text


def test_escaping_repo_names_when_the_repo_is_shown() -> None:
    text = text_of(
        high=(
            high(repo_id="r1", repo="a<i>/b&c"),
            high(repo_id="r2", repo="x"),
        ),
        low_scores=(
            low(repo_id="r1", repo="<u>r</u>"),
            low(repo_id="r2", repo="y"),
        ),
    )
    assert "a&lt;i&gt;/b&amp;c" in text
    assert "&lt;u&gt;r&lt;/u&gt;" in text
    assert "<i>" not in text
    assert "<u>" not in text


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
    poll = plus_six.replace(hour=1, minute=30)
    assert "Last poll: 2026-11-17 19:30 UTC" in text_of(last_poll_at=poll, stale=(stale(),))
    assert text_of(last_poll_at=poll).endswith("last poll 2026-11-17 19:30 UTC")
    late = datetime(2026, 11, 17, 23, 30, tzinfo=timezone(timedelta(hours=-5)))  # 11-18 04:30 UTC
    assert item_lines(text_of(low_scores=(low(run=late),)))[0].endswith("· 0d")


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
        assert first == f"<b>Radar weekly · 50 open ({number}/{total})</b>"
        assert second == POLL_LINE
    sent = [line for message in messages for line in item_lines(message.text)]
    assert sent == [item.line for item in digest.items]
    assert len(set(sent)) == 50
    footers = [message.text.endswith(FOOTER) for message in messages]
    assert footers == [False] * (total - 1) + [True]
    assert sum(message.text.count(FOOTER) for message in messages) == 1


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


# --- low-priority block ----------------------------------------------------------------------

OPENED = POLL - timedelta(days=8)


def lowp(
    name: str = "Omega",
    *,
    pid: str = "p5",
    repo_id: str = "r5",
    repo: str = "acme/api",
    action: str = "audit.tests",
    count: int = 2,
    opened: datetime = OPENED,
) -> LowSeries:
    return LowSeries(
        project_id=pid,
        project_name=name,
        repo_id=repo_id,
        repo_name=repo,
        action_key=action,
        high_now=count,
        opened_at=opened,
    )


def test_low_block_is_one_head_and_one_line_per_item() -> None:
    lines = lines_of(low_priority=(lowp(),))
    assert lines == [
        "<b>Radar weekly · 1 open</b>",
        POLL_LINE,
        "Low priority · 1",
        "🟡 <b>Omega</b> · tests High 2 · 8d",
        FOOTER,
    ]


def test_low_block_empty_changes_nothing() -> None:
    assert text_of(low_priority=()) == text_of()
    assert text_of(low_priority=(lowp(count=0),)) == CLEAR
    digest = build_digest(DigestInput(last_poll_at=POLL, max_items=10, high=(high(),)))
    assert (digest.low_lines, digest.low_hidden, digest.low_total) == ((), 0, 0)


def test_only_low_items_make_a_titled_digest_not_the_all_clear() -> None:
    text = text_of(low_priority=(lowp(),))
    assert text.startswith("<b>Radar weekly · 1 open</b>")
    assert "nothing open" not in text


def test_low_block_sits_after_the_high_items_and_before_the_rest() -> None:
    lines = lines_of(
        high=(high("Acme"),),
        stale=(stale("Beta"),),
        low_scores=(low("Gamma"),),
        low_priority=(lowp("Omega"),),
    )
    assert lines[:2] == ["<b>Radar weekly · 4 open</b>", POLL_LINE]
    assert lines[2:] == [
        "🔴 <b>Acme</b> · security High 3 · 16d",
        "Low priority · 1",
        "🟡 <b>Omega</b> · tests High 2 · 8d",
        "🟡 <b>Beta</b> · no scan 20d",
        "🟡 <b>Gamma</b> · ai-readiness 40/100 · 8d",
        FOOTER,
    ]


def test_low_block_is_capped_at_five_with_a_more_line() -> None:
    series = tuple(lowp(f"P{n}", pid=f"p{n}", repo_id=f"r{n}", count=n + 1) for n in range(6))
    lines = lines_of(low_priority=series)
    assert lines[2] == "Low priority · 6"
    assert [line.split("<b>")[1].split("</b>")[0] for line in lines[3:8]] == [
        "P5",
        "P4",
        "P3",
        "P2",
        "P1",
    ]
    assert lines[8] == "+1 more low priority."
    assert lines[-1] == FOOTER
    assert lines[0] == "<b>Radar weekly · 6 open</b>"


def test_low_block_order_is_count_age_name() -> None:
    older = OPENED - timedelta(days=3)
    series = (
        lowp("Zed", pid="p1", repo_id="r1", count=2),
        lowp("Abe", pid="p2", repo_id="r2", count=2),
        lowp("Old", pid="p3", repo_id="r3", count=2, opened=older),
        lowp("Big", pid="p4", repo_id="r4", count=9),
    )
    names = names_of(text_of(low_priority=series))
    assert names == ["Big", "Old", "Abe", "Zed"]
    assert names == names_of(text_of(low_priority=tuple(reversed(series))))


def test_low_block_shows_the_repo_only_when_two_repos_share_project_and_audit() -> None:
    two = (lowp("Omega", repo_id="r5"), lowp("Omega", repo_id="r6", repo="acme/web", count=1))
    assert item_lines(text_of(low_priority=two)) == [
        "🟡 <b>Omega</b> · tests (acme/api) High 2 · 8d",
        "🟡 <b>Omega</b> · tests (acme/web) High 1 · 8d",
    ]
    assert item_lines(text_of(low_priority=two[:1])) == ["🟡 <b>Omega</b> · tests High 2 · 8d"]


def test_low_items_do_not_use_the_max_items_cap() -> None:
    digest = build_digest(
        DigestInput(
            last_poll_at=POLL,
            max_items=3,
            stale=(stale("B1", pid="a"), stale("B2", pid="b"), stale("B3", pid="c")),
            low_priority=(lowp("L1", pid="x"), lowp("L2", pid="y", repo_id="r9")),
        )
    )
    assert digest.hidden_items == 0
    assert digest.total_items == 3
    assert digest.low_total == 2
    assert len(item_lines(text_of(max_items=3, stale=(stale(),), low_priority=(lowp(),)))) == 2


def test_low_block_escapes_names() -> None:
    nasty = '<b>x</b> & "q"'
    text = text_of(low_priority=(lowp(nasty),))
    assert "<b>x</b>" not in text
    assert '&lt;b&gt;x&lt;/b&gt; &amp; "q"' in text


def test_low_block_in_russian() -> None:
    assert lines_of("ru", low_priority=(lowp(),)) == [
        "<b>Радар · дайджест · открыто: 1</b>",
        "Последний опрос: 2026-11-18 06:00 UTC",
        "Низкий приоритет · 1",
        "🟡 <b>Omega</b> · tests высокие 2 · 8 д",
        "Возраст считается с первого появления в радаре.",
    ]
    series = tuple(lowp(f"P{n}", pid=f"p{n}", repo_id=f"r{n}") for n in range(7))
    assert "Низкий приоритет: не показано ещё 2." in lines_of("ru", low_priority=series)


def test_low_block_splits_between_lines_and_keeps_every_line() -> None:
    series = tuple(lowp(f"P{n}", pid=f"p{n}", repo_id=f"r{n}") for n in range(5))
    digest = build_digest(
        DigestInput(
            last_poll_at=POLL,
            max_items=50,
            stale=tuple(stale(f"S{n}" * 30, pid=f"s{n}") for n in range(60)),
            low_priority=series,
        )
    )
    messages = render_digest(digest, max_chars=1000)
    assert len(messages) > 1
    joined = "\n".join(message.text for message in messages)
    assert all(f"<b>P{n}</b>" in joined for n in range(5))
    assert all(telegram_length(message.text) <= 1000 for message in messages)


# --- the green block: one line per clean project ---


def green(language: Language = "en", **kwargs: object) -> list[str]:
    """The lines of the digest from the all-clear line on, for the given clean projects."""
    return lines_of(language, **kwargs)


def test_green_block_is_head_plus_one_line_per_project() -> None:
    lines = green(clean_projects=("p1", "p2"), project_names={"p1": "Acme", "p2": "Beta"})
    assert lines == [
        CLEAR,
        "Clean · 2",
        "🟢 <b>Acme</b> · no high issues",
        "🟢 <b>Beta</b> · no high issues",
    ]


def test_green_lines_are_sorted_by_name_then_id_whatever_the_input_order() -> None:
    names = {"p1": "beta", "p2": "Alpha", "p3": "Beta", "p4": "alpha"}
    forward = green(clean_projects=("p1", "p2", "p3", "p4"), project_names=names)
    backward = green(clean_projects=("p4", "p3", "p2", "p1"), project_names=names)
    assert forward == backward
    assert [line.split("<b>")[1].split("</b>")[0] for line in forward[2:]] == [
        "Alpha",
        "alpha",
        "beta",
        "Beta",
    ]


def test_green_block_empty_changes_nothing() -> None:
    assert green(clean_projects=()) == [CLEAR]
    assert "🟢" not in text_of(stale=(stale(),))


def test_green_duplicate_ids_collapse() -> None:
    lines = green(clean_projects=("p1", "p1"), project_names={"p1": "Acme"})
    assert lines[1:] == ["Clean · 1", "🟢 <b>Acme</b> · no high issues"]


def test_green_is_outside_the_title_count_the_cap_and_the_footer() -> None:
    lines = lines_of(
        max_items=1,
        high=(high(),),
        stale=(stale(),),
        clean_projects=("p7", "p8"),
        project_names={"p7": "Clean A", "p8": "Clean B"},
    )
    assert lines[0] == "<b>Radar weekly · 2 open</b>"
    assert "+1 more open item not shown." in lines
    assert sum(line.startswith("🟢") for line in lines) == 2
    assert lines[-1] == FOOTER
    # Without a High line the green lines alone never bring the footer.
    assert FOOTER not in lines_of(clean_projects=("p7",), project_names={"p7": "Clean A"})


def test_green_block_sits_after_resolved_and_before_the_footer() -> None:
    lines = lines_of(
        high=(high(),),
        resolved=(resolved("never_ran", pid="p5"),),
        clean_projects=("p7",),
        project_names={"p5": "Nova", "p7": "Acme"},
    )
    assert lines[-5:] == [
        "Resolved · 1",
        "<b>Nova</b> · no scan yet closed",
        "Clean · 1",
        "🟢 <b>Acme</b> · no high issues",
        FOOTER,
    ]


def test_green_with_nothing_open_keeps_the_all_clear_line_first() -> None:
    lines = green(unread_projects=1, clean_projects=("p7",), project_names={"p7": "Acme"})
    assert lines[0].startswith("<b>Radar weekly</b> · nothing open in the projects read")
    assert lines[-2:] == ["Clean · 1", "🟢 <b>Acme</b> · no high issues"]


def test_green_in_russian() -> None:
    kwargs: dict[str, object] = {
        "clean_projects": ("p1", "p2"),
        "project_names": {"p1": "Яр", "p2": "Аз"},
    }
    ru = green("ru", **kwargs)
    assert ru[-3:] == [
        "Чисто · 2",
        "🟢 <b>Аз</b> · нет высоких проблем",
        "🟢 <b>Яр</b> · нет высоких проблем",
    ]
    assert len(green("en", **kwargs)) == len(ru)


def test_green_name_is_cleaned_escaped_and_an_unknown_id_is_unnamed() -> None:
    lines = green(clean_projects=("p1", "p2"), project_names={"p1": "A <b>&\n  Co"})
    assert "🟢 <b>A &lt;b&gt;&amp; Co</b> · no high issues" in lines
    assert "🟢 <b>(unnamed project)</b> · no high issues" in lines


def test_green_block_splits_between_lines_and_keeps_every_line() -> None:
    ids = tuple(f"p{n:02d}" for n in range(60))
    names = {pid: f"Clean project {pid} " + "x" * 40 for pid in ids}
    digest = build_digest(
        DigestInput(last_poll_at=POLL, max_items=5, clean_projects=ids, project_names=names)
    )
    messages = render_digest(digest, max_chars=1000)
    assert len(messages) > 1
    assert all(telegram_length(message.text) <= 1000 for message in messages)
    sent = [
        line for message in messages for line in message.text.split("\n") if line.startswith("🟢")
    ]
    assert sent == list(digest.clean_lines)
    assert len(set(sent)) == 60
