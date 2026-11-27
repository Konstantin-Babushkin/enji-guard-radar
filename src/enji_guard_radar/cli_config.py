"""The `effective-config` command: the settings a poll would use, YAML with the sheet over it.

Read-only by construction: it reads the sheet (or, when that fails, the last good copy) but
never writes state and never sends a message. The first line says where the settings came from.
The printed config holds no secret (those live in the env file).
"""

import argparse
import logging
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import yaml

from enji_guard_radar.config import ConfigError, RadarConfig
from enji_guard_radar.ports import SheetReader
from enji_guard_radar.secret_guard import redact
from enji_guard_radar.settings import SettingsError, load_sheet_key
from enji_guard_radar.sheet_client import SheetsHttp, key_secrets, make_sheet_reader
from enji_guard_radar.sheet_store import FileSheetStore, ReadOnlySheetStore
from enji_guard_radar.sheet_sync import SheetSettingsSource, describe_source

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "state"
DEFAULT_ENV_FILE = ".env"
YAML_ONLY_SOURCE = "YAML only (sheet disabled)"


def add_parser(commands: Any) -> None:
    parser = commands.add_parser(
        "effective-config",
        help="print the settings a poll would use (YAML with the Google Sheet over it)",
    )
    parser.add_argument("--config", type=Path, metavar="PATH")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(DEFAULT_STATE_DIR),
        metavar="DIR",
        help=f"directory with the last good sheet copy (default: {DEFAULT_STATE_DIR})",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"dotenv file with the sheet key (default: {DEFAULT_ENV_FILE}; may be missing)",
    )


def run_effective_config(
    args: argparse.Namespace,
    *,
    out: TextIO,
    environ: Mapping[str, str],
    read_config: Callable[[Path | None], RadarConfig],
    clock: Callable[[], datetime],
    sheet_reader: SheetReader | None,
    sheet_http: SheetsHttp | None,
) -> int:
    try:
        base = read_config(args.config)
    except ConfigError as error:
        logger.error("effective-config rejected: %s", error)  # noqa: TRY400
        print(f"error: {error}", file=sys.stderr)
        return 2
    config, source = base, YAML_ONLY_SOURCE
    secrets: tuple[str, ...] = ()
    if base.sheet.enabled:
        env_file: Path = args.env_file or Path(DEFAULT_ENV_FILE)
        try:
            key = load_sheet_key(environ, env_file)
        except SettingsError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        secrets = key_secrets(key)
        reader = make_sheet_reader(
            base.sheet, key, injected=sheet_reader, http=sheet_http, clock=clock
        )
        store = ReadOnlySheetStore(FileSheetStore(args.state_dir))
        resolution = SheetSettingsSource(reader, store).resolve(base, clock())
        config, source = resolution.config, describe_source(resolution)
    body = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    out.write(redact(f"# source: {source}\n{body}", secrets))
    return 0
