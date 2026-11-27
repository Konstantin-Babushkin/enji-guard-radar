import logging
from pathlib import Path

import pytest

import enji_guard_radar
from enji_guard_radar.env_file import EnvFileError, read_env_file

REPO = Path(__file__).resolve().parents[1]
SECRET = "dummy-secret-value-4f9c1e"


def write_env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "project.env"
    path.write_text(text, encoding="utf-8")
    return path


def test_values_load_from_the_given_file(tmp_path: Path) -> None:
    path = write_env(tmp_path, f"TELEGRAM_BOT_TOKEN={SECRET}\nTELEGRAM_CHAT_ID=-100123\n")
    assert read_env_file(path) == {"TELEGRAM_BOT_TOKEN": SECRET, "TELEGRAM_CHAT_ID": "-100123"}


def test_comments_blank_lines_export_and_matching_quotes_are_handled(tmp_path: Path) -> None:
    path = write_env(tmp_path, "# c\n\nexport A=1\nB='x y'\nC=\"z\"\nD='unbalanced\n")
    assert read_env_file(path) == {"A": "1", "B": "x y", "C": "z", "D": "'unbalanced"}


def test_missing_file_fails_naming_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.env"
    with pytest.raises(EnvFileError, match="not found") as info:
        read_env_file(missing)
    assert str(missing) in str(info.value)


def test_unreadable_path_fails_naming_the_path(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError) as info:
        read_env_file(tmp_path)  # a directory
    assert str(tmp_path) in str(info.value)


@pytest.mark.parametrize("bad_line", [f"TELEGRAM_BOT_TOKEN {SECRET}", f"1BAD={SECRET}"])
def test_malformed_line_names_line_number_and_never_its_content(
    tmp_path: Path, bad_line: str
) -> None:
    path = write_env(tmp_path, f"OK=1\n{bad_line}\n")
    with pytest.raises(EnvFileError, match="line 2") as info:
        read_env_file(path)
    assert SECRET not in str(info.value)


def test_non_utf8_content_is_not_echoed(tmp_path: Path) -> None:
    path = tmp_path / "project.env"
    path.write_bytes(b"TELEGRAM_BOT_TOKEN=" + SECRET.encode() + b"\xff\n")
    with pytest.raises(EnvFileError) as info:
        read_env_file(path)
    assert SECRET not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_without_flag_behavior_is_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    assert enji_guard_radar.main([]) == 0
    captured = capsys.readouterr()
    assert captured.out == "enji-guard-radar: not implemented yet\n"
    assert captured.err == ""


def test_with_flag_secret_values_never_reach_output_or_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    path = write_env(tmp_path, f"TELEGRAM_BOT_TOKEN={SECRET}\n")
    with caplog.at_level(logging.DEBUG):
        assert enji_guard_radar.main(["--env-file", str(path)]) == 0
    captured = capsys.readouterr()
    assert "loaded 1 variables" in caplog.text
    assert SECRET not in captured.out + captured.err + caplog.text


def test_with_bad_flag_file_exit_2_names_path_and_leaks_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    path = write_env(tmp_path, f"TELEGRAM_BOT_TOKEN {SECRET}\n")
    missing = tmp_path / "nope.env"
    with caplog.at_level(logging.DEBUG):
        assert enji_guard_radar.main(["--env-file", str(path)]) == 2
        assert enji_guard_radar.main(["--env-file", str(missing)]) == 2
    captured = capsys.readouterr()
    assert str(path) in captured.err
    assert str(missing) in captured.err
    assert SECRET not in captured.out + captured.err + caplog.text


def test_example_file_lists_the_secret_names_with_placeholders_only() -> None:
    example = read_env_file(REPO / "project.env.example")
    assert set(example) == {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_OPS_CHAT_ID",
        "GOOGLE_SERVICE_ACCOUNT_JSON",
    }
    assert all(value.startswith("replace-with-") for value in example.values())


def test_real_env_file_names_are_git_ignored() -> None:
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".env", "project.env"} <= set(ignored)
