"""Markdown audit reports from Guard: parse strictly, read page by page, never guess.

A report that cannot be read with certainty is `ReportUnknown`, never "zero findings"
(SRC-03, SRC-04). Only ids, severities, the score and finding titles are kept, and titles live
in memory only (D-06): never in repr, logs or state. No other report text, evidence path or code
location is stored, returned or logged.
"""

import logging
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from enji_guard_radar.model import Severity
from enji_guard_radar.ports import GuardCallError, GuardTools

logger = logging.getLogger(__name__)

TOOL_GET_AUDIT_REPORT = "get_audit_report"
# ASSUMPTION: argument name confirmed in Phase 2 against the real tool schema
ARG_TASK_ID = "task_id"
ARG_PROJECT_ID = "project_id"
ARG_REPOSITORY_ID = "repo_id"
_ARG_CURSOR = "cursor"
MAX_REPORT_PAGES = 50
MAX_REPORT_CHARS = 2_000_000
OVERVIEW_ACTION_KEYS = frozenset({"audit.recon"})
FINDINGS_HEADINGS = ("## Findings", "## Security findings")

# Guard writes the report in English or Russian per run (OBSERVED 2026-11-19: both, same repo).
SCORE_LINE = re.compile(r"\*\*(?:Score|Оценка): *(\d{1,3}) */ *100\*\*")
_ID = re.compile(r"`([^`]+)`")
SEVERITY_PREFIXES = ("**Severity:**", "**Серьёзность:**")
_SEVERITY_WORDS = {
    "высокая": Severity.HIGH,
    "средняя": Severity.MEDIUM,
    "низкая": Severity.LOW,
    # ASSUMPTION: "Information" (OBSERVED) sits below Low and never matters to a rule, so it is Low.
    "information": Severity.LOW,
    "информация": Severity.LOW,
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReportFinding(_Frozen):
    finding_id: str
    severity: Severity
    severity_raw: str
    # Raw, untrusted Guard text (D-06): in memory only, hidden from repr, never logged or stored.
    title: str = Field(default="", repr=False)


class AuditReport(_Frozen):
    task_id: str
    action_key: str
    layout: Literal["scored", "overview"]
    score: int | None
    findings: tuple[ReportFinding, ...]
    content_version: str

    @property
    def high_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity is Severity.HIGH)


class ReportUnknown(_Frozen):
    task_id: str
    action_key: str
    reason: str


ReportResult = AuditReport | ReportUnknown


class _BadPage(Exception):  # noqa: N818
    """A report page that this reader does not accept. Text is safe to log."""


def parse_severity(word: str) -> Severity:
    """High, Medium, Low and their Russian forms are known; every other word is unknown."""
    cleaned = word.strip().casefold()
    if cleaned in {Severity.HIGH, Severity.MEDIUM, Severity.LOW}:
        return Severity(cleaned)
    return _SEVERITY_WORDS.get(cleaned, Severity.UNKNOWN)


def _unknown(task_id: str, action_key: str, reason: str) -> ReportUnknown:
    logger.warning(
        "report unknown: task_id=%s action_key=%s reason=%s", task_id, action_key, reason
    )
    return ReportUnknown(task_id=task_id, action_key=action_key, reason=reason)


# hyphen, en dash, em dash, colon, whitespace (escaped: ruff flags a literal en dash)
_TITLE_SEPARATORS = "-\u2013\u2014: \t"


def _title(heading: str) -> str:
    """Text after the backticked id of a finding heading; empty when there is no id or no text."""
    match = _ID.search(heading)
    if match is None:
        return ""
    return heading[match.end() :].strip().lstrip(_TITLE_SEPARATORS).strip()


def _finding(task_id: str, block: list[str]) -> ReportFinding:
    id_match = _ID.search(block[0])
    finding_id = id_match.group(1) if id_match else "unknown"
    severity_raw = ""
    for line in block[1:]:
        prefix = next((p for p in SEVERITY_PREFIXES if line.startswith(p)), None)
        if prefix is not None:
            words = line[len(prefix) :].split()
            severity_raw = words[0] if words else ""
            break
    severity = parse_severity(severity_raw)
    if severity is Severity.UNKNOWN:
        logger.warning(
            "report finding severity unknown: task_id=%s finding_id=%s raw=%r",
            task_id,
            finding_id,
            severity_raw,
        )
    return ReportFinding(
        finding_id=finding_id,
        severity=severity,
        severity_raw=severity_raw,
        title=_title(block[0]),
    )


