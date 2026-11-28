from datetime import UTC, datetime, timedelta, timezone

import pytest

from enji_guard_radar.digest import DigestInput, build_digest, render_digest
from enji_guard_radar.high_rules import LowSeries
from enji_guard_radar.ledger import ResolvedItem
from enji_guard_radar.messages import TELEGRAM_MAX_CHARS, telegram_length
from enji_guard_radar.model import Alert, AlertKey, AlertKind, Audience, Severity
from enji_guard_radar.texts import Language

POLL = datetime(2026, 11, 18, 6, 0, tzinfo=UTC)
CLEAR = "<b>Radar · weekly digest</b> · nothing open · last poll 2026-11-18 06:00 UTC"
CLEAR_RU = (
    "<b>Радар · недельная сводка</b> · ничего не открыто · последний опрос 2026-11-18 06:00 UTC"
)
POLL_FOOTER = "Poll 2026-11-18 06:00 UTC"


def high(
    name: str = "Acme",
    *,
    pid: str = "p1",
    repo_id: str = "r1",
    action: str = "audit.security",
    count: int | None = 3,
    age: int | None = 16,
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
        repo_name="acme/svc",
        seen_since=datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
    )


def lowp(
    name: str = "Omega",
    *,
    pid: str = "p5",
    repo_id: str = "r5",
    action: str = "audit.tests",
    count: int = 2,
    age: int = 8,
) -> LowSeries:
    return LowSeries(
        project_id=pid,
        project_name=name,
        repo_id=repo_id,
        repo_name="acme/api",
        action_key=action,
        high_now=count,
        opened_at=POLL - timedelta(days=age),
    )


def stale(name: str = "Beta", *, pid: str = "p2", age: int = 20) -> Alert:
    return Alert(
        key=AlertKey(project_id=pid, level="stale"),
        kind=AlertKind.STALE,
        project_name=name,
        severity=Severity.MEDIUM,
        age_days=age,
        last_run_at=datetime(2026, 10, 29, 12, 0, tzinfo=UTC),
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


def resolved(
    level: str = "high", *, pid: str = "p1", action: str = "audit.security", repo_id: str = "r1"
) -> ResolvedItem:
    is_high = level == "high"
    return ResolvedItem(
        key=AlertKey(
            project_id=pid,
            repo_id=repo_id if is_high else "*",
            action_key=action if is_high else "*",
            level=level,
        ),
        resolved_at=POLL - timedelta(days=2),
    )


def text_of(language: Language = "en", **kwargs: object) -> str:
    kwargs.setdefault("last_poll_at", POLL)
    kwargs.setdefault("max_items", 10)
    messages = render_digest(build_digest(DigestInput(**kwargs), language=language))
    assert len(messages) == 1
    return messages[0].text


def lines_of(language: Language = "en", **kwargs: object) -> list[str]:
    return text_of(language, **kwargs).split("\n")


def project_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith(("🔴", "🟡", "🟢"))]


def names_of(lines: list[str]) -> list[str]:
    return [line.split("<b>")[1].split("</b>")[0] for line in project_lines(lines)]


# --- the digest by project --------------------------------------------------------------------


def mixed_input() -> dict[str, object]:
    """Red with three audits, red with one, low-priority only, clean, unread, resolved."""
    return {
        "high": (
            high("Enji Fleet", pid="f", action="audit.dependency-hygiene", count=30, age=17),
            high("Enji Fleet", pid="f", action="audit.security", repo_id="r2", count=10, age=9),
            high("Enji Fleet", pid="f", action="audit.tests", count=2, age=3),
            high("enjiorg", pid="e", action="audit.dependency-hygiene", count=2, age=10),
        ),
        "low_priority": (lowp("Low Co", pid="l", action="audit.tests", count=4, age=4),),
        "clean_projects": ("g",),
        "project_names": {"g": "google-workspace-applications"},
        "unread_projects": 5,
        "resolved": (resolved(pid="a"), resolved(pid="b"), resolved(pid="c", repo_id="r2")),
    }


