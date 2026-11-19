"""Anti-corruption layer for Guard JSON lists: parse, validate, page, never guess.

Anything unexpected becomes an "unknown" result (SRC-04), never a default that a rule
could read as "no runs". Only a well-formed empty list is a real empty answer.
"""

import logging
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, ConfigDict

from enji_guard_radar.guard_report import TOOL_GET_AUDIT_REPORT, ReportResult, read_report
from enji_guard_radar.ports import (
    AuthFailureKind,
    GuardAuthError,
    GuardCallError,
    GuardFailure,
    GuardTools,
)

logger = logging.getLogger(__name__)

TOOL_LIST_PROJECTS = "list_projects"
TOOL_LIST_REPOSITORIES = "list_repositories"
TOOL_LIST_AUDIT_RUNS = "list_audit_runs"
# The Guard tools the radar may call. `run_audit` (scope audits:run) starts work, is never used
# and is deliberately absent; list_audits and get_audit are opted out until a rule needs them.
GUARD_READ_TOOLS = frozenset(
    {TOOL_LIST_PROJECTS, TOOL_LIST_REPOSITORIES, TOOL_LIST_AUDIT_RUNS, TOOL_GET_AUDIT_REPORT}
)
# ASSUMPTION: argument names are not recorded in docs/guard-data-notes.md; Phase 2 confirms
# them against the real tool schemas
ARG_PROJECT_ID = "project_id"
ARG_REPOSITORY_ID = "repo_id"
ARG_CURSOR = "cursor"
MAX_PAGES = 50

# (repository id, action key, task id) -> True when that run's report must not be read.
SkipReport = Callable[[str, str, str], bool]
# ASSUMPTION: no run takes longer than 24 h from link to completion (OBSERVED 5 min to about 2 h)
MAX_RUN_DURATION = timedelta(hours=24)

_TIME_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})"
)


class GuardDataError(Exception):
    """Guard failed or answered with data this reader rejects. Text is safe to log.

    `cause` says why (login, unavailable or bad data); `auth_kind` is set only for a login failure.
    """

    def __init__(
        self,
        message: str,
        *,
        cause: GuardFailure = GuardFailure.DATA,
        auth_kind: AuthFailureKind | None = None,
    ) -> None:
        super().__init__(message)
        self.cause = cause
        self.auth_kind = auth_kind


class RunStatus(StrEnum):
    COMPLETED = "completed"
    UNKNOWN = "unknown"


class ReportStatus(StrEnum):
    READY = "ready"
    PENDING = "pending"
    UNKNOWN = "unknown"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GuardProject(_Frozen):
    id: str
    name: str


class GuardRepository(_Frozen):
    id: str
    project_id: str
    name: str
    provider: str
    connected: bool


class AuditRun(_Frozen):
    task_id: str
    action_key: str
    status: RunStatus
    status_raw: str
    report_status: ReportStatus
    report_status_raw: str
    created_at: AwareDatetime
    linked_at: AwareDatetime
    completed_at: AwareDatetime | None
    task_available: bool | None = None

    @property
    def counts(self) -> bool:
        """SRC-03: only a completed run with a ready report and a completion time counts."""
        return (
            self.status is RunStatus.COMPLETED
            and self.report_status is ReportStatus.READY
            and self.completed_at is not None
        )


class RepoRuns(_Frozen):
    repository: GuardRepository
    runs: tuple[AuditRun, ...]
    reports: tuple[ReportResult, ...] = ()
    # True when the run listing ended because Guard said has_more was false; False when run
    # paging stopped early on the 24 h bound, so older runs may exist that this poll never read.
    runs_complete: bool = False


class ProjectOk(_Frozen):
    project: GuardProject
    repositories: tuple[RepoRuns, ...]

    @property
    def newest_counting_run_at(self) -> datetime | None:
        times = [
            run.completed_at
            for repo in self.repositories
            for run in repo.runs
            if run.counts and run.completed_at is not None
        ]
        return max(times) if times else None


class ProjectUnknown(_Frozen):
    project: GuardProject
    reason: str
    cause: GuardFailure = GuardFailure.DATA
    auth_kind: AuthFailureKind | None = None


class ListingOk(_Frozen):
    projects: tuple[GuardProject, ...]


class ListingUnknown(_Frozen):
    reason: str
    cause: GuardFailure = GuardFailure.DATA
    auth_kind: AuthFailureKind | None = None


ProjectData = ProjectOk | ProjectUnknown
ProjectListing = ListingOk | ListingUnknown


@dataclass(frozen=True)
class Page[T]:
    items: tuple[T, ...]
    has_more: bool
    next_cursor: str | None