def _finding_blocks(lines: list[str]) -> tuple[list[list[str]], int] | str:
    """Finding blocks (a `###` heading whose next text line is a severity line) and the number of
    severity lines seen anywhere, or the reason the blocks are ambiguous.

    The findings heading is not matched by name: Guard words it many ways (OBSERVED: 23 forms in
    two languages). A finding is recognised by its shape, and every severity line must belong to a
    block that sits in one `##` section, or the report is unknown.
    """
    blocks: list[list[str]] = []
    sections: set[int] = set()
    section = -1
    for index, line in enumerate(lines):
        if line.startswith("## "):
            section += 1
        elif line.startswith("### "):
            following = next((text for text in lines[index + 1 :] if text.strip()), "")
            if following.startswith(SEVERITY_PREFIXES):
                blocks.append([line, following])
                sections.add(section)
    severity_lines = sum(1 for line in lines if line.startswith(SEVERITY_PREFIXES))
    if len(sections) > 1:
        return "findings in more than one section"
    if severity_lines != len(blocks):
        return "severity line outside a finding"
    return blocks, severity_lines


def parse_report_markdown(
    content: str, *, task_id: str, action_key: str, content_version: str
) -> ReportResult:
    """Pure: Markdown in, typed report or ReportUnknown out. Logs ids and reasons only."""
    if not content.strip():
        return _unknown(task_id, action_key, "empty content")
    lines = content.splitlines()
    first = next(line for line in lines if line.strip())
    if not first.startswith("# "):
        return _unknown(task_id, action_key, "no title line")
    if action_key in OVERVIEW_ACTION_KEYS:
        return AuditReport(
            task_id=task_id,
            action_key=action_key,
            layout="overview",
            score=None,
            findings=(),
            content_version=content_version,
        )
    scores = set(SCORE_LINE.findall(content))
    if not scores:
        return _unknown(task_id, action_key, "score line missing")
    if len(scores) > 1:
        return _unknown(task_id, action_key, "score lines differ")
    score = int(next(iter(scores)))
    if score > 100:
        return _unknown(task_id, action_key, "score out of range")
    found = _finding_blocks(lines)
    if isinstance(found, str):
        return _unknown(task_id, action_key, found)
    blocks, _seen = found
    if not blocks and score < 100:
        # Points go only for findings: a lower score with none means a layout we do not know.
        return _unknown(task_id, action_key, "score below 100 but no findings")
    return AuditReport(
        task_id=task_id,
        action_key=action_key,
        layout="scored",
        score=score,
        findings=tuple(_finding(task_id, block) for block in blocks),
        content_version=content_version,
    )


def _check_page(page: Mapping[str, object], task_id: str, first_version: str | None) -> None:
    if page.get("task_id") != task_id:
        message = "page task_id differs from the request"
        raise _BadPage(message)
    status = page.get("report_status")
    if status != "ready":
        shown = status if isinstance(status, str) else "not a string"
        message = f"report not ready: {shown}"
        raise _BadPage(message)
    if not isinstance(page.get("content"), str):
        message = "content is missing or not a string"
        raise _BadPage(message)
    version = page.get("content_version")
    if not isinstance(version, str):
        message = "content_version is missing or not a string"
        raise _BadPage(message)
    if first_version is not None and version != first_version:
        message = "content_version changed between pages"
        raise _BadPage(message)
    has_more = page.get("has_more")
    if not isinstance(has_more, bool):
        message = "has_more is missing or not a boolean"
        raise _BadPage(message)
    if has_more and not isinstance(page.get("next_cursor"), str):
        message = "has_more without next_cursor"
        raise _BadPage(message)


def _read_pages(tools: GuardTools, task_id: str, project_id: str, repo_id: str) -> tuple[str, str]:
    """All pages joined, plus the content_version. Raises _BadPage or GuardCallError."""
    base = {ARG_PROJECT_ID: project_id, ARG_REPOSITORY_ID: repo_id, ARG_TASK_ID: task_id}
    args = dict(base)
    parts: list[str] = []
    total = 0
    version: str | None = None
    for _ in range(MAX_REPORT_PAGES):
        page: object = tools.call(TOOL_GET_AUDIT_REPORT, args)
        if not isinstance(page, Mapping):
            message = "page is not an object"
            raise _BadPage(message)
        _check_page(page, task_id, version)
        content = str(page["content"])
        version = str(page["content_version"])
        total += len(content)
        if total > MAX_REPORT_CHARS:
            message = "report too large"
            raise _BadPage(message)
        parts.append(content)
        if not page["has_more"]:
            return "".join(parts), version
        args = {**base, _ARG_CURSOR: str(page["next_cursor"])}
    message = f"more than {MAX_REPORT_PAGES} pages"
    raise _BadPage(message)


def read_report(
    tools: GuardTools, task_id: str, action_key: str, *, project_id: str, repo_id: str
) -> ReportResult:
    """Read every page of one report and parse it. Any failure is ReportUnknown, never raised."""
    try:
        content, version = _read_pages(tools, task_id, project_id, repo_id)
    except GuardCallError as error:
        return _unknown(task_id, action_key, f"{TOOL_GET_AUDIT_REPORT} failed: {error}")
    except _BadPage as error:
        return _unknown(task_id, action_key, str(error))
    return parse_report_markdown(
        content, task_id=task_id, action_key=action_key, content_version=version
    )