def test_mixed_digest_reads_as_an_overview_then_one_line_per_project_in_english() -> None:
    assert text_of("en", **mixed_input()) == (
        "<b>Radar · weekly digest</b>\n"
        "High problems: 48 in 3 projects, closed this week: 3.\n"
        "🔴 <b>Enji Fleet</b> — 42 high problems; most in: dependencies, security; oldest 17 days\n"
        "🔴 <b>enjiorg</b> — 2 high problems; most in: dependencies; 10 days\n"
        "🟡 <b>Low Co</b> — 4 high problems, low priority; most in: tests; 4 days\n"
        "🟢 <b>google-workspace-applications</b> — clean\n"
        "⚠️ Could not read 5 projects · poll 2026-11-18 06:00 UTC"
    )


def test_mixed_digest_reads_as_an_overview_then_one_line_per_project_in_russian() -> None:
    assert text_of("ru", **mixed_input()) == (
        "<b>Радар · недельная сводка</b>\n"
        "Высоких проблем: 48 в 3 проектах, закрыто за неделю: 3.\n"
        "🔴 <b>Enji Fleet</b> — 42 высокие проблемы; больше всего: зависимости, безопасность; "
        "самая старая 17 дней\n"
        "🔴 <b>enjiorg</b> — 2 высокие проблемы; больше всего: зависимости; 10 дней\n"
        "🟡 <b>Low Co</b> — 4 высокие проблемы, низкий приоритет; больше всего: тесты; 4 дня\n"
        "🟢 <b>google-workspace-applications</b> — чисто\n"
        "⚠️ Не удалось прочитать 5 проектов · опрос 2026-11-18 06:00 UTC"
    )


def test_digest_has_one_line_per_project_and_none_of_the_removed_parts() -> None:
    lines = lines_of("en", **mixed_input())
    assert len(project_lines(lines)) == 4
    assert len(lines) == 2 + 4 + 1  # title, overview, projects, footer
    text = "\n".join(lines)
    for gone in ("not shown", "Resolved", "Ages count", "Low priority", "acme/", "recurring"):
        assert gone not in text


@pytest.mark.parametrize(
    ("alerts", "red_names"),
    [
        (
            (high("Small", pid="a", count=2, age=30), high("Big", pid="b", count=9)),
            ["Big", "Small"],
        ),
        ((high("Young", pid="a", age=2), high("Old", pid="b", age=20)), ["Old", "Young"]),
        ((high("Beta", pid="b"), high("alpha", pid="a")), ["alpha", "Beta"]),
    ],
    ids=["most high first", "then oldest", "then name"],
)
def test_red_projects_are_ordered_by_count_then_age_then_name(
    alerts: tuple[Alert, ...], red_names: list[str]
) -> None:
    assert names_of(lines_of(high=alerts)) == red_names
    assert names_of(lines_of(high=tuple(reversed(alerts)))) == red_names


def test_red_comes_before_low_priority_only_before_stale_only_before_clean() -> None:
    lines = lines_of(
        high=(high("Red", pid="r", count=1),),
        low_priority=(lowp("Yellow", pid="y", count=50),),
        stale=(stale("Stale", pid="s", age=99),),
        clean_projects=("g",),
        project_names={"g": "Green"},
    )
    assert names_of(lines) == ["Red", "Yellow", "Stale", "Green"]
    assert [line[0] for line in project_lines(lines)] == ["🔴", "🟡", "🟡", "🟢"]


def test_a_project_with_high_in_both_kinds_of_audit_is_red_and_counts_the_rest() -> None:
    lines = lines_of(
        high=(high("Acme", count=5, age=12),),
        low_priority=(lowp("Acme", pid="p1", count=3, age=40),),
    )
    assert project_lines(lines) == [
        "🔴 <b>Acme</b> — 5 high problems; most in: security; 12 days; "
        "3 more in low-priority audits"
    ]
    assert "High problems: 8 in 1 project," in lines[1]
    ru = project_lines(lines_of("ru", high=(high("Acme"),), low_priority=(lowp("Acme", pid="p1"),)))
    assert ru[0].endswith("; ещё 2 в аудитах низкого приоритета")


