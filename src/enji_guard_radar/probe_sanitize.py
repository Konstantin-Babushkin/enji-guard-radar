"""`probe` core: turn raw Guard recordings into fixtures that hold no real content, and prove it.

Allowlist, not denylist: per tool only known structural fields are written. Any other field is
dropped and counted, so a new Guard field shows up in the probe summary without leaking. Reports
are rebuilt from their structure with placeholder text. Three checks guard the result before the
shell writes anything: round-trip parse equality, the taint set (raw strings that must not appear)
and a generic leak scanner. All pure: no file, no clock, no network.
"""

import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Final
from urllib.parse import urlsplit

from enji_guard_radar.guard import (
    ARG_CURSOR,
    ARG_PROJECT_ID,
    ARG_REPOSITORY_ID,
    TOOL_LIST_AUDIT_RUNS,
    TOOL_LIST_PROJECTS,
    TOOL_LIST_REPOSITORIES,
    GuardDataError,
    parse_guard_time,
)
from enji_guard_radar.guard_report import (
    ARG_TASK_ID,
    FINDINGS_HEADINGS,
    OVERVIEW_ACTION_KEYS,
    TOOL_GET_AUDIT_REPORT,
    AuditReport,
    parse_report_markdown,
)
from enji_guard_radar.probe import RawReport, Recording, collect_reports
from enji_guard_radar.secret_guard import SECRET_PATTERNS

PLACEHOLDER_PROJECT: Final = "00000000-0000-4000-8000-00000000a{:03d}"
PLACEHOLDER_REPO: Final = "repo_00000000-0000-4000-8000-00000000b{:03d}"
PLACEHOLDER_TASK: Final = "00000000-0000-4000-8000-00000000c{:03d}"
KEPT_SEVERITY_WORD: Final = re.compile(r"[A-Za-z]{1,20}")
MANIFEST_FILE: Final = "calls.json"
TOOLS_FILE: Final = "tools.json"
OTHER: Final = "<other>"
REDACTED_ARGUMENT: Final = "<redacted>"

_ENUM: Final = re.compile(r"[a-z_]{1,20}")
_ACTION_KEY: Final = re.compile(r"audit\.[a-z0-9-]{1,40}")
_FINDING_ID: Final = re.compile(r"F-\d+")
_REPORT_GROUP: Final = re.compile(r"[a-z-]{1,30}")
_SCORE: Final = re.compile(r"\*\*Score: *(\d{1,3}) */ *100\*\*")
_CONFIDENCE_WORD: Final = re.compile(r"Confidence:\**\s*([A-Za-z]{1,20})")
_ID: Final = re.compile(r"`([^`]+)`")
_SEVERITY_PREFIX: Final = "**Severity:**"
_PLACEHOLDER_LINE: Final = "<placeholder>"
_PLACEHOLDER_TITLE: Final = "Placeholder title"
_UNKNOWN_CONFIDENCE: Final = "<confidence>"
_ZERO_SHA: Final = "0" * 40
_REPO_FALLBACK: Final = "acme/service-0"
_OVERVIEW_HEADINGS: Final = (
    "## Executive Summary",
    "## Audited Target and Revision",
    "## What Was Checked",
    "## Main Findings",
    "## Limitations",
    "## What to Do Next",
)
_FENCE: Final = "```"
_PROBLEM_LIMIT: Final = 3  # characters of a tainted string a problem line may show

_BOILERPLATE: Final = "\n".join(
    [
        _PLACEHOLDER_LINE,
        _PLACEHOLDER_TITLE,
        "# Placeholder title",
        "## Executive summary",
        "## Placeholder question",
        "**Score: <n> / 100** · Placeholder · Confidence: <confidence>",
        "**Severity:** · **Confidence:**",
        "Placeholder",
        "acme-project-",
        "acme/service-",
        "audit.unknown-",
        "SLUG-",
        "<opaque-cursor-",
        "https://guard.example/",
        "recorded failure",
        "main",
        "report",
        *FINDINGS_HEADINGS,
        *_OVERVIEW_HEADINGS,
    ]
)

