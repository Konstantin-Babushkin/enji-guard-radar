"""FixtureGuardTools is a strict, read-only, offline replay."""

import json
from pathlib import Path

import pytest

from enji_guard_radar.fixture_source import FixtureGuardTools
from enji_guard_radar.ports import GuardCallError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
LISTING = {"projects": [], "has_more": False}


def _manifest(tmp_path: Path, calls: list[dict[str, object]]) -> Path:
    (tmp_path / "calls.json").write_text(json.dumps({"calls": calls}), encoding="utf-8")
    return tmp_path


def _entry(tool: str = "list_projects", **fields: object) -> dict[str, object]:
    return {"tool": tool, "args": {}, "response": "ok.json", **fields}


def _write(tmp_path: Path, name: str = "ok.json", text: str | None = None) -> None:
    (tmp_path / name).write_text(
        text if text is not None else json.dumps(LISTING), encoding="utf-8"
    )


def test_committed_fixtures_load_and_replay() -> None:
    tools = FixtureGuardTools(FIXTURES)
    assert "projects" in tools.call("list_projects", {})


def test_valid_manifest_replays_a_response_and_a_recorded_error(tmp_path: Path) -> None:
    _write(tmp_path)
    directory = _manifest(
        tmp_path,
        [
            _entry(),
            _entry("list_repositories", args={"project_id": "p"}, response=None, error="down"),
        ],
    )
    tools = FixtureGuardTools(directory)
    assert tools.call("list_projects", {}) == LISTING
    with pytest.raises(GuardCallError, match="down"):
        tools.call("list_repositories", {"project_id": "p"})
    with pytest.raises(GuardCallError, match="no recorded response"):
        tools.call("list_repositories", {"project_id": "other"})


@pytest.mark.parametrize(
    "calls",
    [
        pytest.param([_entry(), _entry()], id="duplicate_same_args"),
        pytest.param(
            [
                _entry("list_repositories", args={"a": "1", "b": "2"}),
                _entry("list_repositories", args={"b": "2", "a": "1"}),
            ],
            id="duplicate_args_in_other_order",
        ),
        pytest.param([_entry(error="down")], id="response_and_error"),
        pytest.param([_entry(response=None)], id="neither_response_nor_error"),
        pytest.param([_entry("run_audit")], id="run_audit_entry"),
        pytest.param([_entry("list_audits")], id="tool_outside_the_read_set"),
    ],
)
def test_bad_manifest_fails_at_construction(tmp_path: Path, calls: list[dict[str, object]]) -> None:
    with pytest.raises(GuardCallError):
        FixtureGuardTools(_manifest(tmp_path, calls))


def test_missing_or_invalid_manifest_fails_at_construction(tmp_path: Path) -> None:
    with pytest.raises(GuardCallError):
        FixtureGuardTools(tmp_path)
    (tmp_path / "calls.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(GuardCallError):
        FixtureGuardTools(tmp_path)


def test_run_audit_call_is_refused_even_when_recorded_elsewhere() -> None:
    with pytest.raises(GuardCallError, match="tool not allowed: run_audit"):
        FixtureGuardTools(FIXTURES).call("run_audit", {})


@pytest.mark.parametrize("tool", ["run_audit", "get_audit", "list_audits", "", "LIST_PROJECTS"])
def test_every_tool_outside_the_read_set_is_refused(tool: str) -> None:
    with pytest.raises(GuardCallError, match="tool not allowed"):
        FixtureGuardTools(FIXTURES).call(tool, {})


@pytest.mark.parametrize(
    ("response", "write"),
    [
        pytest.param("missing.json", False, id="missing_file"),
        pytest.param("ok.json", "{not json", id="invalid_json"),
        pytest.param("ok.json", "[1, 2]", id="json_list"),
        pytest.param("ok.json", "null", id="json_null"),
        pytest.param("/nonexistent-dir/ok.json", False, id="absolute_path_traversal"),
        pytest.param("../outside.json", False, id="dotdot_traversal"),
        pytest.param("sub/../../outside.json", False, id="nested_dotdot_traversal"),
    ],
)
def test_bad_response_files_raise_guard_call_error(
    tmp_path: Path, response: str, write: str | bool
) -> None:
    directory = tmp_path / "fixtures"
    directory.mkdir()
    if isinstance(write, str):
        _write(directory, text=write)
    (tmp_path / "outside.json").write_text(json.dumps(LISTING), encoding="utf-8")
    tools = FixtureGuardTools(_manifest(directory, [_entry(response=response)]))
    with pytest.raises(GuardCallError):
        tools.call("list_projects", {})