def test_low_priority_only_project_is_yellow_and_says_so() -> None:
    (line,) = project_lines(lines_of(low_priority=(lowp("Omega", count=1, age=0),)))
    assert line == "🟡 <b>Omega</b> — 1 high problem, low priority; most in: tests; today"


def test_stale_and_never_ran_projects_are_yellow_without_a_count() -> None:
    lines = lines_of(stale=(stale("Beta", age=20), never_ran("Nova")))
    assert project_lines(lines) == [
        "🟡 <b>Beta</b> — no scan for 20 days",
        "🟡 <b>Nova</b> — no scan yet",
    ]
    assert lines[1] == "No open high problems, closed this week: 0."
    ru = project_lines(lines_of("ru", stale=(stale("Beta", age=2), never_ran("Nova"))))
    assert ru == ["🟡 <b>Beta</b> — нет проверок 2 дня", "🟡 <b>Nova</b> — проверок ещё не было"]


def test_a_red_project_that_is_also_stale_stays_one_line() -> None:
    lines = lines_of(high=(high("Acme", count=1, age=2),), stale=(stale("Acme", pid="p1", age=30),))
    assert project_lines(lines) == [
        "🔴 <b>Acme</b> — 1 high problem; most in: security; 2 days; no scan for 30 days"
    ]


@pytest.mark.parametrize(
    ("age", "english", "russian"),
    [
        (0, "today", "сегодня"),
        (1, "1 day", "1 день"),
        (2, "2 days", "2 дня"),
        (21, "21 days", "21 день"),
    ],
)
def test_project_age_is_spelled_out_in_words(age: int, english: str, russian: str) -> None:
    (en,) = project_lines(lines_of("en", high=(high(age=age),)))
    (ru,) = project_lines(lines_of("ru", high=(high(age=age),)))
    assert en.endswith(f"; {english}")
    assert ru.endswith(f"; {russian}")


def test_oldest_is_said_only_when_several_series_stand_behind_the_age() -> None:
    one = project_lines(lines_of(high=(high(age=7),)))[0]
    two = project_lines(
        lines_of(high=(high(age=7), high(repo_id="r2", age=11), high(repo_id="r3", age=2)))
    )[0]
    assert one.endswith("; 7 days")
    assert two.endswith("; oldest 11 days")


def test_two_repos_of_one_project_and_audit_are_one_count_without_repo_names() -> None:
    (line,) = project_lines(lines_of(high=(high(count=2), high(repo_id="r2", count=3))))
    assert line.startswith("🔴 <b>Acme</b> — 5 high problems; most in: security;")
    assert "acme/" not in line


def test_duplicate_input_series_collapse() -> None:
    (line,) = project_lines(lines_of(high=(high(count=2), high(count=2))))
    assert "2 high problems" in line


# --- audit names ------------------------------------------------------------------------------


def test_audit_keys_become_short_words_with_the_raw_key_for_an_unknown_one() -> None:
    (line,) = project_lines(
        lines_of(high=(high(action="audit.dependency-hygiene"), high(action="audit.<new>&")))
    )
    assert "most in: &lt;new&gt;&amp;, dependencies;" in line


def test_at_most_two_audits_are_named_biggest_first_ties_by_key() -> None:
    (line,) = project_lines(
        lines_of(
            high=(
                high(action="audit.tests", count=5),
                high(action="audit.security", count=5, repo_id="r2"),
                high(action="audit.dead-code", count=5, repo_id="r3"),
                high(action="audit.recon", count=9, repo_id="r4"),
            )
        )
    )
    assert "most in: overview, dead code;" in line


# --- all clear, unread, clean projects ---------------------------------------------------------


def test_all_clear_is_one_line_in_both_languages() -> None:
    assert lines_of() == [CLEAR]
    assert lines_of("ru") == [CLEAR_RU]


def test_all_clear_with_unread_projects_says_so_and_warns() -> None:
    lines = lines_of(unread_projects=2)
    assert lines == [
        "<b>Radar · weekly digest</b> · nothing open in the projects read · "
        "last poll 2026-11-18 06:00 UTC",
        "⚠️ Could not read 2 projects",
    ]
    assert lines_of("ru", unread_projects=1)[1] == "⚠️ Не удалось прочитать 1 проект"