# Strings the sanitizer itself writes (or a fixed form of them): never a leak, never taint.
_PLACEHOLDER_SHAPES: Final = (
    re.compile(r"(?:repo_)?00000000-0000-4000-8000-00000000[abc]\d{3,}"),
    re.compile(r"<opaque-cursor(?:-\d+)?>"),
    re.compile(r"acme-project-\d+"),
    re.compile(r"acme/[a-z0-9-]+"),
    re.compile(r"SLUG-\d+"),
    re.compile(r"F-\d+"),
    re.compile(r"0{7,40}"),
    re.compile(r"[\d\-:.,/]{1,15}"),
)

_TAINT_MIN_CHARS: Final = 4
_TAINT_KEY_SUFFIXES: Final = ("_id", "_url", "_cursor", "_name")
_TAINT_KEYS: Final = frozenset({"id", "name", "url", "cursor"})
_PROSE_SPECIALS: Final = frozenset("/.:_@")
_TOKEN_STRIP: Final = "\"'()[]<>{}.,;*`!?"
_LABEL: Final = re.compile(r"100\*\*\s*·\s*([^·]+?)\s*(?:·|$)")

_EMAIL: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")
_URL: Final = re.compile(r"https?://([^\s/\"\\?#]+)")
_UUID: Final = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_REPO_ID: Final = re.compile(r"\brepo_(?!id\b)[0-9A-Za-z-]+")  # not the argument name repo_id
_SHA: Final = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{40}(?![0-9A-Za-z])")
_LONG_RUN: Final = re.compile(r"[A-Za-z0-9+]{20,}|[A-Za-z0-9+/]{40,}")
_SOURCE_PATH: Final = re.compile(
    r"(?:[\w.-]+/)+[\w.-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|rb|php|c|h|cpp|cs|kt|swift|md|"
    r"json|ya?ml|toml|sh|sql|html|css|txt)\b"
)
_CONTENT_VERSION: Final = re.compile(r'"content_version":\s*"[0-9a-f]{64}"')
_PLACEHOLDER_UUID: Final = re.compile(r"00000000-0000-4000-8000-00000000[abc]\d{3,}")


# --- stable placeholders -----------------------------------------------------------------------


class Sanitizer:
    """Stateful maps from raw values to placeholders, numbered by first appearance."""

    def __init__(self) -> None:
        self._tables: dict[str, dict[str, str]] = {}
        self.kept: set[str] = set()

    def _assign(self, kind: str, raw: str, make: Callable[[int], str]) -> str:
        table = self._tables.setdefault(kind, {})
        if raw not in table:
            table[raw] = make(len(table) + 1)
        return table[raw]

    def project_id(self, raw: str) -> str:
        return self._assign("project_id", raw, PLACEHOLDER_PROJECT.format)

    def repo_id(self, raw: str) -> str:
        return self._assign("repo_id", raw, PLACEHOLDER_REPO.format)

    def task_id(self, raw: str) -> str:
        return self._assign("task_id", raw, PLACEHOLDER_TASK.format)

    def cursor(self, raw: str) -> str:
        return self._assign("cursor", raw, "<opaque-cursor-{}>".format)

    def project_name(self, raw: str) -> str:
        return self._assign("project_name", raw, "acme-project-{}".format)

    def repo_name(self, raw: str) -> str:
        return self._assign("repo_name", raw, "acme/service-{}".format)

    def action_key(self, raw: str) -> str:
        if _ACTION_KEY.fullmatch(raw):
            self.kept.add(raw)
            return raw
        return self._assign("action_key", raw, "audit.unknown-{}".format)

    def finding_id(self, raw: str) -> str:
        if _FINDING_ID.fullmatch(raw):
            self.kept.add(raw)
            return raw
        return self._assign("finding_id", raw, "SLUG-{}".format)

    def enum(self, raw: str) -> str:
        if _ENUM.fullmatch(raw):
            self.kept.add(raw)
            return raw
        return OTHER

    def word(self, raw: str) -> str:
        """A severity or confidence word: kept when it is one word of letters."""
        if KEPT_SEVERITY_WORD.fullmatch(raw):
            self.kept.add(raw)
            return raw
        return OTHER


