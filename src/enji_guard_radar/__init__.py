"""Enji Guard Radar: poll Guard, triage with Claude, alert Telegram."""

import argparse
import logging
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

from enji_guard_radar.env_file import EnvFileError, read_env_file

logger = logging.getLogger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    if args.env_file is not None:
        try:
            env_values = read_env_file(args.env_file)
        except EnvFileError as error:
            logger.error("env file rejected: %s", error)  # noqa: TRY400
            print(f"error: {error}", file=sys.stderr)
            return 2
        logger.info("loaded %d variables from %s", len(env_values), args.env_file)

    print("enji-guard-radar: not implemented yet")
    return 0
