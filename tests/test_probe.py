"""`probe`: dirty Guard in, clean replayable fixtures out, and a refusal when anything could leak.

Everything is fake: a dirty in-memory Guard session with injected real-looking content. No
network, no real data. The dirt is synthetic on purpose; every row proves that it never reaches a
file.
"""

import ast
import io
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Self

import pytest
from guard_fakes import Key, call_key, report_markdown, run_item, world

from enji_guard_radar import cli, probe_sanitize
from enji_guard_radar.guard import (
    ARG_CURSOR,
    ARG_PROJECT_ID,
    GUARD_READ_TOOLS,
    TOOL_LIST_AUDIT_RUNS,
    TOOL_LIST_PROJECTS,
    TOOL_LIST_REPOSITORIES,
    ListingOk,
    fetch_project,
    list_projects,
)
from enji_guard_radar.guard_auth import AUTH_FAILURE_TEXT
from enji_guard_radar.guard_report import ARG_TASK_ID, TOOL_GET_AUDIT_REPORT
from enji_guard_radar.ports import (
    AuthFailureKind,
    GuardAuthError,
    GuardCallError,
    GuardFailure,
    GuardSession,
)
from enji_guard_radar.probe import (
    RawReport,
    Recording,
    RecordingGuardTools,
    format_summary,
    summarize,
)
from enji_guard_radar.probe_sanitize import (
    SanitizedFixtures,
    Sanitizer,
    check_round_trip,
    find_taint,
    rebuild_report,
    sanitize_recordings,
    scan_for_leaks,
    taint_strings,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "enji_guard_radar"
NOW = "2026-11-16T12:00:00Z"

RAW_PROJECT = "0a1b2c3d-1111-4222-8333-444455556666"
RAW_PAUSED_PROJECT = "0a1b2c3d-9999-4222-8333-444455556666"
RAW_REPO = "repo_9f8e7d6c-aaaa-4bbb-8ccc-ddddeeeeffff"
RAW_SECURITY_TASK = "11112222-3333-4444-8555-666677778888"
RAW_RECON_TASK = "11112222-3333-4444-8555-666677779999"
RAW_SHA = "9f8e7d6c5b4a39281706f5e4d3c2b1a098765432"
RAW_TOKEN = "ghp_" + "A1b2C3d4" * 4 + "Zz9Y"
RAW_PROJECT_NAME = "Real Corp Payments"
RAW_REPO_NAME = "realcorp/payments-api"

DIRTY_REPORT = f"""# SQL injection in charge endpoint

## Executive summary

Contact jane.doe@realcorp.com about {RAW_TOKEN} in {RAW_REPO_NAME}.

## What confirmed risks exist in the charge flow?

See https://guard.enji.ai/app/projects/{RAW_PROJECT}.

**Score: 31 / 100** · Significant fixes needed · Confidence: High

`{RAW_REPO_NAME}` · `main` · `{RAW_SHA}`

## Security findings

### `SQL-INJECTION-IN-CHARGE` — SQL injection in charge endpoint

**Severity:** High · **Confidence:** High

**Evidence:**

- `src/payments/charge.py:42`

### `F-002` — Weak password hash

**Severity:** Medium · **Confidence:** Medium
"""

DIRTY_OVERVIEW = f"""# Repository Overview of {RAW_PROJECT_NAME}

## Executive Summary

- {RAW_REPO_NAME} handles card payments.
"""

FORBIDDEN = (
    "real corp",
    "realcorp",
    "sql injection",
    "sql-injection",
    "payments/charge.py",
    "jane.doe",
    "ghp_",
    "guard.enji.ai",
    RAW_PROJECT,
    RAW_REPO,
    RAW_SECURITY_TASK,
    RAW_SHA,
    "password hash",
)


def report_page(content: str) -> Mapping[str, object]:
    return {
        "task_id": "",
        "report_status": "ready",
        "content": content,
        "content_version": "raw-version-1",
        "has_more": False,
    }


def dirty_responses(
    *, projects: tuple[tuple[str, str], ...] | None = None
) -> dict[Key, Mapping[str, object] | Exception]:
    security = run_item(RAW_SECURITY_TASK, "audit.security", "2026-11-16T10:00:00.123456Z")
    security["report_url"] = (
        f"https://guard.enji.ai/app/projects/{RAW_PROJECT}/repos/{RAW_REPO}/vulns"
        f"?run={RAW_SECURITY_TASK}"
    )
    security["internal_note"] = "secret planning note"
    recon = run_item(RAW_RECON_TASK, "audit.recon", "2026-11-16T09:00:00.123456Z")
    responses = world(
        projects=projects or ((RAW_PROJECT, RAW_PROJECT_NAME),),
        repos_by_project={
            RAW_PROJECT: [(RAW_REPO, RAW_REPO_NAME)],
            RAW_PAUSED_PROJECT: [],
        },
        runs_by_repo={RAW_REPO: [security, recon]},
        reports_by_task={
            RAW_SECURITY_TASK: {**report_page(DIRTY_REPORT), "task_id": RAW_SECURITY_TASK},
            RAW_RECON_TASK: {**report_page(DIRTY_OVERVIEW), "task_id": RAW_RECON_TASK},
        },
    )
    return dict(responses)


class FakeGuardSession:
    """A GuardSession over a response map; remembers every call."""

    def __init__(
        self,
        responses: Mapping[Key, Mapping[str, object] | Exception],
        *,
        enter_error: Exception | None = None,
    ) -> None:
        self.responses = responses
        self.enter_error = enter_error
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.entered = False

    def __enter__(self) -> Self:
        if self.enter_error is not None:
            raise self.enter_error
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.entered = False

    def tool_names(self) -> frozenset[str]:
        return GUARD_READ_TOOLS

    def tool_arguments(self) -> Mapping[str, tuple[str, ...]]:
        return {
            TOOL_LIST_PROJECTS: ("cursor",),
            TOOL_LIST_REPOSITORIES: ("cursor", "project_id"),
            TOOL_LIST_AUDIT_RUNS: ("cursor", "project_id", "repo_id"),
            TOOL_GET_AUDIT_REPORT: ("cursor", "project_id", "repo_id", "task_id"),
        }

    def result_shapes(self) -> Mapping[str, int]:
        return {"structured": len(self.calls)}

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        self.calls.append((tool, dict(args)))
        key = call_key(tool, **args)
        if key not in self.responses:
            message = f"no response for {tool}"
            raise GuardCallError(message)
        response = self.responses[key]
        if isinstance(response, Exception):
            raise response
        return response


def dirty_session() -> FakeGuardSession:
    return FakeGuardSession(dirty_responses())


def connector_for(session: GuardSession) -> Callable[[Path], GuardSession]:
    return lambda _directory: session


def never_connect(_directory: Path) -> GuardSession:
    message = "the Guard connector must not be called"
    raise AssertionError(message)


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """`probe` installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def run_probe(
    out_dir: Path,
    session: GuardSession | None,
    tmp_path: Path,
    *,
    extra: list[str] | None = None,
) -> tuple[int, str]:
    buffer = io.StringIO()
    code = cli.main(
        ["probe", "--out", str(out_dir), "--state-dir", str(tmp_path / "state"), *(extra or [])],
        guard_connector=connector_for(session) if session is not None else never_connect,
        stdout=buffer,
    )
    return code, buffer.getvalue()


def all_text(directory: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(directory.iterdir()) if path.is_file()
    )


# --- tracer ------------------------------------------------------------------------------------


def test_probe_writes_clean_fixtures_that_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "recorded"
    code, output = run_probe(out_dir, dirty_session(), tmp_path)
    assert code == 0, capsys.readouterr().err

    written = all_text(out_dir).casefold()
    for forbidden in FORBIDDEN:
        assert forbidden.casefold() not in written
    assert "High=1" in output
    assert "Medium=1" in output
    assert "Review with: git diff --stat -- " in output

    replay = io.StringIO()
    replay_code = cli.main(
        [
            "run",
            "--dry-run",
            "--fixtures",
            str(out_dir),
            "--state-dir",
            str(tmp_path / "replay-state"),
            "--env-file",
            str(tmp_path / "none.env"),
            "--now",
            NOW,
        ],
        environ={},
        stdout=replay,
    )
    assert replay_code == 0
    assert "summary:" in replay.getvalue()
    assert "1 evaluated" in replay.getvalue()


# --- recorder and sanitizer --------------------------------------------------------------------


def test_recorder_keeps_answers_and_failures_and_adds_no_tool() -> None:
    session = FakeGuardSession(
        {
            call_key(TOOL_LIST_PROJECTS): {"projects": [], "has_more": False},
            call_key(TOOL_GET_AUDIT_REPORT, task_id="t"): GuardCallError(
                "down", cause=GuardFailure.UNAVAILABLE
            ),
        }
    )
    recorder = RecordingGuardTools(session)
    recorder.call(TOOL_LIST_PROJECTS, {})
    with pytest.raises(GuardCallError):
        recorder.call(TOOL_GET_AUDIT_REPORT, {"task_id": "t"})
    with pytest.raises(GuardCallError, match="not allowed"):
        recorder.call("run_audit", {})
    assert [(r.tool, r.error_cause) for r in recorder.recordings] == [
        (TOOL_LIST_PROJECTS, None),
        (TOOL_GET_AUDIT_REPORT, "unavailable"),
    ]
    assert [tool for tool, _args in session.calls] == [TOOL_LIST_PROJECTS, TOOL_GET_AUDIT_REPORT]


def as_json(value: object) -> Any:
    """The value as plain JSON data, so a test can index it freely."""
    return json.loads(json.dumps(value))


def record_dirty() -> tuple[list[Recording], FakeGuardSession]:
    session = dirty_session()
    recorder = RecordingGuardTools(session)
    listing = list_projects(recorder)
    assert isinstance(listing, ListingOk)
    for project in listing.projects:
        fetch_project(recorder, project)
    return recorder.recordings, session


def test_sanitizer_placeholders_are_consistent_between_arguments_and_responses() -> None:
    recordings, _session = record_dirty()
    fixtures = sanitize_recordings(recordings, tool_arguments={})
    calls = as_json(fixtures.manifest)["calls"]
    files = as_json(fixtures.files)
    repos_call = next(c for c in calls if c["tool"] == TOOL_LIST_REPOSITORIES)
    repos = files[repos_call["response"]]["repositories"]
    assert repos_call["args"] == {ARG_PROJECT_ID: "00000000-0000-4000-8000-00000000a001"}
    assert repos[0]["project_id"] == "00000000-0000-4000-8000-00000000a001"
    assert repos[0]["id"] == "repo_00000000-0000-4000-8000-00000000b001"
    assert repos[0]["name"] == "acme/service-1"
    runs_call = next(c for c in calls if c["tool"] == TOOL_LIST_AUDIT_RUNS)
    assert runs_call["args"] == {
        "project_id": "00000000-0000-4000-8000-00000000a001",
        "repo_id": "repo_00000000-0000-4000-8000-00000000b001",
    }
    runs = files[runs_call["response"]]["runs"]
    assert runs[0]["task_id"] == "00000000-0000-4000-8000-00000000c001"
    assert runs[0]["report_url"] == (
        "https://guard.example/vulns?run=00000000-0000-4000-8000-00000000c001"
    )
    assert "internal_note" not in runs[0]
    assert fixtures.dropped["list_audit_runs.internal_note"] == 1


def test_sanitizer_writes_unique_calls_and_one_page_reports() -> None:
    recordings, _session = record_dirty()
    recordings.append(
        Recording(
            TOOL_GET_AUDIT_REPORT, {ARG_TASK_ID: RAW_SECURITY_TASK, ARG_CURSOR: "p2"}, {}, None
        )
    )
    fixtures = sanitize_recordings(recordings, tool_arguments={"list_projects": ("cursor",)})
    calls = as_json(fixtures.manifest)["calls"]
    files = as_json(fixtures.files)
    keys = [(c["tool"], tuple(sorted(c["args"].items()))) for c in calls]
    assert len(keys) == len(set(keys))
    report_calls = [c for c in calls if c["tool"] == TOOL_GET_AUDIT_REPORT]
    assert len(report_calls) == 2
    page = files[report_calls[0]["response"]]
    assert page["has_more"] is False
    assert page["next_cursor"] is None
    assert fixtures.tools == {"list_projects": ["cursor"]}


def test_sanitizer_records_a_failed_call_as_an_error_entry() -> None:
    recordings = [
        Recording(TOOL_LIST_AUDIT_RUNS, {"project_id": "p1", "repo_id": "r1"}, None, "unavailable")
    ]
    fixtures = sanitize_recordings(recordings, tool_arguments={})
    assert fixtures.files == {}
    assert fixtures.manifest == {
        "calls": [
            {
                "tool": TOOL_LIST_AUDIT_RUNS,
                "args": {
                    "project_id": "00000000-0000-4000-8000-00000000a001",
                    "repo_id": "repo_00000000-0000-4000-8000-00000000b001",
                },
                "error": "recorded failure (unavailable)",
            }
        ]
    }


def test_sanitizer_numbers_list_cursors_and_names() -> None:
    sanitizer = Sanitizer()
    assert sanitizer.cursor("abc") == "<opaque-cursor-1>"
    assert sanitizer.cursor("def") == "<opaque-cursor-2>"
    assert sanitizer.cursor("abc") == "<opaque-cursor-1>"
    assert sanitizer.project_name("Real") == "acme-project-1"
    assert sanitizer.repo_name("a/b") == "acme/service-1"
    assert sanitizer.action_key("audit.security") == "audit.security"
    assert sanitizer.action_key("Weird Key") == "audit.unknown-1"
    assert sanitizer.finding_id("F-007") == "F-007"
    assert sanitizer.finding_id("SQL-INJECTION") == "SLUG-1"
    assert sanitizer.enum("ready") == "ready"
    assert sanitizer.enum("Ready now!") == "<other>"


def test_rebuilt_report_keeps_structure_and_a_new_severity_word() -> None:
    raw = report_markdown(high=1, medium=1, score=40).replace(
        "**Severity:** High", "**Severity:** Critical"
    )
    rebuilt = rebuild_report(
        raw, action_key="audit.security", repo_name="acme/service-1", sanitizer=Sanitizer()
    )
    assert "**Severity:** Critical" in rebuilt
    assert "**Score: 40 / 100**" in rebuilt
    assert "Placeholder finding" not in rebuilt
    report = RawReport("t", "audit.security", raw, "v")
    fixtures = SanitizedFixtures(
        files={"get_audit_report_1.json": {"task_id": "t", "content": rebuilt}},
        manifest={},
        tools={},
        dropped={},
    )
    assert check_round_trip([report], fixtures) == []


def test_summary_lists_severity_words_layouts_and_unknown_reasons() -> None:
    recordings, session = record_dirty()
    recordings.append(Recording(TOOL_LIST_PROJECTS, {}, None, "data"))
    summary = summarize(
        recordings,
        tool_arguments=session.tool_arguments(),
        result_shapes=session.result_shapes(),
        dropped={"list_audit_runs.internal_note": 1},
    )
    text = format_summary(summary)
    assert "severity words: High=1, Medium=1" in text
    assert "report layouts: overview=1, scored=1" in text
    assert "run statuses: completed=2" in text
    assert "failed calls: 1" in text
    assert "dropped fields: list_audit_runs.internal_note=1" in text
    assert "list_projects(cursor)" in text
    unknown = summarize(
        [
            Recording(
                TOOL_LIST_AUDIT_RUNS,
                {"project_id": "p", "repo_id": "r"},
                {"runs": [run_item("t", "audit.x", "2026-11-16T10:00:00.000000Z")]},
                None,
            ),
            Recording(
                TOOL_GET_AUDIT_REPORT,
                {ARG_TASK_ID: "t"},
                {"content": "# T\n\n**Score: 5 / 100**\n", "content_version": "v"},
                None,
            ),
        ],
        tool_arguments={},
        result_shapes={},
        dropped={},
    )
    assert "score below 100 but no findings=1" in format_summary(unknown)


# --- check 1: round trip -----------------------------------------------------------------------

FENCED = """# T

**Score: 50 / 100** · Placeholder

## Security findings

### `F-001` — a

**Severity:** High · **Confidence:** High

```
## not a heading
```

### `F-002` — b

**Severity:** Low · **Confidence:** High
"""


def fixtures_with(task_id: str, content: str) -> SanitizedFixtures:
    return SanitizedFixtures(
        files={"get_audit_report_1.json": {"task_id": task_id, "content": content}},
        manifest={},
        tools={},
        dropped={},
    )


def test_round_trip_identical_structure_has_no_problem() -> None:
    raw = report_markdown(high=2, medium=1, score=70)
    report = RawReport("task-1", "audit.security", raw, "v")
    assert check_round_trip([report], fixtures_with("task-1", raw)) == []


def test_round_trip_fenced_heading_is_a_problem_naming_the_task_and_property() -> None:
    report = RawReport("task-1", "audit.security", FENCED, "v")
    rebuilt = rebuild_report(
        FENCED, action_key="audit.security", repo_name="acme/service-1", sanitizer=Sanitizer()
    )
    problems = check_round_trip([report], fixtures_with("task-1", rebuilt))
    assert problems == ["report task-1: round trip differs in outcome"]


def test_round_trip_missing_findings_heading_is_unknown_on_both_sides() -> None:
    raw = "# T\n\n**Score: 50 / 100** · x\n\n## Other\n\ntext\n"
    rebuilt = rebuild_report(
        raw, action_key="audit.security", repo_name="acme/service-1", sanitizer=Sanitizer()
    )
    report = RawReport("task-1", "audit.security", raw, "v")
    assert check_round_trip([report], fixtures_with("task-1", rebuilt)) == []


def test_round_trip_missing_sanitized_report_is_a_problem() -> None:
    report = RawReport("task-9", "audit.security", report_markdown(high=1), "v")
    assert check_round_trip([report], fixtures_with("task-1", "")) == [
        "report task-9: no sanitized report was written"
    ]


def test_round_trip_outcome_change_is_a_problem() -> None:
    raw = report_markdown(high=1)
    report = RawReport("task-1", "audit.security", raw, "v")
    problems = check_round_trip([report], fixtures_with("task-1", "# T\n"))
    assert problems == ["report task-1: round trip differs in outcome"]


@pytest.mark.parametrize(
    ("raw", "rebuilt_has"),
    [
        ("", ""),
        ("no title here\n\n**Score: 1 / 100**\n", "Placeholder title"),
        ("# T\n\n**Score: 1 / 100**\n**Score: 2 / 100**\n\n## Findings\n", "Score: 2 / 100"),
    ],
)
def test_round_trip_holds_for_odd_reports(raw: str, rebuilt_has: str) -> None:
    rebuilt = rebuild_report(
        raw, action_key="audit.security", repo_name="acme/service-1", sanitizer=Sanitizer()
    )
    assert rebuilt_has in rebuilt
    assert (
        check_round_trip([RawReport("t", "audit.security", raw, "v")], fixtures_with("t", rebuilt))
        == []
    )


# --- check 2: taint ----------------------------------------------------------------------------


def test_taint_collects_names_ids_titles_and_prose_tokens() -> None:
    recordings, _session = record_dirty()
    taint = taint_strings(recordings, kept=Sanitizer().kept)
    for expected in (
        RAW_PROJECT_NAME,
        RAW_REPO_NAME,
        RAW_PROJECT,
        RAW_REPO,
        RAW_SECURITY_TASK,
        "SQL injection in charge endpoint",
        "SQL-INJECTION-IN-CHARGE",
        "src/payments/charge.py:42",
        "jane.doe@realcorp.com",
        RAW_TOKEN,
        RAW_SHA,
        "Significant fixes needed",
    ):
        assert expected in taint


def test_taint_leaves_out_placeholders_kept_values_and_structural_words() -> None:
    recordings = [
        Recording(
            TOOL_GET_AUDIT_REPORT,
            {ARG_TASK_ID: "00000000-0000-4000-8000-00000000c001"},
            {
                "content": "# Title\n\n**Score: 31 / 100** · Placeholder · Confidence: High\n\n"
                "`acme/service-a` · `main` · `0000000000000000000000000000000000000000`\n\n"
                "## Security findings\n\n### `F-001` — x\n\n**Severity:** High\n\n"
                "audit.recon completed 2026-11-10 38/100\n",
                "content_version": "v",
            },
            None,
        )
    ]
    taint = taint_strings(recordings, kept={"audit.recon", "completed", "High"})
    assert "00000000-0000-4000-8000-00000000c001" not in taint
    assert not {"F-001", "Score:", "audit.recon", "2026-11-10", "38/100", "acme/service-a"} & taint
    assert "Severity:**" not in taint


def test_find_taint_is_case_insensitive_and_never_shows_the_whole_string() -> None:
    problems = find_taint(
        {"a.json": 'x "REALCORP/payments-api" y', "b.json": "clean"}, {"realcorp/payments-api"}
    )
    assert problems == ["a.json: raw string rea... (21 chars) survived in the output"]
    assert "payments" not in problems[0]
    assert find_taint({"b.json": "clean"}, {"realcorp"}) == []


def test_find_taint_ignores_scaffolding_words_but_not_data_values() -> None:
    texts = {
        "list_audit_runs_1.json": '{"runs": [{"task_id": "x", "next_cursor": null}]}',
        "tools.json": '{"tools": {"run_audit": {"arguments": ["project_id", "Idempotency"]}}}',
        "calls.json": '{"calls": [{"error": "recorded failure"}]}',
    }
    # prose tokens that quote field names, an error key or a tool word are scaffolding
    assert find_taint(texts, {"task_id", "next_cursor", 'error":', "Idempotency"}) == []
    # a real name that sits in a data value is still a leak
    leaky = {**texts, "list_repositories_1.json": '{"name": "realcorp-payments"}'}
    assert len(find_taint(leaky, {"realcorp-payments", "task_id"})) == 1


# --- check 3: leak scanner ---------------------------------------------------------------------

LEAKS = [
    ("jane.doe@realcorp.com", "email"),
    ("see https://real.example.com/x", "url outside .example"),
    ("http://guard.enji.ai/app", "url outside .example"),
    ("0a1b2c3d-1111-4222-8333-444455556666", "uuid"),
    ("repo_9f8e7d6c-aaaa-4bbb-8ccc-ddddeeeeffff", "uuid"),
    ("repo_abc123", "repo id"),
    ("9f8e7d6c5b4a39281706f5e4d3c2b1a098765432", "40-hex sha"),
    (RAW_TOKEN, "secret pattern github_token"),
    ("AKIAABCDEFGHIJKLMNOP", "secret pattern aws_access_key_id"),
    ("abcdefghijABCDEFGHIJ0123456789", "long token"),
    ("src/app/main.py", "source file path"),
    ("src/payments/charge.py:42", "source file path"),
]


@pytest.mark.parametrize(("text", "detector"), LEAKS)
def test_leak_scanner_flags(text: str, detector: str) -> None:
    problems = scan_for_leaks({"f.json": text})
    assert f"f.json: {detector}" in problems
    assert all(text not in problem for problem in problems)


ACCEPTED = [
    "00000000-0000-4000-8000-00000000a001",
    "repo_00000000-0000-4000-8000-00000000b001",
    "00000000-0000-4000-8000-00000000c012",
    "https://guard.example/vulns?run=00000000-0000-4000-8000-00000000c001",
    "https://git.example/acme/service-1",
    "0000000000000000000000000000000000000000",
    '{"content_version": "' + "ab12" * 16 + '"}',
    "acme/service-1 <opaque-cursor-3> acme-project-2 audit.security",
    "2026-11-10T13:43:19.881Z recorded failure (unavailable)",
]


@pytest.mark.parametrize("text", ACCEPTED)
def test_leak_scanner_accepts_placeholders(text: str) -> None:
    assert scan_for_leaks({"f.json": text}) == []


def test_leak_scanner_flags_a_content_version_that_is_not_a_digest() -> None:
    assert scan_for_leaks({"f.json": '{"content_version": "ab12ab12ab12ab12ab12ab12"}'}) == [
        "f.json: long token"
    ]


# --- refusal through the CLI -------------------------------------------------------------------


def no_leftovers(tmp_path: Path, out_dir: Path) -> bool:
    return not out_dir.exists() and not [p for p in tmp_path.iterdir() if "tmp" in p.name]


def test_probe_refused_when_a_free_text_field_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Sanitizer, "project_name", lambda _self, raw: raw)
    out_dir = tmp_path / "recorded"
    code, output = run_probe(out_dir, dirty_session(), tmp_path)
    assert code == 1
    assert output == ""
    error = capsys.readouterr().err
    assert "error: probe refused to write:" in error
    assert "survived in the output" in error
    assert RAW_PROJECT_NAME not in error
    assert no_leftovers(tmp_path, out_dir)


def test_probe_refused_when_the_leak_scanner_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Sanitizer, "repo_id", lambda _self, raw: raw)
    monkeypatch.setattr(Sanitizer, "project_id", lambda _self, raw: raw)
    out_dir = tmp_path / "recorded"
    code, _output = run_probe(out_dir, dirty_session(), tmp_path)
    assert code == 1
    error = capsys.readouterr().err
    assert "uuid" in error
    assert no_leftovers(tmp_path, out_dir)


def test_probe_refused_when_the_round_trip_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(probe_sanitize, "rebuild_report", lambda *_a, **_k: "# T\n")
    out_dir = tmp_path / "recorded"
    code, _output = run_probe(out_dir, dirty_session(), tmp_path)
    assert code == 1
    assert "round trip differs in outcome" in capsys.readouterr().err
    assert no_leftovers(tmp_path, out_dir)


def test_probe_refused_when_a_write_fails_leaves_no_partial_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Path.write_text
    count = {"n": 0}

    def failing(self: Path, *args: object, **kwargs: object) -> int:
        count["n"] += 1
        if count["n"] == 2:
            raise OSError(28, "No space left on device")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", failing)
    out_dir = tmp_path / "recorded"
    code, _output = run_probe(out_dir, dirty_session(), tmp_path)
    assert code == 1
    assert no_leftovers(tmp_path, out_dir)


# --- safety ------------------------------------------------------------------------------------


def test_probe_calls_only_read_tools(tmp_path: Path) -> None:
    session = dirty_session()
    code, _output = run_probe(tmp_path / "recorded", session, tmp_path)
    assert code == 0
    assert session.calls
    assert {tool for tool, _args in session.calls} <= GUARD_READ_TOOLS


@pytest.mark.parametrize("module", ["probe", "probe_sanitize", "cli_probe"])
def test_probe_modules_never_import_subprocess_or_os_system(module: str) -> None:
    tree = ast.parse((SRC / f"{module}.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert "subprocess" not in imported
    assert "git" not in imported


# --- CLI edges ---------------------------------------------------------------------------------


def test_probe_refuses_a_not_empty_out_before_connecting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "recorded"
    out_dir.mkdir()
    (out_dir / "keep.txt").write_text("mine", encoding="utf-8")
    code, _output = run_probe(out_dir, None, tmp_path)
    assert code == 2
    assert "not empty" in capsys.readouterr().err
    assert (out_dir / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_probe_refuses_an_out_that_is_a_file(tmp_path: Path) -> None:
    target = tmp_path / "recorded"
    target.write_text("x", encoding="utf-8")
    code, _output = run_probe(target, None, tmp_path)
    assert code == 2


def test_probe_accepts_an_empty_existing_out(tmp_path: Path) -> None:
    out_dir = tmp_path / "recorded"
    out_dir.mkdir()
    code, _output = run_probe(out_dir, dirty_session(), tmp_path)
    assert code == 0
    assert (out_dir / "calls.json").is_file()
    assert (out_dir / "tools.json").is_file()
    assert not [p for p in tmp_path.iterdir() if "tmp" in p.name]


def test_probe_listing_unknown_writes_nothing_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    responses = dirty_responses()
    responses[call_key(TOOL_LIST_PROJECTS)] = GuardCallError("down")
    out_dir = tmp_path / "recorded"
    code, output = run_probe(out_dir, FakeGuardSession(responses), tmp_path)
    assert code == 1
    assert output == ""
    assert "nothing was written" in capsys.readouterr().err
    assert no_leftovers(tmp_path, out_dir)


def test_probe_auth_expired_writes_nothing_and_names_the_auth_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    error = GuardAuthError(AuthFailureKind.AUTH_EXPIRED, "expired")
    out_dir = tmp_path / "recorded"
    code, _output = run_probe(out_dir, FakeGuardSession({}, enter_error=error), tmp_path)
    assert code == 1
    assert (
        "error: Guard login expired or revoked: run `enji-guard-radar auth`"
        in capsys.readouterr().err
    )
    assert AUTH_FAILURE_TEXT[AuthFailureKind.AUTH_EXPIRED].startswith("Guard login expired")
    assert no_leftovers(tmp_path, out_dir)


def test_probe_skips_paused_and_excluded_projects(tmp_path: Path) -> None:
    config = tmp_path / "radar.yaml"
    config.write_text(f'projects:\n  "{RAW_PAUSED_PROJECT}":\n    mode: paused\n', encoding="utf-8")
    responses = world(
        projects=((RAW_PROJECT, "One"), (RAW_PAUSED_PROJECT, "Two")),
        repos_by_project={RAW_PROJECT: [], RAW_PAUSED_PROJECT: []},
        runs_by_repo={},
        reports_by_task={},
    )
    session = FakeGuardSession(responses)
    code, _output = run_probe(
        tmp_path / "recorded", session, tmp_path, extra=["--config", str(config)]
    )
    assert code == 0
    assert session.calls
    assert all(RAW_PAUSED_PROJECT not in args.values() for _tool, args in session.calls)
    assert any(args.get(ARG_PROJECT_ID) == RAW_PROJECT for _tool, args in session.calls)


def test_probe_rejects_an_unreadable_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _output = run_probe(
        tmp_path / "recorded", None, tmp_path, extra=["--config", str(tmp_path / "missing.yaml")]
    )
    assert code == 2
    assert "config file not found" in capsys.readouterr().err


def test_probe_output_replays_failures_as_errors(tmp_path: Path) -> None:
    responses = dirty_responses()
    scope = {ARG_PROJECT_ID: RAW_PROJECT, "repo_id": RAW_REPO, ARG_TASK_ID: RAW_RECON_TASK}
    responses[call_key(TOOL_GET_AUDIT_REPORT, **scope)] = GuardCallError("boom")
    out_dir = tmp_path / "recorded"
    code, output = run_probe(out_dir, FakeGuardSession(responses), tmp_path)
    assert code == 0
    manifest = json.loads((out_dir / "calls.json").read_text(encoding="utf-8"))
    errors = [call["error"] for call in manifest["calls"] if "error" in call]
    assert errors == ["recorded failure (unavailable)"]
    assert "failed calls: 1" in output