def test_unread_warning_carries_the_poll_time_when_something_is_open() -> None:
    assert lines_of(high=(high(),), unread_projects=1)[-1] == (
        "⚠️ Could not read 1 project · poll 2026-11-18 06:00 UTC"
    )
    assert lines_of(high=(high(),))[-1] == POLL_FOOTER


def test_all_clear_with_resolved_items_adds_the_closed_count_line() -> None:
    assert lines_of(resolved=(resolved(pid="a"), resolved(pid="b"))) == [
        CLEAR,
        "No open high problems, closed this week: 2.",
    ]
    assert lines_of("ru", resolved=(resolved(),)) == [
        CLEAR_RU,
        "Открытых высоких проблем нет, закрыто за неделю: 1.",
    ]


def test_clean_projects_follow_the_all_clear_line_sorted_by_name_then_id() -> None:
    lines = lines_of(
        clean_projects=("p2", "p1", "p3"), project_names={"p1": "beta", "p2": "Alpha", "p3": "beta"}
    )
    assert lines == [
        CLEAR,
        "🟢 <b>Alpha</b> — clean",
        "🟢 <b>beta</b> — clean",
        "🟢 <b>beta</b> — clean",
    ]


def test_clean_projects_above_the_cap_collapse_into_one_more_line() -> None:
    ids = tuple(f"p{i}" for i in range(5))
    names = {pid: f"Name {pid}" for pid in ids}
    lines = lines_of(max_items=3, clean_projects=ids, project_names=names, high=(high(pid="red"),))
    assert names_of(lines)[1:] == ["Name p0", "Name p1", "Name p2"]
    assert lines[-2] == "and 2 more projects clean"
    assert lines_of("ru", max_items=3, clean_projects=ids, project_names=names)[-1] == (
        "и ещё 2 проекта чисто"
    )


@pytest.mark.parametrize(("clean", "more"), [(3, False), (4, True)])
def test_clean_cap_boundary(clean: int, more: bool) -> None:
    ids = tuple(f"p{i}" for i in range(clean))
    text = text_of(max_items=3, clean_projects=ids, project_names=dict.fromkeys(ids, "X"))
    assert ("and 1 more project clean" in text) is more
    assert text.count("🟢") == min(clean, 3)


def test_a_clean_id_that_has_an_open_problem_is_not_listed_as_clean() -> None:
    lines = lines_of(high=(high("Acme"),), clean_projects=("p1",), project_names={"p1": "Acme"})
    assert [line[0] for line in project_lines(lines)] == ["🔴"]


def test_clean_duplicate_ids_collapse_and_unknown_id_is_unnamed() -> None:
    lines = lines_of(clean_projects=("p1", "p1", "zz"), project_names={"p1": "Acme"})
    assert lines[1:] == [
        "🟢 <b>(unnamed project)</b> — clean",
        "🟢 <b>Acme</b> — clean",
    ]


# --- the resolved count ------------------------------------------------------------------------


def test_resolved_is_only_a_count_in_the_overview() -> None:
    text = text_of(
        high=(high(),),
        resolved=(resolved(pid="a"), resolved(pid="b"), resolved("stale", pid="c")),
    )
    assert "closed this week: 2." in text
    assert "closed" not in text.replace("closed this week: 2.", "")


def test_duplicate_resolved_keys_count_once_and_none_is_zero() -> None:
    assert "closed this week: 1." in text_of(
        high=(high(),), resolved=(resolved(pid="a"), resolved(pid="a"))
    )
    assert "closed this week: 0." in text_of(high=(high(),))


def test_overview_numbers_in_russian_agree_with_their_nouns() -> None:
    one = lines_of("ru", high=(high(count=1),), resolved=(resolved(),))[1]
    assert one == "Высоких проблем: 1 в 1 проекте, закрыто за неделю: 1."
    many = lines_of("ru", high=(high("A", pid="a", count=5), high("B", pid="b", count=6)))[1]
    assert many == "Высоких проблем: 11 в 2 проектах, закрыто за неделю: 0."


