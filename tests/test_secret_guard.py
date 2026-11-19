import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from telegram_fakes import FAKE_TOKEN

from enji_guard_radar.secret_guard import (
    LOG_FORMAT,
    MIN_SECRET_CHARS,
    QUIET_LOGGERS,
    REDACTED,
    RedactingFormatter,
    configure_logging,
    find_secret,
    redact,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
LETTERS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


@pytest.fixture
def clean_root_logger() -> Iterator[logging.Logger]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    quiet = {name: logging.getLogger(name).level for name in QUIET_LOGGERS}
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, quiet_level in quiet.items():
        logging.getLogger(name).setLevel(quiet_level)


@pytest.mark.parametrize(
    ("text", "known", "expected"),
    [
        pytest.param(FAKE_TOKEN, [FAKE_TOKEN], "known_secret", id="known_value"),
        pytest.param(FAKE_TOKEN, [], "telegram_bot_token", id="token_shape"),
        pytest.param(
            "see https://api.telegram.org/bot123/sendMessage", [], "telegram_bot_url", id="url"
        ),
        pytest.param("ghp_" + LETTERS[:36], [], "github_token", id="github_token"),
        pytest.param("github_pat_" + "a1" * 15, [], "github_pat", id="github_pat"),
        pytest.param("AKIA" + "ABCDEFGHIJKLMNOP", [], "aws_access_key_id", id="aws"),
        pytest.param("-----BEGIN RSA PRIVATE KEY-----", [], "private_key", id="private_key"),
        pytest.param("-----BEGIN PRIVATE KEY-----", [], "private_key", id="private_key_plain"),
        pytest.param("sk-" + LETTERS[:24], [], "api_key_sk", id="sk"),
        pytest.param("xoxb-" + "1234567890abcdef0123", [], "slack_token", id="slack"),
        pytest.param("Bearer " + LETTERS[:30], [], "bearer_token", id="bearer"),
        pytest.param("abc" + FAKE_TOKEN + "xyz", [], "telegram_bot_token", id="glued_shape"),
        pytest.param("abc" + FAKE_TOKEN + "xyz", [FAKE_TOKEN], "known_secret", id="glued_known"),
        pytest.param("🟡 <b>Project B</b> · no scan 28d", [FAKE_TOKEN], None, id="alert_text"),
        pytest.param("risk-assessment-of-the-whole-platform", [], None, id="sk_inside_word"),
        pytest.param("a short secret", ["short"], None, id="known_too_short"),
    ],
)
def test_find_secret_table(text: str, known: list[str], expected: str | None) -> None:
    assert find_secret(text, known) == expected


def test_find_secret_never_returns_the_value() -> None:
    assert find_secret(FAKE_TOKEN, [FAKE_TOKEN]) != FAKE_TOKEN


def test_committed_fixtures_do_not_trip_find_secret() -> None:
    paths = sorted(FIXTURES.glob("*.json"))
    assert paths
    for path in paths:
        assert find_secret(path.read_text(encoding="utf-8"), [FAKE_TOKEN]) is None, path.name


def test_redact_replaces_every_occurrence_and_keeps_other_text() -> None:
    text = f"a {FAKE_TOKEN} b {FAKE_TOKEN} c"
    assert redact(text, [FAKE_TOKEN]) == f"a {REDACTED} b {REDACTED} c"
    assert redact("nothing to hide", [FAKE_TOKEN]) == "nothing to hide"


def test_redact_masks_pattern_matches_without_a_known_value() -> None:
    assert redact(f"x {FAKE_TOKEN} y", []) == f"x {REDACTED} y"


def test_redact_ignores_known_secret_shorter_than_minimum() -> None:
    short = "x" * (MIN_SECRET_CHARS - 1)
    assert redact(f"a {short} b", [short]) == f"a {short} b"
    assert redact(f"a {short * 2} b", [short * 2]) == f"a {REDACTED} b"


def make_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, [FAKE_TOKEN]))
    logger = logging.getLogger(name)
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


def test_redacting_formatter_masks_message_args_and_exception_arg() -> None:
    logger, stream = make_logger("test.redacting.args")
    logger.info("token %s", FAKE_TOKEN)
    logger.error("failed: %s", ValueError(FAKE_TOKEN))
    output = stream.getvalue()
    assert FAKE_TOKEN not in output
    assert output.count(REDACTED) == 2


def test_redacting_formatter_masks_traceback() -> None:
    logger, stream = make_logger("test.redacting.traceback")
    try:
        message = f"POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage failed"
        raise RuntimeError(message)  # noqa: TRY301
    except RuntimeError:
        logger.exception("send failed")
    output = stream.getvalue()
    assert "Traceback" in output
    assert FAKE_TOKEN not in output
    assert REDACTED in output


def test_configure_logging_masks_and_installs_one_handler(
    clean_root_logger: logging.Logger,
) -> None:
    stream = io.StringIO()
    configure_logging(level=logging.INFO, known_secrets=[FAKE_TOKEN], stream=stream)
    logging.getLogger("test.configure").info("token %s", FAKE_TOKEN)
    assert REDACTED in stream.getvalue()
    assert FAKE_TOKEN not in stream.getvalue()


def test_configure_logging_twice_leaves_one_radar_handler(
    clean_root_logger: logging.Logger,
) -> None:
    foreign = logging.NullHandler()
    clean_root_logger.addHandler(foreign)
    before = len(clean_root_logger.handlers)
    first, second = io.StringIO(), io.StringIO()
    configure_logging(level=logging.INFO, known_secrets=[FAKE_TOKEN], stream=first)
    configure_logging(level=logging.DEBUG, known_secrets=[FAKE_TOKEN], stream=second)

    radar = [h for h in clean_root_logger.handlers if getattr(h, "radar_handler", False)]
    assert len(radar) == 1
    assert len(clean_root_logger.handlers) == before + 1
    assert foreign in clean_root_logger.handlers
    assert clean_root_logger.level == logging.DEBUG
    logging.getLogger("test.configure.twice").debug("hello")
    assert "hello" in second.getvalue()
    assert first.getvalue() == ""


def test_configure_logging_quiets_http_libraries(clean_root_logger: logging.Logger) -> None:
    configure_logging(level=logging.DEBUG, known_secrets=[], stream=io.StringIO())
    for name in ("urllib3", "httpx", "httpcore", "httpx2", "httpcore2", "mcp"):
        assert logging.getLogger(name).level == logging.WARNING


def test_mcp_logger_info_is_dropped_and_warning_passes(clean_root_logger: logging.Logger) -> None:
    stream = io.StringIO()
    configure_logging(level=logging.DEBUG, known_secrets=[], stream=stream)
    sdk = logging.getLogger("mcp.client.streamable_http")
    sdk.info("Received session ID: abc-session")
    sdk.warning("sdk warning kept")
    assert "session ID" not in stream.getvalue()
    assert "sdk warning kept" in stream.getvalue()


def test_jwt_shaped_string_is_found_and_redacted() -> None:
    jwt = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
    assert find_secret(f"token {jwt} here", []) == "jwt"
    assert jwt not in redact(f"token {jwt} here", [])
    assert find_secret("audit.public-api-access-safety", []) is None
