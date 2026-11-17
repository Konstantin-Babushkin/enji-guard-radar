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
