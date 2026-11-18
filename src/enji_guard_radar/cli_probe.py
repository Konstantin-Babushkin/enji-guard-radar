"""The `probe` command: record sanitized Guard responses as fixtures. Read-only, never runs git.

The walk is the run's own read walk (`list_projects`, then `fetch_project` per active project)
through a recording wrapper. Raw responses stay in memory. Only sanitized files that passed the
three checks of `probe_sanitize` are written, all or nothing: into a temporary sibling directory
that is renamed to `--out` at the end.
"""

import argparse
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, TextIO

from enji_guard_radar.config import ConfigError, RadarConfig, parse_config
from enji_guard_radar.guard import ListingOk, ProjectUnknown, fetch_project, list_projects
from enji_guard_radar.guard_auth import AUTH_FAILURE_TEXT
from enji_guard_radar.ports import GuardAuthError, GuardCallError, GuardConnector
from enji_guard_radar.probe import RecordingGuardTools, format_summary, summarize
from enji_guard_radar.probe_sanitize import (
    check_round_trip,
    find_taint,
    render_files,
    sanitize_recordings,
    scan_for_leaks,
    taint_strings,
)
from enji_guard_radar.secret_guard import configure_logging

logger = logging.getLogger(__name__)

DEFAULT_PROBE_DIR = "fixtures/guard-real"
DEFAULT_STATE_DIR = "state"
EXIT_REFUSED = 1
EXIT_USAGE = 2
DIRECTORY_MODE = 0o755


def add_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "probe",
        help="record sanitized Guard responses as fixtures; read-only; never commits",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(DEFAULT_PROBE_DIR),
        metavar="DIR",
        help=f"empty or new directory for the fixtures (default: {DEFAULT_PROBE_DIR})",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(DEFAULT_STATE_DIR),
        metavar="DIR",
        help=f"directory with guard-auth.json (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--config", type=Path, metavar="PATH", help="radar config file (paused/excluded projects)"
    )


def _read_config(path: Path | None) -> RadarConfig:
    if path is None:
        return RadarConfig()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        message = f"config file not found: {path}"
        raise ConfigError(message) from None
    except (OSError, UnicodeDecodeError):
        message = f"cannot read config file {path}"
        raise ConfigError(message) from None
    return parse_config(text, source=str(path))


def _not_empty(directory: Path) -> bool:
    if not directory.exists():
        return False
    return not directory.is_dir() or any(directory.iterdir())


def _write_all_or_nothing(directory: Path, texts: dict[str, str]) -> None:
    """Write every file into a temporary sibling, then rename it to `directory`."""
    parent = directory.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}.tmp-", dir=parent))
    try:
        for name, text in texts.items():
            (staging / name).write_text(text, encoding="utf-8")
        staging.chmod(DIRECTORY_MODE)
        if directory.exists():
            directory.rmdir()  # an empty directory the operator created
        staging.rename(directory)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _refuse(problems: list[str]) -> int:
    logger.error("probe refused to write: %d problem(s)", len(problems))
    print(f"error: probe refused to write: {len(problems)} problem(s)", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return EXIT_REFUSED


def run_probe(args: argparse.Namespace, *, out: TextIO, guard_connector: GuardConnector) -> int:
    configure_logging(level=logging.INFO, known_secrets=(), stream=sys.stderr)
    try:
        config = _read_config(args.config)
    except ConfigError as error:
        logger.error("probe rejected: %s", error)  # noqa: TRY400
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE
    target: Path = args.out
    if _not_empty(target):
        logger.error("probe rejected: output directory is not empty")
        print(
            f"error: --out {target} exists and is not empty; choose a new directory",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        with guard_connector(args.state_dir) as session:
            recorder = RecordingGuardTools(session)
            listing = list_projects(recorder)
            if not isinstance(listing, ListingOk):
                logger.error("probe stopped: project listing unavailable")
                text = (
                    AUTH_FAILURE_TEXT[listing.auth_kind]
                    if listing.auth_kind is not None
                    else f"Guard project listing unavailable ({listing.reason})"
                )
                print(f"error: {text}; nothing was written", file=sys.stderr)
                return EXIT_REFUSED
            unknown = 0
            for project in listing.projects:
                if config.mode_for(project.id) != "active":
                    logger.info("probe skips a project that is paused or excluded")
                    continue
                if isinstance(fetch_project(recorder, project), ProjectUnknown):
                    unknown += 1
            tool_arguments = dict(session.tool_arguments())
            result_shapes = dict(session.result_shapes())
    except GuardAuthError as error:
        logger.error("probe stopped: Guard login unusable (%s)", error.kind.value)  # noqa: TRY400
        print(f"error: {AUTH_FAILURE_TEXT[error.kind]}; nothing was written", file=sys.stderr)
        return EXIT_REFUSED
    except GuardCallError as error:
        logger.error("probe stopped: Guard unavailable")  # noqa: TRY400
        print(f"error: Guard unavailable ({error}); nothing was written", file=sys.stderr)
        return EXIT_REFUSED
    logger.info(
        "probe walked Guard: %d call(s), %d project(s) unknown",
        len(recorder.recordings),
        unknown,
    )

    sanitized = sanitize_recordings(recorder.recordings, tool_arguments=tool_arguments)
    texts = render_files(sanitized)
    taint = taint_strings(recorder.recordings, kept=sanitized.kept)
    problems = [
        *check_round_trip(sanitized.reports, sanitized),
        *find_taint(texts, taint),
        *scan_for_leaks(texts),
    ]
    if problems:
        return _refuse(problems)

    try:
        _write_all_or_nothing(target, texts)
    except OSError as error:
        logger.error("probe could not write the fixtures: %s", error.strerror)  # noqa: TRY400
        print(f"error: cannot write {target}: {error.strerror}", file=sys.stderr)
        return EXIT_REFUSED
    logger.info("probe wrote %d file(s) to the output directory", len(texts))
    summary = summarize(
        recorder.recordings,
        tool_arguments=tool_arguments,
        result_shapes=result_shapes,
        dropped=sanitized.dropped,
    )
    out.write(format_summary(summary))
    out.write(
        f"wrote {len(texts)} file(s) to {target}\n"
        f"Review with: git diff --stat -- {target} && git diff -- {target}. "
        "Commit only after reading it.\n"
    )
    return 0
