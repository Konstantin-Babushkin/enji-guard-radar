"""`probe` core: record the Guard walk, and summarise what Guard really returned.

Pure: the recorder wraps any `GuardTools`, the summary is built from the recordings. Nothing
here touches the disk. The summary holds counts and shape words only, never a name, an id or any
report text.
"""

import logging
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from enji_guard_radar.guard import GUARD_READ_TOOLS, TOOL_LIST_AUDIT_RUNS
from enji_guard_radar.guard_report import (
    ARG_TASK_ID,
    TOOL_GET_AUDIT_REPORT,
    AuditReport,
    parse_report_markdown,
)
from enji_guard_radar.ports import GuardCallError, GuardTools

logger = logging.getLogger(__name__)

OTHER_WORD: Final = "<other>"
_WORD: Final = re.compile(r"[A-Za-z_]{1,20}")
_NONE: Final = "none"
_UNKNOWN_ACTION_KEY: Final = "audit.unknown"
_ARG_CURSOR: Final = "cursor"


def word_shape(value: object) -> str:
    """The value when it is one short word of letters and underscores, else a fixed marker."""
    return value if isinstance(value, str) and _WORD.fullmatch(value) else OTHER_WORD


@dataclass(frozen=True)
class Recording:
    """One Guard call: its answer, or the failure cause when it failed."""

    tool: str
    args: dict[str, str]
    response: Mapping[str, object] | None
    error_cause: str | None


class RecordingGuardTools:
    """Passes every call to `inner` and keeps what came back. Offers no tool of its own."""

    def __init__(self, inner: GuardTools) -> None:
        self._inner = inner
        self.recordings: list[Recording] = []

    def call(self, tool: str, args: Mapping[str, str]) -> Mapping[str, object]:
        if tool not in GUARD_READ_TOOLS:
            message = f"tool not allowed: {tool}"
            raise GuardCallError(message)
        try:
            response = self._inner.call(tool, args)
        except GuardCallError as error:
            self.recordings.append(Recording(tool, dict(args), None, error.cause.value))
            raise
        self.recordings.append(Recording(tool, dict(args), response, None))
        return response


@dataclass(frozen=True)
class RawReport:
    """A report as the parser sees it: all pages joined. Lives in memory only."""

    task_id: str
    action_key: str
    content: str
    content_version: str


def _action_keys(recordings: list[Recording]) -> dict[str, str]:
    keys: dict[str, str] = {}
    for recording in recordings:
        if recording.tool != TOOL_LIST_AUDIT_RUNS or recording.response is None:
            continue
        runs = recording.response.get("runs")
        if not isinstance(runs, list):
            continue
        for run in runs:
            if isinstance(run, Mapping):
                task_id, action_key = run.get("task_id"), run.get("action_key")
                if isinstance(task_id, str) and isinstance(action_key, str):
                    keys[task_id] = action_key
    return keys


def collect_reports(recordings: list[Recording]) -> tuple[RawReport, ...]:
    """Every report whose calls all succeeded, pages joined in the order they were read."""
    action_keys = _action_keys(recordings)
    parts: dict[str, list[str]] = {}
    versions: dict[str, str] = {}
    broken: set[str] = set()
    for recording in recordings:
        if recording.tool != TOOL_GET_AUDIT_REPORT:
            continue
        task_id = recording.args.get(ARG_TASK_ID)
        if task_id is None:
            continue
        page = recording.response
        content = page.get("content") if page is not None else None
        if not isinstance(content, str):
            broken.add(task_id)
            continue
        parts.setdefault(task_id, []).append(content)
        version = page.get("content_version") if page is not None else None
        versions.setdefault(task_id, version if isinstance(version, str) else "")
    return tuple(
        RawReport(
            task_id,
            action_keys.get(task_id, _UNKNOWN_ACTION_KEY),
            "".join(pages),
            versions.get(task_id, ""),
        )
        for task_id, pages in parts.items()
        if task_id not in broken
    )


@dataclass(frozen=True)
class ProbeSummary:
    """What the operator learns about Guard from one probe: counts and words, never content."""

    calls: dict[str, int] = field(default_factory=dict)
    failed_calls: int = 0
    run_statuses: dict[str, int] = field(default_factory=dict)
    report_statuses: dict[str, int] = field(default_factory=dict)
    severity_words: dict[str, int] = field(default_factory=dict)
    layouts: dict[str, int] = field(default_factory=dict)
    unknown_reasons: dict[str, int] = field(default_factory=dict)
    result_shapes: dict[str, int] = field(default_factory=dict)
    tool_arguments: dict[str, tuple[str, ...]] = field(default_factory=dict)
    dropped: dict[str, int] = field(default_factory=dict)


def summarize(
    recordings: list[Recording],
    *,
    tool_arguments: Mapping[str, tuple[str, ...]],
    result_shapes: Mapping[str, int],
    dropped: Mapping[str, int],
) -> ProbeSummary:
    calls: Counter[str] = Counter()
    run_statuses: Counter[str] = Counter()
    report_statuses: Counter[str] = Counter()
    for recording in recordings:
        calls[recording.tool] += 1
        if recording.tool != TOOL_LIST_AUDIT_RUNS or recording.response is None:
            continue
        runs = recording.response.get("runs")
        for run in runs if isinstance(runs, list) else []:
            if isinstance(run, Mapping):
                run_statuses[word_shape(run.get("status"))] += 1
                report_statuses[word_shape(run.get("report_status"))] += 1

    severity_words: Counter[str] = Counter()
    layouts: Counter[str] = Counter()
    unknown_reasons: Counter[str] = Counter()
    for report in collect_reports(recordings):
        parsed = parse_report_markdown(
            report.content,
            task_id=report.task_id,
            action_key=report.action_key,
            content_version=report.content_version,
        )
        if isinstance(parsed, AuditReport):
            layouts[parsed.layout] += 1
            for finding in parsed.findings:
                severity_words[word_shape(finding.severity_raw)] += 1
        else:
            layouts["unknown"] += 1
            unknown_reasons[parsed.reason] += 1
    return ProbeSummary(
        calls=dict(calls),
        failed_calls=sum(1 for recording in recordings if recording.response is None),
        run_statuses=dict(run_statuses),
        report_statuses=dict(report_statuses),
        severity_words=dict(severity_words),
        layouts=dict(layouts),
        unknown_reasons=dict(unknown_reasons),
        result_shapes=dict(result_shapes),
        tool_arguments={name: tuple(args) for name, args in tool_arguments.items()},
        dropped=dict(dropped),
    )


def _counts(values: Mapping[str, int]) -> str:
    return ", ".join(f"{name}={values[name]}" for name in sorted(values)) or _NONE


def format_summary(summary: ProbeSummary) -> str:
    arguments = (
        "; ".join(
            f"{tool}({', '.join(sorted(summary.tool_arguments[tool]))})"
            for tool in sorted(summary.tool_arguments)
        )
        or _NONE
    )
    lines = [
        "probe summary",
        f"calls per tool: {_counts(summary.calls)}",
        f"failed calls: {summary.failed_calls}",
        f"run statuses: {_counts(summary.run_statuses)}",
        f"report statuses: {_counts(summary.report_statuses)}",
        f"severity words: {_counts(summary.severity_words)}",
        f"report layouts: {_counts(summary.layouts)}",
        f"unknown report reasons: {_counts(summary.unknown_reasons)}",
        f"result shapes: {_counts(summary.result_shapes)}",
        f"tool arguments: {arguments}",
        f"dropped fields: {_counts(summary.dropped)}",
    ]
    return "\n".join(lines) + "\n"