# --- text safety -------------------------------------------------------------------------------


def test_names_are_escaped_and_cut() -> None:
    nasty = '<b>x</b> & "q" <script>alert(1)</script>'
    text = text_of(
        high=(high(nasty),),
        stale=(stale(nasty, pid="p7"),),
        clean_projects=("p8",),
        project_names={"p8": nasty},
    )
    assert "<script" not in text
    assert "&lt;script&gt;" in text
    assert "&amp;amp;" not in text
    long = text_of(stale=(stale("N" * 200),))
    assert "N" * 80 not in long
    assert "N" * 77 + "..." in long


def test_counts_only_no_ids_paths_or_placeholders() -> None:
    text = text_of(high=(high(),), stale=(stale(),), resolved=(resolved(),))
    assert "F-001" not in text
    assert "src/" not in text
    assert "Placeholder" not in text
    assert "p1" not in text.replace("<b>", "")


def test_utc_poll_time() -> None:
    plus_six = datetime(2026, 11, 18, 6, 0, tzinfo=UTC).astimezone(timezone(timedelta(hours=6)))
    poll = plus_six.replace(hour=1, minute=30)
    assert text_of(last_poll_at=poll, high=(high(),)).endswith("Poll 2026-11-17 19:30 UTC")
    assert text_of(last_poll_at=poll).endswith("last poll 2026-11-17 19:30 UTC")


def test_utc_rejects_naive_poll_time() -> None:
    with pytest.raises(ValueError, match="timezone"):
        DigestInput(last_poll_at=datetime(2026, 11, 18, 6, 0), max_items=10)  # noqa: DTZ001


def test_digest_message_is_a_group_message_without_keys() -> None:
    (message,) = render_digest(
        build_digest(DigestInput(**mixed_input(), last_poll_at=POLL, max_items=9))
    )
    assert message.keys == ()
    assert message.audience is Audience.GROUP
    assert telegram_length(message.text) < TELEGRAM_MAX_CHARS


# --- splitting ---------------------------------------------------------------------------------


def test_split_into_numbered_messages_at_line_boundaries() -> None:
    alerts = tuple(high("N" * 80, pid=f"p{i:02d}", count=100 - i) for i in range(60))
    digest = build_digest(DigestInput(last_poll_at=POLL, max_items=5, high=alerts))
    messages = render_digest(digest)
    assert len(messages) > 1
    total = len(messages)
    for number, message in enumerate(messages, start=1):
        assert telegram_length(message.text) <= 4000
        first, second = message.text.split("\n")[:2]
        assert first == f"<b>Radar · weekly digest ({number}/{total})</b>"
        assert second.startswith("High problems: ")
    sent = [line for message in messages for line in project_lines(message.text.split("\n"))]
    assert sent == list(digest.project_lines)
    assert len(set(sent)) == 60
    assert [m.text.endswith(POLL_FOOTER) for m in messages] == [False] * (total - 1) + [True]


def test_split_leaves_a_single_message_unnumbered() -> None:
    messages = render_digest(build_digest(DigestInput(last_poll_at=POLL, max_items=3)))
    assert len(messages) == 1
    assert "(1/1)" not in messages[0].text


def test_all_clear_with_many_clean_projects_splits_but_keeps_one_unnumbered_head() -> None:
    ids = tuple(f"p{i:03d}" for i in range(50))
    names = dict.fromkeys(ids, "C" * 80)
    digest = build_digest(
        DigestInput(last_poll_at=POLL, max_items=50, clean_projects=ids, project_names=names)
    )
    messages = render_digest(digest, max_chars=1000)
    assert len(messages) > 1
    assert all(telegram_length(m.text) <= 1000 for m in messages)
    assert all(m.text.startswith(CLEAR) for m in messages)
    assert sum(m.text.count("🟢") for m in messages) == 50


# --- bad input ---------------------------------------------------------------------------------


def test_invalid_inputs_are_rejected() -> None:
    for alert in (high(count=0), high(kind=AlertKind.STALE), high(age=None)):
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
