"""High / low audit-type priority end to end: cli.main on fixtures/guard with a fake Telegram.

Everything is offline. The fixture security series (High 1, opened 8 days before the poll) is the
only High series, so "security is low priority" means the list names some other audit type.
"""

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, ok_response

from enji_guard_radar import cli
from enji_guard_radar.ledger import State, deserialize_state

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
ENVIRON = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "-1001234567890"}
DIGEST_AT = "2026-11-18T06:00:00Z"
SECURITY = "audit.security"
NINE_HIGH_KEYS = (
    "audit.security",
    "audit.dependency-vulnerabilities",
    "audit.dependency-aging",
    "audit.authentication-handling",
    "audit.tech-health",
    "audit.config-hygiene",
    "audit.dependency-hygiene",
    "audit.secrets-handling",
    "audit.public-api-access-safety",
)
SECURITY_LINE = '<b>Project A &lt;core&gt; &amp; "tools"</b> · security High 1 · 8d'


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def config_with(tmp_path: Path, key: str, types: tuple[str, ...] | None) -> Path:
    text = (FIXTURES / "radar.yaml").read_text(encoding="utf-8")
    if types is not None:
        text += f"high:\n  {key}:\n" + "".join(f"    - {t}\n" for t in types)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "radar.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def live_run(tmp_path: Path, config: Path) -> FakeTransport:
    transport = FakeTransport([ok_response(1)])
    code = cli.main(
        [
            "run",
            "--fixtures",
            str(FIXTURES),
            "--config",
            str(config),
            "--state-dir",
            str(tmp_path / "state"),
            "--env-file",
            str(tmp_path / "none.env"),
            "--now",
            DIGEST_AT,
        ],
        environ=ENVIRON,
        transport=transport,
        sleep=FakeTime().sleep,
        stdout=io.StringIO(),
    )
    assert code == 0
    return transport


def sent_texts(transport: FakeTransport) -> list[str]:
    return [str(payload["text"]) for _, payload in transport.requests]


def saved_state(tmp_path: Path) -> State:
    return deserialize_state((tmp_path / "state" / "state.json").read_bytes())


def assert_security_is_low_only_in_the_digest(tmp_path: Path, transport: FakeTransport) -> None:
    (digest,) = sent_texts(transport)  # one message: the digest; nothing was alerted
    assert digest.startswith("<b>Radar weekly")
    assert "Low priority · 1" in digest.split("\n")
    assert f"🟡 {SECURITY_LINE}" in digest.split("\n")
    assert "🔴" not in digest
    state = saved_state(tmp_path)
    assert state.schema_version == 3
    assert [c for c in state.conditions if c.key.action_key == SECURITY] == []
    (history,) = [h for h in state.histories if h.action_key == SECURITY]
    assert history.open is True


def test_low_type_is_digest_only_in_a_live_run(tmp_path: Path) -> None:
    config = config_with(tmp_path, "high_priority_audit_types", ("audit.tech-health",))
    assert_security_is_low_only_in_the_digest(tmp_path, live_run(tmp_path, config))


def test_legacy_audit_types_key_gives_the_same_split(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    without_security = tuple(key for key in NINE_HIGH_KEYS if key != SECURITY)
    config = config_with(tmp_path / "a", "audit_types", without_security)
    assert_security_is_low_only_in_the_digest(tmp_path / "a", live_run(tmp_path / "a", config))
    assert "high.audit_types is deprecated" in caplog.text

    # The operator's stopgap as it stands (all nine keys, security included): security is high.
    config = config_with(tmp_path / "b", "audit_types", NINE_HIGH_KEYS)
    (digest,) = sent_texts(live_run(tmp_path / "b", config))
    assert f"🔴 {SECURITY_LINE}" in digest.split("\n")
    assert "Low priority" not in digest


def test_default_config_digest_has_no_low_block(tmp_path: Path) -> None:
    config = config_with(tmp_path, "high_priority_audit_types", None)
    (digest,) = sent_texts(live_run(tmp_path, config))
    assert f"🔴 {SECURITY_LINE}" in digest.split("\n")
    assert "Low priority" not in digest
    state = saved_state(tmp_path)
    assert [c.key.action_key for c in state.conditions if c.key.level == "high"] == [SECURITY]