# --- report rebuild ----------------------------------------------------------------------------


def _outside_fences(lines: Sequence[str]) -> list[str]:
    """Lines outside fenced code blocks; the fence lines themselves are left out."""
    kept: list[str] = []
    inside = False
    for line in lines:
        if line.lstrip().startswith(_FENCE):
            inside = not inside
            continue
        if not inside:
            kept.append(line)
    return kept


@dataclass
class _Finding:
    heading_id: str | None
    severity: str | None = None
    confidence: str | None = None


def _emit_finding(finding: _Finding, sanitizer: Sanitizer) -> list[str]:
    heading = (
        f"### `{sanitizer.finding_id(finding.heading_id)}` — {_PLACEHOLDER_TITLE}"
        if finding.heading_id is not None
        else f"### {_PLACEHOLDER_TITLE}"
    )
    lines = [heading, ""]
    if finding.severity is not None:
        severity = (
            f"{_SEVERITY_PREFIX} {sanitizer.word(finding.severity)}"
            if finding.severity
            else _SEVERITY_PREFIX
        )
        if finding.confidence is not None:
            severity += f" · **Confidence:** {sanitizer.word(finding.confidence)}"
        lines += [severity, ""]
    return [*lines, _PLACEHOLDER_LINE, ""]


def _confidence_word(line: str) -> str | None:
    match = re.search(r"\*\*Confidence:\*\*\s*(\S+)", line)
    return match.group(1) if match else None


def rebuild_report(content: str, *, action_key: str, repo_name: str, sanitizer: Sanitizer) -> str:
    """The report layout with placeholder text; only what the parser reads is carried over."""
    if not content.strip():
        return ""
    lines = content.splitlines()
    first = next(line for line in lines if line.strip())
    title = f"# {_PLACEHOLDER_TITLE}" if first.startswith("# ") else _PLACEHOLDER_TITLE
    if action_key in OVERVIEW_ACTION_KEYS:
        out = [title, ""]
        for heading in _OVERVIEW_HEADINGS:
            out += [heading, "", _PLACEHOLDER_LINE, ""]
        return "\n".join(out) + "\n"

    out = [title, "", "## Executive summary", "", _PLACEHOLDER_LINE, ""]
    out += ["## Placeholder question", "", _PLACEHOLDER_LINE, ""]
    for line in lines:  # the parser counts score lines everywhere, fenced or not
        for match in _SCORE.finditer(line):
            confidence = _CONFIDENCE_WORD.search(line[match.end() :])
            word = sanitizer.word(confidence.group(1)) if confidence else _UNKNOWN_CONFIDENCE
            out += [f"**Score: {match.group(1)} / 100** · Placeholder · Confidence: {word}", ""]
    out += [f"`{repo_name}` · `main` · `{_ZERO_SHA}`", ""]

    in_findings = False
    finding: _Finding | None = None
    for line in _outside_fences(lines):
        if line.rstrip() in FINDINGS_HEADINGS:
            if finding is not None:
                out += _emit_finding(finding, sanitizer)
                finding = None
            out += [line.rstrip(), "", _PLACEHOLDER_LINE, ""]
            in_findings = True
        elif line.startswith("## "):
            if finding is not None:
                out += _emit_finding(finding, sanitizer)
                finding = None
            in_findings = False
        elif not in_findings:
            continue
        elif line.startswith("### "):
            if finding is not None:
                out += _emit_finding(finding, sanitizer)
            id_match = _ID.search(line)
            finding = _Finding(heading_id=id_match.group(1) if id_match else None)
        elif finding is not None and finding.severity is None and line.startswith(_SEVERITY_PREFIX):
            words = line[len(_SEVERITY_PREFIX) :].split()
            finding.severity = words[0] if words else ""
            finding.confidence = _confidence_word(line)
    if finding is not None:
        out += _emit_finding(finding, sanitizer)
    return "\n".join(out) + "\n"


# --- sanitizing --------------------------------------------------------------------------------


