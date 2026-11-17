"""The shell: argument parsing, env file, output. Everything with I/O lives here."""

import argparse
import logging
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path
from typing import TextIO

from enji_guard_radar.env_file import EnvFileError, read_env_file

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enji-guard-radar")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('enji-guard-radar')}",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help="dotenv file (KEY=VALUE per line) with credentials; fails if missing or unreadable",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout if stdout is not None else sys.stdout
    args = _build_parser().parse_args(argv)

    if args.env_file is not None:
        try:
            env_values = read_env_file(args.env_file)
        except EnvFileError as error:
            logger.error("env file rejected: %s", error)  # noqa: TRY400
            print(f"error: {error}", file=sys.stderr)
            return 2
        logger.info("loaded %d variables from %s", len(env_values), args.env_file)

    print("enji-guard-radar: not implemented yet", file=out)
    return 0