def newest_counting_runs_by_action_key(runs: Iterable[AuditRun]) -> tuple[AuditRun, ...]:
    """The counting run with the latest completion per audit type, sorted by action_key.

    On equal completion times the run that comes first in Guard's order stays.
    """
    newest: dict[str, AuditRun] = {}
    for run in runs:
        if not run.counts or run.completed_at is None:
            continue
        current = newest.get(run.action_key)
        if current is None or (
            current.completed_at is not None and run.completed_at > current.completed_at
        ):
            newest[run.action_key] = run
    return tuple(newest[key] for key in sorted(newest))


def parse_guard_time(value: str) -> datetime:
    """Parse a Guard timestamp (UTC with Z or an offset, up to 6 fraction digits) to UTC."""
    if _TIME_PATTERN.fullmatch(value) is None:
        message = "invalid timestamp"
        raise GuardDataError(message)
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        message = "invalid timestamp"
        raise GuardDataError(message) from None


def _text(item: Mapping[str, object], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        message = f"field {key} is missing or not a string"
        raise GuardDataError(message)
    return value


def _flag(item: Mapping[str, object], key: str) -> bool:
    value = item.get(key)
    if not isinstance(value, bool):
        message = f"field {key} is missing or not a boolean"
        raise GuardDataError(message)
    return value


def _time(item: Mapping[str, object], key: str) -> datetime:
    return parse_guard_time(_text(item, key))


def _optional_time(item: Mapping[str, object], key: str) -> datetime | None:
    if key not in item:
        message = f"field {key} is missing"
        raise GuardDataError(message)
    return None if item[key] is None else _time(item, key)


def _optional_flag(item: Mapping[str, object], key: str) -> bool | None:
    return None if item.get(key) is None else _flag(item, key)


def _page[T](
    payload: object,
    list_key: str,
    parse_item: Callable[[Mapping[str, object]], T],
) -> Page[T]:
    if not isinstance(payload, Mapping):
        message = "response is not an object"
        raise GuardDataError(message)
    raw_items = payload.get(list_key)
    if not isinstance(raw_items, list):
        message = f"list {list_key} is missing or not a list"
        raise GuardDataError(message)
    has_more = _flag(payload, "has_more")
    next_cursor = payload.get("next_cursor")
    if next_cursor is not None and not isinstance(next_cursor, str):
        message = "next_cursor is not a string"
        raise GuardDataError(message)
    items: list[T] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            message = f"an entry of {list_key} is not an object"
            raise GuardDataError(message)
        items.append(parse_item(raw))
    return Page(items=tuple(items), has_more=has_more, next_cursor=next_cursor)


def _project(item: Mapping[str, object]) -> GuardProject:
    return GuardProject(id=_text(item, "id"), name=_text(item, "name"))


def _repository(item: Mapping[str, object]) -> GuardRepository:
    return GuardRepository(
        id=_text(item, "id"),
        project_id=_text(item, "project_id"),
        name=_text(item, "name"),
        provider=_text(item, "provider"),
        connected=_flag(item, "connected"),
    )


def _run(item: Mapping[str, object]) -> AuditRun:
    task_id = _text(item, "task_id")
    status_raw = _text(item, "status")
    report_status_raw = _text(item, "report_status")
    try:
        status = RunStatus(status_raw.strip().casefold())
    except ValueError:
        status = RunStatus.UNKNOWN
        logger.warning("run status unknown: task_id=%s raw=%r", task_id, status_raw)
    try:
        report_status = ReportStatus(report_status_raw.strip().casefold())
    except ValueError:
        report_status = ReportStatus.UNKNOWN
        logger.warning("report status unknown: task_id=%s raw=%r", task_id, report_status_raw)
    run = AuditRun(
        task_id=task_id,
        action_key=_text(item, "action_key"),
        status=status,
        status_raw=status_raw,
        report_status=report_status,
        report_status_raw=report_status_raw,
        created_at=_time(item, "created_at"),
        linked_at=_time(item, "linked_at"),
        completed_at=_optional_time(item, "completed_at"),
        task_available=_optional_flag(item, "task_available"),
    )
    if status is RunStatus.COMPLETED and run.completed_at is None:
        logger.warning("completed run has no completed_at: task_id=%s", task_id)
    return run


def parse_projects_page(payload: Mapping[str, object]) -> Page[GuardProject]:
    return _page(payload, "projects", _project)


def parse_repositories_page(payload: Mapping[str, object]) -> Page[GuardRepository]:
    return _page(payload, "repositories", _repository)


def parse_runs_page(payload: Mapping[str, object]) -> Page[AuditRun]:
    return _page(payload, "runs", _run)


def _pages[T](
    tools: GuardTools,
    tool: str,
    base_args: Mapping[str, str],
    parse: Callable[[Mapping[str, object]], Page[T]],
) -> Iterator[Page[T]]:
    """Yield pages until has_more is false. Any failure surfaces as GuardDataError."""
    args = dict(base_args)
    for _ in range(MAX_PAGES):
        try:
            page = parse(tools.call(tool, args))
        except GuardCallError as error:
            message = f"{tool} failed: {error}"
            auth_kind = error.kind if isinstance(error, GuardAuthError) else None
            raise GuardDataError(message, cause=error.cause, auth_kind=auth_kind) from None
        except GuardDataError as error:
            message = f"{tool} failed: {error}"
            raise GuardDataError(message, cause=error.cause, auth_kind=error.auth_kind) from None
        yield page
        if not page.has_more:
            return
        if page.next_cursor is None:
            message = f"{tool} failed: has_more without next_cursor"
            raise GuardDataError(message)
        args = {**base_args, ARG_CURSOR: page.next_cursor}
    message = f"{tool} failed: more than {MAX_PAGES} pages"
    raise GuardDataError(message)


def list_projects(tools: GuardTools) -> ProjectListing:
    projects: list[GuardProject] = []
    try:
        for page in _pages(tools, TOOL_LIST_PROJECTS, {}, parse_projects_page):
            projects.extend(page.items)
    except GuardDataError as error:
        logger.error("project listing unavailable: %s", error)  # noqa: TRY400
        return ListingUnknown(reason=str(error), cause=error.cause, auth_kind=error.auth_kind)
    if not projects:
        logger.error("project listing is empty: treating Guard as unavailable")
        return ListingUnknown(reason="list_projects returned no projects")
    return ListingOk(projects=tuple(projects))


def _read_runs(tools: GuardTools, repository: GuardRepository) -> tuple[tuple[AuditRun, ...], bool]:
    """Read run pages (newest linked first) until the 24 h bound proves nothing newer is left.

    The flag is True when the listing was read to its end (Guard's last page said has_more false)
    and False when paging stopped early, so the run list may be missing older runs.
    """
    complete = True
    runs: list[AuditRun] = []
    newest: datetime | None = None
    oldest_linked: datetime | None = None
    in_order = True
    args = {ARG_PROJECT_ID: repository.project_id, ARG_REPOSITORY_ID: repository.id}
    for page in _pages(tools, TOOL_LIST_AUDIT_RUNS, args, parse_runs_page):
        for run in page.items:
            if in_order and oldest_linked is not None and run.linked_at > oldest_linked:
                in_order = False
                logger.warning(
                    "run pages of %s are not newest first; reading all pages", repository.id
                )
            oldest_linked = (
                run.linked_at if oldest_linked is None else min(oldest_linked, run.linked_at)
            )
            if run.counts and run.completed_at is not None:
                newest = run.completed_at if newest is None else max(newest, run.completed_at)
            runs.append(run)
        if (
            in_order
            and page.has_more
            and newest is not None
            and oldest_linked is not None
            and oldest_linked + MAX_RUN_DURATION <= newest
        ):
            logger.info("run paging of %s stopped early: no unread run can be newer", repository.id)
            complete = False
            break
    return tuple(runs), complete


def _repo_runs(
    tools: GuardTools, repository: GuardRepository, *, skip_report: SkipReport | None = None
) -> RepoRuns:
    runs, runs_complete = _read_runs(tools, repository)
    reports: list[ReportResult] = []
    for run in newest_counting_runs_by_action_key(runs):
        if skip_report is not None and skip_report(repository.id, run.action_key, run.task_id):
            logger.debug(
                "report %s (%s) not read: already known or not tracked", run.task_id, run.action_key
            )
            continue
        # A report failure is local to that report (ReportUnknown); it never makes the project
        # unknown.
        reports.append(
            read_report(
                tools,
                run.task_id,
                run.action_key,
                project_id=repository.project_id,
                repo_id=repository.id,
            )
        )
    return RepoRuns(
        repository=repository, runs=runs, reports=tuple(reports), runs_complete=runs_complete
    )


def fetch_project(
    tools: GuardTools, project: GuardProject, *, skip_report: SkipReport | None = None
) -> ProjectData:
    """Read a project's repositories, runs and newest reports; any list failure is unknown.

    SRC-04: a well-formed answer with zero items (a project with no repositories, a repository
    with no runs) is real. A blank, truncated or malformed page, a failed call or a page that was
    not fetched is unknown and never becomes a partial ProjectOk. A failed report only makes that
    report unknown (RepoRuns.reports); the project stays evaluable. A report that skip_report
    (repo id, action key, task id) accepts is not read and has no entry in RepoRuns.reports.
    """
    try:
        repositories: list[GuardRepository] = []
        for page in _pages(
            tools, TOOL_LIST_REPOSITORIES, {ARG_PROJECT_ID: project.id}, parse_repositories_page
        ):
            repositories.extend(page.items)
        repo_runs = tuple(
            _repo_runs(tools, repository, skip_report=skip_report) for repository in repositories
        )
    except GuardDataError as error:
        logger.warning("project %s is unknown: %s", project.id, error)
        return ProjectUnknown(
            project=project, reason=str(error), cause=error.cause, auth_kind=error.auth_kind
        )
    return ProjectOk(project=project, repositories=repo_runs)
