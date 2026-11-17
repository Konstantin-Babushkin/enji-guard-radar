import hashlib
import io
from pathlib import Path

from enji_guard_radar import cli

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
DEMO_NOW = "2026-11-16T12:00:00Z"

B_LINES = [
    "Stale Guard scans: <b>Project B</b>",
    "No completed Guard run for 27 days (last: 2026-10-20).",
    "Severity: medium.",
]
C_LINES = [
    "No Guard runs: <b>Project C (no repositories)</b>",
    "It has no repositories in Guard.",
    "Severity: medium.",
]


def run_dry(now: str = DEMO_NOW) -> tuple[int, str]:
    out = io.StringIO()
    code = cli.main(
        ["run", "--dry-run", "--fixtures", str(FIXTURES), "--now", now],
        stdout=out,
    )
    return code, out.getvalue()


def contains_contiguous(text: str, wanted: list[str]) -> bool:
    lines = text.splitlines()
    return any(lines[i : i + len(wanted)] == wanted for i in range(len(lines)))


def fixture_digests() -> dict[str, str]:
    return {
        str(path.relative_to(FIXTURES)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(FIXTURES.rglob("*"))
        if path.is_file()
    }


def test_dry_run_prints_stale_alert_for_project_b() -> None:
    code, out = run_dry()
    assert code == 0
    assert contains_contiguous(out, B_LINES)


def test_dry_run_reports_project_c_as_never_ran() -> None:
    _, out = run_dry()
    assert contains_contiguous(out, C_LINES)


def test_dry_run_does_not_alert_fresh_project_a() -> None:
    # A's newest run (c012) sits on page 2 of its runs; page 3 is unrecorded, so reading it
    # would make A "not checked". A must appear neither as an alert nor under "not checked".
    _, out = run_dry()
    assert "Project A" not in out


def test_dry_run_is_repeatable_and_leaves_fixtures_unchanged() -> None:
    before = fixture_digests()
    first = run_dry()
    second = run_dry()
    assert first == second
    assert fixture_digests() == before


def test_dry_run_lists_unreachable_project_d_as_not_checked() -> None:
    code, out = run_dry()
    assert code == 0
    lines = out.splitlines()
    assert "--- not checked (1) ---" in lines
    assert (
        "Project D (Guard unreachable): Guard data unavailable "
        "(list_repositories failed: simulated Guard outage)"
    ) in lines
    assert "Project D" not in "\n".join(line for line in lines if line.endswith("</b>"))


def test_dry_run_alerts_long_stale_project_e() -> None:
    _, out = run_dry()
    assert "No completed Guard run for 104 days (last: 2026-08-04)." in out.splitlines()


def test_dry_run_summary_counts() -> None:
    _, out = run_dry()
    assert (
        "summary: 5 evaluated, 2 stale, 1 never ran, 1 not checked, 3 message(s) would be sent"
        in out.splitlines()
    )


def test_dry_run_orders_alerts_worst_first() -> None:
    _, out = run_dry()
    headlines = [
        line
        for line in out.splitlines()
        if line.startswith(("Stale Guard scans:", "No Guard runs:"))
    ]
    assert headlines == [
        "Stale Guard scans: <b>Project E (paused in config)</b>",
        "Stale Guard scans: <b>Project B</b>",
        "No Guard runs: <b>Project C (no repositories)</b>",
    ]