@dataclass(frozen=True)
class SanitizedFixtures:
    """What the shell may write once the checks pass, plus what the checks need."""

    files: dict[str, dict[str, object]]
    manifest: dict[str, object]
    tools: dict[str, list[str]]
    dropped: dict[str, int]
    kept: frozenset[str] = frozenset()
    reports: tuple[RawReport, ...] = field(default=())


def render_files(fixtures: SanitizedFixtures) -> dict[str, str]:
    """Every file of the fixture directory as text, by file name."""
    texts = {name: _dump(payload) for name, payload in fixtures.files.items()}
    texts[MANIFEST_FILE] = _dump(fixtures.manifest)
    texts[TOOLS_FILE] = _dump(fixtures.tools)
    return texts


def _dump(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _map_args(args: Mapping[str, str], sanitizer: Sanitizer) -> dict[str, str]:
    mappers: dict[str, Callable[[str], str]] = {
        ARG_PROJECT_ID: sanitizer.project_id,
        ARG_REPOSITORY_ID: sanitizer.repo_id,
        ARG_TASK_ID: sanitizer.task_id,
        ARG_CURSOR: sanitizer.cursor,
    }
    return {
        name: mappers[name](value) if name in mappers else REDACTED_ARGUMENT
        for name, value in args.items()
    }


@dataclass
class _Walk:
    """The state of one sanitizing pass."""

    sanitizer: Sanitizer = field(default_factory=Sanitizer)
    dropped: Counter[str] = field(default_factory=Counter)
    repo_name_by_id: dict[str, str] = field(default_factory=dict)
    repo_name_by_task: dict[str, str] = field(default_factory=dict)

    def drop_unknown(self, tool: str, item: Mapping[str, object], allowed: Iterable[str]) -> None:
        allowed_names = set(allowed)
        for name in item:
            if name not in allowed_names:
                self.dropped[f"{tool}.{name}"] += 1

    def paging(self, tool: str, response: Mapping[str, object], list_key: str) -> dict[str, object]:
        self.drop_unknown(tool, response, (list_key, "has_more", "next_cursor"))
        out: dict[str, object] = {}
        has_more = response.get("has_more")
        if isinstance(has_more, bool):
            out["has_more"] = has_more
        cursor = response.get("next_cursor")
        if isinstance(cursor, str):
            out["next_cursor"] = self.sanitizer.cursor(cursor)
        elif "next_cursor" in response and cursor is None:
            out["next_cursor"] = None
        return out

    def items(self, response: Mapping[str, object], key: str) -> list[Mapping[str, object]] | None:
        value = response.get(key)
        if not isinstance(value, list):
            return None
        return [item for item in value if isinstance(item, Mapping)]

    def text(self, item: Mapping[str, object], key: str, mapper: Callable[[str], str]) -> object:
        value = item.get(key)
        return mapper(value) if isinstance(value, str) else None

    def timestamp(self, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                parse_guard_time(value)
            except GuardDataError:
                return "invalid-timestamp"
            self.sanitizer.kept.add(value)
            return value
        return None

    def projects(self, response: Mapping[str, object]) -> dict[str, object]:
        out = self.paging(TOOL_LIST_PROJECTS, response, "projects")
        items = self.items(response, "projects")
        if items is not None:
            rows: list[dict[str, object]] = []
            for item in items:
                self.drop_unknown(TOOL_LIST_PROJECTS, item, ("id", "name"))
                rows.append(
                    _present(
                        id=self.text(item, "id", self.sanitizer.project_id),
                        name=self.text(item, "name", self.sanitizer.project_name),
                    )
                )
            out = {"projects": rows, **out}
        return out

    def repositories(self, response: Mapping[str, object]) -> dict[str, object]:
        out = self.paging(TOOL_LIST_REPOSITORIES, response, "repositories")
        items = self.items(response, "repositories")
        if items is not None:
            rows: list[dict[str, object]] = []
            for item in items:
                self.drop_unknown(
                    TOOL_LIST_REPOSITORIES,
                    item,
                    ("id", "project_id", "name", "provider", "connected"),
                )
                name = self.text(item, "name", self.sanitizer.repo_name)
                raw_id = item.get("id")
                if isinstance(raw_id, str) and isinstance(name, str):
                    self.repo_name_by_id[raw_id] = name
                connected = item.get("connected")
                rows.append(
                    _present(
                        id=self.text(item, "id", self.sanitizer.repo_id),
                        project_id=self.text(item, "project_id", self.sanitizer.project_id),
                        name=name,
                        provider=self.text(item, "provider", self.sanitizer.enum),
                        connected=connected if isinstance(connected, bool) else None,
                    )
                )
            out = {"repositories": rows, **out}
        return out

    def runs(self, response: Mapping[str, object], repository_id: str) -> dict[str, object]:
        out = self.paging(TOOL_LIST_AUDIT_RUNS, response, "runs")
        items = self.items(response, "runs")
        if items is not None:
            rows: list[dict[str, object]] = []
            repo_name = self.repo_name_by_id.get(repository_id, _REPO_FALLBACK)
            for item in items:
                self.drop_unknown(
                    TOOL_LIST_AUDIT_RUNS,
                    item,
                    (
                        "task_id",
                        "action_key",
                        "status",
                        "report_status",
                        "created_at",
                        "linked_at",
                        "completed_at",
                        "task_available",
                        "report_url",
                    ),
                )
                raw_task = item.get("task_id")
                if isinstance(raw_task, str):
                    self.repo_name_by_task[raw_task] = repo_name
                available = item.get("task_available")
                row = _present(
                    task_id=self.text(item, "task_id", self.sanitizer.task_id),
                    action_key=self.text(item, "action_key", self.sanitizer.action_key),
                    status=self.text(item, "status", self.sanitizer.enum),
                    report_status=self.text(item, "report_status", self.sanitizer.enum),
                    created_at=self.timestamp(item.get("created_at")),
                    linked_at=self.timestamp(item.get("linked_at")),
                    task_available=available if isinstance(available, bool) else None,
                )
                if "completed_at" in item:
                    row["completed_at"] = self.timestamp(item.get("completed_at"))
                report_url = item.get("report_url")
                if isinstance(report_url, str) and isinstance(raw_task, str):
                    row["report_url"] = self.report_url(report_url, raw_task)
                rows.append(row)
            out = {"runs": rows, **out}
        return out

    def report_url(self, raw_url: str, raw_task: str) -> str:
        segments = [part for part in urlsplit(raw_url).path.split("/") if part]
        group = segments[-1] if segments and _REPORT_GROUP.fullmatch(segments[-1]) else "report"
        return f"https://guard.example/{group}?run={self.sanitizer.task_id(raw_task)}"

    def report(
        self, response: Mapping[str, object] | None, raw: RawReport | None, raw_task: str
    ) -> dict[str, object]:
        out: dict[str, object] = {"task_id": self.sanitizer.task_id(raw_task)}
        if response is not None:
            self.drop_unknown(
                TOOL_GET_AUDIT_REPORT,
                response,
                (
                    "task_id",
                    "report_status",
                    "content",
                    "content_version",
                    "has_more",
                    "next_cursor",
                ),
            )
            status = response.get("report_status")
            if isinstance(status, str):
                out["report_status"] = self.sanitizer.enum(status)
        if raw is not None:
            repo_name = self.repo_name_by_task.get(raw_task, _REPO_FALLBACK)
            content = rebuild_report(
                raw.content,
                action_key=self.sanitizer.action_key(raw.action_key),
                repo_name=repo_name,
                sanitizer=self.sanitizer,
            )
            out["content"] = content
            out["content_version"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
            out["has_more"] = False
            out["next_cursor"] = None
        return out


def _present(**fields: object) -> dict[str, object]:
    """The fields that have a value; a field the raw answer lacked stays absent."""
    return {name: value for name, value in fields.items() if value is not None}


def sanitize_recordings(
    recordings: list[Recording], *, tool_arguments: Mapping[str, tuple[str, ...]]
) -> SanitizedFixtures:
    walk = _Walk()
    sanitizer = walk.sanitizer
    raw_reports = {report.task_id: report for report in collect_reports(recordings)}
    report_errors: dict[str, str] = {}
    for recording in recordings:
        task = recording.args.get(ARG_TASK_ID)
        if recording.tool == TOOL_GET_AUDIT_REPORT and task is not None and recording.error_cause:
            report_errors.setdefault(task, recording.error_cause)

    files: dict[str, dict[str, object]] = {}
    entries: list[dict[str, object]] = []
    counters: Counter[str] = Counter()
    seen: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    mapped_reports: list[RawReport] = []

    for recording in recordings:
        tool = recording.tool
        raw_task = recording.args.get(ARG_TASK_ID, "")
        if tool == TOOL_GET_AUDIT_REPORT:
            if ARG_CURSOR in recording.args:
                continue  # later pages are folded into the first call's content
            args = _map_args(
                {k: v for k, v in recording.args.items() if k != ARG_CURSOR}, sanitizer
            )
        else:
            args = _map_args(recording.args, sanitizer)
        key = (tool, tuple(sorted(args.items())))
        if key in seen:
            continue
        seen.add(key)

        failure = recording.error_cause or report_errors.get(raw_task)
        if failure is not None:
            entries.append({"tool": tool, "args": args, "error": f"recorded failure ({failure})"})
            continue
        payload = _payload(walk, recording, raw_reports.get(raw_task))
        if payload is None:
            continue
        if tool == TOOL_GET_AUDIT_REPORT and raw_task in raw_reports:
            raw = raw_reports[raw_task]
            mapped_reports.append(
                replace(
                    raw,
                    task_id=sanitizer.task_id(raw_task),
                    action_key=sanitizer.action_key(raw.action_key),
                )
            )
        counters[tool] += 1
        name = f"{tool}_{counters[tool]}.json"
        files[name] = payload
        entries.append({"tool": tool, "args": args, "response": name})

    tools = {name: sorted(arguments) for name, arguments in sorted(tool_arguments.items())}
    return SanitizedFixtures(
        files=files,
        manifest={"calls": entries},
        tools=tools,
        dropped=dict(walk.dropped),
        kept=frozenset(sanitizer.kept),
        reports=tuple(mapped_reports),
    )


def _payload(
    walk: _Walk, recording: Recording, raw_report: RawReport | None
) -> dict[str, object] | None:
    response = recording.response
    if response is None:
        return None
    if recording.tool == TOOL_LIST_PROJECTS:
        return walk.projects(response)
    if recording.tool == TOOL_LIST_REPOSITORIES:
        return walk.repositories(response)
    if recording.tool == TOOL_LIST_AUDIT_RUNS:
        return walk.runs(response, recording.args.get(ARG_REPOSITORY_ID, ""))
    if recording.tool == TOOL_GET_AUDIT_REPORT:
        return walk.report(response, raw_report, recording.args.get(ARG_TASK_ID, ""))
    return None


# --- check 1: round trip -----------------------------------------------------------------------


def _signature(content: str, report: RawReport) -> tuple[object, ...]:
    parsed = parse_report_markdown(
        content,
        task_id=report.task_id,
        action_key=report.action_key,
        content_version=report.content_version,
    )
    if isinstance(parsed, AuditReport):
        return (
            "known",
            parsed.layout,
            parsed.score,
            len(parsed.findings),
            tuple(finding.severity for finding in parsed.findings),
            parsed.high_count,
        )
    return ("unknown", parsed.reason)


_PROPERTIES: Final = ("outcome", "layout", "score", "finding count", "severities", "High count")


def check_round_trip(raw_reports: Sequence[RawReport], sanitized: SanitizedFixtures) -> list[str]:
    """Raw and sanitized Markdown must parse the same. `raw_reports` carry the mapped task id."""
    rebuilt = {
        payload["task_id"]: payload["content"]
        for payload in sanitized.files.values()
        if isinstance(payload.get("task_id"), str) and isinstance(payload.get("content"), str)
    }
    problems: list[str] = []
    for report in raw_reports:
        content = rebuilt.get(report.task_id)
        if not isinstance(content, str):
            problems.append(f"report {report.task_id}: no sanitized report was written")
            continue
        before = _signature(report.content, report)
        after = _signature(content, report)
        if before == after:
            continue
        if before[0] != after[0] or before[0] == "unknown":
            differs = "outcome"
        else:
            differs = next(
                _PROPERTIES[index + 1]
                for index in range(len(before) - 1)
                if before[index + 1] != after[index + 1]
            )
        problems.append(f"report {report.task_id}: round trip differs in {differs}")
    return problems


# --- check 2: taint ----------------------------------------------------------------------------


def _collect_fields(value: object, found: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, str):
                if key in _TAINT_KEYS or str(key).endswith(_TAINT_KEY_SUFFIXES):
                    found.add(item)
            else:
                _collect_fields(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_fields(item, found)


def _collect_prose(content: str, found: set[str]) -> None:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            found.add(heading)
            if " — " in heading:
                found.add(heading.split(" — ", 1)[1])
            if stripped.startswith("###"):
                found.update(
                    value for value in _ID.findall(stripped) if not _FINDING_ID.fullmatch(value)
                )
        label = _LABEL.search(line)
        if label:
            found.add(label.group(1))
        if "`" in line and "·" in line:
            found.add(stripped)
        for raw_token in stripped.split():
            token = raw_token.strip(_TOKEN_STRIP)
            if len(token) >= _TAINT_MIN_CHARS and (
                any(char in _PROSE_SPECIALS or char.isdigit() for char in token)
            ):
                found.add(token)


def taint_strings(recordings: list[Recording], *, kept: Iterable[str]) -> frozenset[str]:
    """Raw strings that must not appear in any output file, minus what the sanitizer kept."""
    found: set[str] = set()
    for recording in recordings:
        found.update(recording.args.values())
        if recording.response is not None:
            _collect_fields(recording.response, found)
    for report in collect_reports(recordings):
        _collect_prose(report.content, found)
    allowed = (_BOILERPLATE + "\n" + "\n".join(kept)).casefold()
    return frozenset(
        value
        for value in found
        if len(value) >= _TAINT_MIN_CHARS
        and value.casefold() not in allowed
        and not any(shape.fullmatch(value) for shape in _PLACEHOLDER_SHAPES)
    )


def find_taint(texts: Mapping[str, str], taint: Iterable[str]) -> list[str]:
    """Problems, one per file and tainted string; a tainted string is never shown in full."""
    problems: list[str] = []
    lowered = {name: text.casefold() for name, text in texts.items()}
    for value in sorted(taint):
        needle = value.casefold()
        for name in sorted(lowered):
            if needle in lowered[name]:
                problems.append(
                    f"{name}: raw string {value[:_PROBLEM_LIMIT]}... ({len(value)} chars) "
                    "survived in the output"
                )
    return problems


# --- check 3: leak scanner ---------------------------------------------------------------------


def _leaks_in(text: str) -> list[str]:
    found: list[str] = []
    scrubbed = _CONTENT_VERSION.sub('"content_version": ""', text)
    if _EMAIL.search(scrubbed):
        found.append("email")
    if any(not host.endswith(".example") for host in _URL.findall(scrubbed)):
        found.append("url outside .example")
    if any(not _PLACEHOLDER_UUID.fullmatch(value) for value in _UUID.findall(scrubbed)):
        found.append("uuid")
    if any(
        not _PLACEHOLDER_UUID.fullmatch(value.removeprefix("repo_"))
        for value in _REPO_ID.findall(scrubbed)
    ):
        found.append("repo id")
    if any(set(value) != {"0"} for value in _SHA.findall(scrubbed)):
        found.append("40-hex sha")
    found.extend(
        f"secret pattern {name}" for name, pattern in SECRET_PATTERNS if pattern.search(scrubbed)
    )
    without_sha = _SHA.sub("", scrubbed)
    if _LONG_RUN.search(without_sha):
        found.append("long token")
    if _SOURCE_PATH.search(scrubbed):
        found.append("source file path")
    return found


def scan_for_leaks(files: Mapping[str, str]) -> list[str]:
    """Generic detectors over file texts. Problems name the file and detector, never the match."""
    return [
        f"{name}: {detector}"
        for name, text in sorted(files.items())
        for detector in _leaks_in(text)
    ]
