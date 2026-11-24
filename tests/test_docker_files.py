"""The Dockerfile, .dockerignore and compose.yaml, checked statically (no Docker needed here)."""

import json
import re
from pathlib import Path
from typing import Any

import yaml

from enji_guard_radar import cli
from enji_guard_radar.secret_guard import find_secret

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"
COMPOSE = ROOT / "compose.yaml"
ENTRY_POINT = "/app/.venv/bin/enji-guard-radar"


def dockerfile_lines() -> list[str]:
    """Instruction lines with continuations joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def instruction(name: str) -> list[str]:
    return [
        line.partition(" ")[2].strip() for line in dockerfile_lines() if line.startswith(name + " ")
    ]


def compose() -> dict[str, Any]:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def service() -> dict[str, Any]:
    services = compose()["services"]
    assert list(services) == ["radar"]
    return dict(services["radar"])


def mounts() -> dict[str, dict[str, Any]]:
    return {m["target"]: m for m in service()["volumes"]}


def test_compose_has_one_restarting_service_with_a_grace_period() -> None:
    radar = service()
    assert radar["restart"] == "unless-stopped"
    assert radar["stop_grace_period"] == "2m"
    assert radar["build"] == "."


def test_compose_mounts_config_env_and_state() -> None:
    by_target = mounts()
    assert set(by_target) == {"/config", "/run/radar.env", "/state"}
    config, env, state = by_target["/config"], by_target["/run/radar.env"], by_target["/state"]
    assert (config["type"], config["source"], config["read_only"]) == ("bind", "./config", True)
    assert (env["type"], env["source"], env["read_only"]) == ("bind", "./.env", True)
    assert env["bind"] == {"create_host_path": False}
    assert (state["type"], state["source"]) == ("volume", "radar-state")
    assert "radar-state" in compose()["volumes"]


def test_compose_carries_no_secret_and_publishes_nothing() -> None:
    radar = service()
    for key in ("environment", "env_file", "ports", "command", "entrypoint"):
        assert key not in radar, key
    code = [
        line
        for line in COMPOSE.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    assert not [line for line in code if "uv run" in line]


def test_dockerfile_runs_the_installed_entry_point_not_uv_run() -> None:
    assert json.loads(instruction("ENTRYPOINT")[0]) == [ENTRY_POINT]
    assert not [line for line in dockerfile_lines() if "uv run" in line]


def test_dockerfile_installs_the_locked_dependencies_once() -> None:
    syncs = [line for line in instruction("RUN") if "uv sync" in line]
    assert syncs == ["uv sync --frozen --no-dev --no-editable"]


def test_dockerfile_command_is_a_valid_serve_invocation() -> None:
    (raw,) = instruction("CMD")
    args = cli._build_parser().parse_args(json.loads(raw))
    assert args.command == "serve"
    assert args.config == Path("/config/radar.yaml")
    assert args.state_dir == Path("/state")
    assert args.env_file == Path("/run/radar.env")


def test_dockerfile_runs_as_a_numeric_non_root_user() -> None:
    (user,) = instruction("USER")
    uid = user.split(":")[0]
    assert uid.isdigit()
    assert int(uid) != 0
    assert any("useradd" in run and f"--uid {uid}" in run for run in instruction("RUN"))


def test_dockerfile_copies_no_secret_or_state() -> None:
    sources = [
        word
        for line in instruction("COPY")
        for word in line.split()
        if not word.startswith("--") and word != "./"
    ]
    forbidden = {".env", "state", "state/", "project.env", ".git", "."}
    assert not forbidden & set(sources)
    assert not [line for line in instruction("ENV") + instruction("ARG") if "TOKEN" in line]


def test_dockerignore_keeps_secrets_and_state_out_of_the_build() -> None:
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    for pattern in (".env", "state/", ".git", "project.env", ".venv"):
        assert pattern in lines


def test_no_secret_in_the_docker_files() -> None:
    for path in (DOCKERFILE, DOCKERIGNORE, COMPOSE):
        assert find_secret(path.read_text(encoding="utf-8"), ()) is None, path.name
