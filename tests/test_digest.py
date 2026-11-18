from datetime import UTC, datetime, timedelta

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
    messages = render_digest(build_digest(DigestInput(**kwargs)))  # type: ignore[arg-type]
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
