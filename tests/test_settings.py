from pathlib import Path

import pytest
from telegram_fakes import FAKE_TOKEN

from enji_guard_radar.settings import (
    LiveTelegram,
    SettingsError,
    TelegramSettings,
    known_secrets,
    load_settings,
    read_dotenv,
    require_live,
)

NO_ENV: dict[str, str] = {}


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_read_dotenv_parses_the_supported_forms(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "# comment\n\nA=1\nexport B=2\nC='v a'\nD=\"v a\"\nE=with # no inline comment\n",
    )
    assert read_dotenv(path) == {
        "A": "1",
        "B": "2",
        "C": "v a",
        "D": "v a",
        "E": "with # no inline comment",
    }


def test_read_dotenv_missing_or_empty_file_is_valid(tmp_path: Path) -> None:
    assert read_dotenv(tmp_path / "nope.env") == {}
    assert read_dotenv(write(tmp_path, "")) == {}


def test_read_dotenv_syntax_error_reports_line_number_not_text(tmp_path: Path) -> None:
    path = write(tmp_path, f"A=1\n# ok\nnot a pair {FAKE_TOKEN}\n")
    with pytest.raises(SettingsError) as info:
        read_dotenv(path)
    assert "line 3" in str(info.value)
    assert "not a pair" not in str(info.value)
    assert FAKE_TOKEN not in str(info.value)


def test_read_dotenv_unreadable_path_is_a_settings_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError):
        read_dotenv(tmp_path)  # a directory


def test_load_settings_env_overrides_dotenv(tmp_path: Path) -> None:
    path = write(tmp_path, "TELEGRAM_BOT_TOKEN=from-file\nTELEGRAM_CHAT_ID=-100\n")
    settings = load_settings({"TELEGRAM_BOT_TOKEN": "from-env"}, path)
    assert settings.bot_token is not None
    assert settings.bot_token.get_secret_value() == "from-env"
    assert settings.chat_id == "-100"
    assert settings.ops_chat_id is None


def test_load_settings_empty_env_value_falls_back_to_dotenv(tmp_path: Path) -> None:
    path = write(tmp_path, "TELEGRAM_BOT_TOKEN=from-file\n")
    settings = load_settings({"TELEGRAM_BOT_TOKEN": "  "}, path)
    assert settings.bot_token is not None
    assert settings.bot_token.get_secret_value() == "from-file"


def test_load_settings_without_any_source_is_all_unset() -> None:
    assert load_settings(NO_ENV, None) == TelegramSettings(
        bot_token=None, chat_id=None, ops_chat_id=None
    )


@pytest.mark.parametrize("chat_id", ["-1001234567890", "12345", "@radar_test"])
def test_load_settings_accepts_chat_ids(chat_id: str) -> None:
    assert load_settings({"TELEGRAM_CHAT_ID": chat_id}, None).chat_id == chat_id


@pytest.mark.parametrize("variable", ["TELEGRAM_CHAT_ID", "TELEGRAM_OPS_CHAT_ID"])
@pytest.mark.parametrize("bad", ["abc", "@ab", "12 34", "-"])
def test_load_settings_rejects_a_bad_chat_id_without_echoing_it(variable: str, bad: str) -> None:
    with pytest.raises(SettingsError) as info:
        load_settings({variable: bad}, None)
    assert variable in str(info.value)
    assert bad not in str(info.value).replace(variable, "")


def test_load_settings_reads_only_the_three_variables(tmp_path: Path) -> None:
    path = write(tmp_path, "OTHER_SECRET=zzz\n")
    settings = load_settings({"OTHER_SECRET": "yyy"}, path)
    assert "zzz" not in repr(settings)
    assert "yyy" not in repr(settings)


def test_require_live_names_the_missing_variables() -> None:
    with pytest.raises(SettingsError) as both:
        require_live(load_settings(NO_ENV, None))
    assert "TELEGRAM_BOT_TOKEN" in str(both.value)
    assert "TELEGRAM_CHAT_ID" in str(both.value)

    with pytest.raises(SettingsError) as token_only:
        require_live(load_settings({"TELEGRAM_CHAT_ID": "12345"}, None))
    assert "TELEGRAM_BOT_TOKEN" in str(token_only.value)
    assert "TELEGRAM_CHAT_ID" not in str(token_only.value)

    with pytest.raises(SettingsError) as chat_only:
        require_live(load_settings({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN}, None))
    assert "TELEGRAM_CHAT_ID" in str(chat_only.value)
    assert FAKE_TOKEN not in str(chat_only.value)


def test_require_live_returns_live_settings_when_complete() -> None:
    live = require_live(
        load_settings({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "-100123"}, None)
    )
    assert isinstance(live, LiveTelegram)
    assert live.bot_token.get_secret_value() == FAKE_TOKEN
    assert live.chat_id == "-100123"
    assert live.ops_chat_id is None


def test_known_secrets_returns_the_token_or_nothing() -> None:
    assert known_secrets(load_settings({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN}, None)) == (FAKE_TOKEN,)
    assert known_secrets(load_settings(NO_ENV, None)) == ()


def test_settings_repr_and_str_never_contain_the_token() -> None:
    settings = load_settings({"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "12345"}, None)
    live = require_live(settings)
    for obj in (settings, live):
        assert FAKE_TOKEN not in repr(obj)
        assert FAKE_TOKEN not in str(obj)
