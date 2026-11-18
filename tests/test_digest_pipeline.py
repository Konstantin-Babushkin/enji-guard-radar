"""The weekly digest in the run: tracer through cli.main on the fixtures, then the edge table.

No network: Telegram is a fake transport, Guard is the synthetic fixtures or an in-memory world.
"""

import hashlib
import io
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telegram_fakes import FAKE_TOKEN, FakeTime, FakeTransport, ok_response

from enji_guard_radar import cli
from enji_guard_radar.ledger import State, deserialize_state
from enji_guard_radar.telegram import HttpTransport

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "guard"
ENVIRON = {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "-1001234567890"}


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def run_at(
    tmp_path: Path, now: str, transport: HttpTransport, *, dry_run: bool = False
) -> tuple[int, str]:
    args = [
        "run",
        "--fixtures",
        str(FIXTURES),
        "--config",
        str(FIXTURES / "radar.yaml"),
        "--state-dir",
        str(tmp_path / "state"),
        "--env-file",
        str(tmp_path / "none.env"),
        "--now",
        now,
    ]
    if dry_run:
        args.append("--dry-run")
    out = io.StringIO()
    code = cli.main(args, environ=ENVIRON, transport=transport, sleep=FakeTime().sleep, stdout=out)
    return code, out.getvalue()


def load_state(tmp_path: Path) -> State:
    return deserialize_state((tmp_path / "state" / "state.json").read_bytes())


def digest_texts(transport: FakeTransport) -> list[str]:
    texts = [str(payload["text"]) for _, payload in transport.requests]
    return [text for text in texts if text.startswith("<b>Guard radar weekly digest")]


def state_digests(tmp_path: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((tmp_path / "state").iterdir())
        if p.is_file()
    }


def test_weekly_digest_is_sent_once_on_the_first_run_at_or_after_the_planned_moment(
    tmp_path: Path,
) -> None:
    transport = FakeTransport([ok_response(1)])
    run_at(tmp_path, "2026-11-18T06:00:00Z", transport)

    assert len(transport.requests) == 1
    text = str(transport.requests[0][1]["text"])
    assert text.startswith("<b>Guard radar weekly digest</b>")
    assert "Last successful Guard poll: 2026-11-18 06:00 UTC." in text
    assert "Open items:" in text
    assert "1 High finding in audit.security (acme/service-a)." in text
    digest = load_state(tmp_path).digest
    assert digest.outcome == "sent"
    assert digest.planned_for == datetime(2026, 11, 18, 5, tzinfo=UTC)
    assert digest.sent_at == datetime(2026, 11, 18, 6, tzinfo=UTC)

    again = FakeTransport([])
    run_at(tmp_path, "2026-11-18T06:00:00Z", again)
    assert again.requests == []
    later = FakeTransport([])
    run_at(tmp_path, "2026-11-18T09:00:00Z", later)
    assert later.requests == []

    # A week later the fixtures also produce new alerts (older runs go stale): count the digests.
    next_week = FakeTransport([ok_response(i) for i in range(2, 12)])
    run_at(tmp_path, "2026-11-25T06:00:00Z", next_week)
    assert len(digest_texts(next_week)) == 1
    assert load_state(tmp_path).digest.planned_for == datetime(2026, 11, 25, 5, tzinfo=UTC)

    before = state_digests(tmp_path)
    dry = FakeTransport([])
    code, out = run_at(tmp_path, "2026-12-02T06:00:00Z", dry, dry_run=True)
    assert code == 0
    assert dry.requests == []
    lines = out.splitlines()
    assert lines.count("--- would send to group ---") == 1
    assert "Guard radar weekly digest" in out
    assert "follow-ups: 0 reminder message(s), digest printed" in out
    assert state_digests(tmp_path) == before
