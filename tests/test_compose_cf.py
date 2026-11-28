"""compose.cf.yaml (the offline override for code-factory), checked statically: no Docker needed."""

import json
import re
from pathlib import Path
from typing import Any

import yaml

from enji_guard_radar import cli

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE = ROOT / "compose.cf.yaml"


class _ComposeLoader(yaml.SafeLoader):
    """Reads Compose's `!reset` and `!override` tags as their plain value."""


def _plain(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return yaml.safe_load(str(loader.construct_scalar(node)))  # type: ignore[arg-type]


_ComposeLoader.add_constructor("!reset", _plain)
_ComposeLoader.add_constructor("!override", _plain)


def radar() -> dict[str, Any]:
    data = yaml.load(OVERRIDE.read_text(encoding="utf-8"), Loader=_ComposeLoader)
    assert list(data["services"]) == ["radar"]
    return dict(data["services"]["radar"])


def test_override_has_no_env_bind_no_secret_and_no_host_path_outside_the_repo() -> None:
    mounts = {m["target"]: m for m in radar()["volumes"]}
    assert set(mounts) == {"/config", "/fixtures/guard", "/state"}
    for mount in mounts.values():
        source = mount["source"]
        assert mount["type"] == "volume" or source.startswith("./")
        assert ".env" not in source
    assert mounts["/fixtures/guard"]["read_only"] is True
    assert mounts["/config"]["read_only"] is True
    for key in ("environment", "env_file", "entrypoint"):
        assert key not in radar(), key


def test_override_publishes_nothing_and_names_nothing_fixed() -> None:
    text = OVERRIDE.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in (
        "container_name",
        "ports:",
        "external",
        "networks:",
        "enji-guard-radar:local",
    ):
        assert forbidden not in code, forbidden
    assert "image: !reset null" in text


def test_override_command_is_a_valid_offline_serve_with_no_env_file() -> None:
    args = cli._build_parser().parse_args(radar()["command"])
    assert args.command == "serve"
    assert args.fixtures == Path("/fixtures/guard")
    assert args.dry_run is True
    assert args.config == Path("/config/radar.example.yaml")
    assert args.state_dir == Path("/state")
    assert args.env_file is None
    assert args.ready_file is not None


def test_healthcheck_probes_exactly_the_ready_file_serve_writes() -> None:
    args = cli._build_parser().parse_args(radar()["command"])
    probe = radar()["healthcheck"]["test"]
    assert probe[0] == "CMD"
    assert probe[1].startswith("/app/.venv/")
    assert json.dumps(str(args.ready_file)).strip('"') in probe[-1]
    assert re.search(r"retries", yaml.safe_dump(radar()["healthcheck"]))
