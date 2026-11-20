"""`messages.language` through the real run: the same alerts and state, only the words differ.

No network: Telegram is a fake transport or a scripted notifier, Guard is in memory or the
synthetic fixtures.
"""

import io
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from telegram_fakes import FakeTime, FakeTransport, error_response, ok_response
from test_high_pipeline import CALM_CONFIG, Sim
from test_pipeline_ops import (
    ENVIRON,
    FIXTURES,
    OPS_CHAT_ID,
    T0,
    T3,
    T6,
    T12,
    Env,
)
from test_reminders_pipeline import Scripted

from enji_guard_radar import cli
from enji_guard_radar.model import OutboundMessage
from enji_guard_radar.texts import Language

CYRILLIC = re.compile("[А-Яа-яЁё]")
GROUP_CHAT = int(ENVIRON["TELEGRAM_CHAT_ID"])
OPS_CHAT = int(OPS_CHAT_ID)
LANGUAGES: tuple[Language, Language] = ("en", "ru")
DIGEST_DAY = "2026-11-18T06:00:00Z"


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """cli.main installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


def with_language(base: str, language: Language) -> str:
    return base + f"messages:\n  language: {language}\n"


def config_file(tmp_path: Path, language: Language) -> Path:
    path = tmp_path / f"radar-{language}.yaml"
    path.write_text(
        with_language((FIXTURES / "radar.yaml").read_text(encoding="utf-8"), language),
        encoding="utf-8",
    )
    return path


def text_of(request: tuple[str, dict[str, object]]) -> str:
    return str(request[1]["text"])


def chats(transport: FakeTransport) -> list[object]:
    return [payload["chat_id"] for _, payload in transport.requests]


# ---- alerts and reminders ---------------------------------------------------------------------


def alert_story(tmp_path: Path, language: Language) -> tuple[list[OutboundMessage], bytes]:
    """A High series appears (alert), rises (alert), and is reminded a week later."""
    root = tmp_path / language
    root.mkdir()
    sim = Sim(root, with_language(CALM_CONFIG, language), reminders_on=True)
    sent = Scripted()
    sim.poll(0, notifier=sent)
    sim.add_run(high=2, day=1)
    sim.poll(1, notifier=sent)
    sim.add_run(high=4, day=2)
    sim.poll(2, notifier=sent)
    sim.poll(9, notifier=sent)
    return sent.sent, (sim.state_dir / "state.json").read_bytes()


def test_alerts_and_reminders_are_the_same_set_in_both_languages(tmp_path: Path) -> None:
    english, english_state = alert_story(tmp_path, "en")
    russian, russian_state = alert_story(tmp_path, "ru")

    assert len(english) == len(russian) == 3
    assert [m.keys for m in english] == [m.keys for m in russian]
    assert [m.audience for m in english] == [m.audience for m in russian]
    assert russian_state == english_state  # the language is not part of the state

    assert english[0].text == "🔴 <b>Proj</b> · security High 0→2 · 0d"
    assert english[1].text == "🔴 <b>Proj</b> · security High 2→4 · 1d"
    assert english[2].text == (
        "<b>Radar reminder · 1 open</b>\n🔴 <b>Proj</b> · security High 4 · 8d"
    )

    assert russian[0].text == "🔴 <b>Proj</b> · security высокие 0→2 · 0 д"
    assert russian[1].text == "🔴 <b>Proj</b> · security высокие 2→4 · 1 д"
    assert russian[2].text == (
        "<b>Радар · напоминание · открыто: 1</b>\n🔴 <b>Proj</b> · security высокие 4 · 8 д"
    )
    assert not any(CYRILLIC.search(m.text) for m in english)


def test_switching_the_language_does_not_resend_anything(tmp_path: Path) -> None:
    sim = Sim(tmp_path, with_language(CALM_CONFIG, "en"), reminders_on=True)
    sim.poll(0)
    sim.add_run(high=2, day=1)
    first = Scripted()
    assert len(sim.poll(1, notifier=first).messages) == 1

    sim.config_text = with_language(CALM_CONFIG, "ru")
    again = Scripted()
    report = sim.poll(1, notifier=again)
    assert report.messages == ()
    assert again.sent == []
    assert sim.poll(2, notifier=again).messages == ()
    assert again.sent == []


# ---- the digest -------------------------------------------------------------------------------


def run_live(
    tmp_path: Path, state: str, now: str, language: Language, transport: FakeTransport
) -> int:
    return cli.main(
        [
            "run",
            "--fixtures",
            str(FIXTURES),
            "--config",
            str(config_file(tmp_path, language)),
            "--state-dir",
            str(tmp_path / state),
            "--env-file",
            str(tmp_path / "none.env"),
            "--now",
            now,
        ],
        environ=ENVIRON,
        transport=transport,
        sleep=FakeTime().sleep,
        stdout=io.StringIO(),
    )


def test_digest_is_sent_in_the_configured_language_with_the_same_state(tmp_path: Path) -> None:
    sent: dict[Language, FakeTransport] = {}
    for language in LANGUAGES:
        sent[language] = FakeTransport([ok_response(1)])
        assert (
            run_live(
                tmp_path, f"state-{language}", "2026-11-18T06:00:00Z", language, sent[language]
            )
            == 0
        )

    (english,) = sent["en"].requests
    (russian,) = sent["ru"].requests
    assert re.match(r"<b>Radar weekly · \d+ open</b>\nLast poll: ", text_of(english))
    assert re.match(r"<b>Радар · дайджест · открыто: \d+</b>\nПоследний опрос: ", text_of(russian))
    assert english[1]["chat_id"] == russian[1]["chat_id"] == GROUP_CHAT
    assert 'Project A &lt;core&gt; &amp; "tools"' in text_of(russian)
    assert "· security высокие 1 · 8 д" in text_of(russian)
    assert (tmp_path / "state-en" / "state.json").read_bytes() == (
        tmp_path / "state-ru" / "state.json"
    ).read_bytes()


@pytest.mark.parametrize(("before", "after"), [("en", "ru"), ("ru", "en")])
def test_switching_the_language_does_not_resend_the_digest(
    tmp_path: Path, before: Language, after: Language
) -> None:
    first = FakeTransport([ok_response(1)])
    assert run_live(tmp_path, "state", "2026-11-18T06:00:00Z", before, first) == 0
    assert len(first.requests) == 1
    second = FakeTransport([])
    assert run_live(tmp_path, "state", "2026-11-18T06:00:00Z", after, second) == 0
    assert second.requests == []
    third = FakeTransport([])
    assert run_live(tmp_path, "state", "2026-11-18T09:00:00Z", after, third) == 0
    assert third.requests == []


def test_dry_run_prints_the_same_russian_text_and_writes_nothing(tmp_path: Path) -> None:
    out = io.StringIO()
    argv = [
        "run",
        "--dry-run",
        "--fixtures",
        str(FIXTURES),
        "--config",
        str(config_file(tmp_path, "ru")),
        "--state-dir",
        str(tmp_path / "state"),
        "--env-file",
        str(tmp_path / "none.env"),
        "--now",
        "2026-11-18T06:00:00Z",
    ]
    assert cli.main(argv, environ={}, stdout=out) == 0
    printed = out.getvalue()
    live = FakeTransport([ok_response(1)])
    assert run_live(tmp_path, "live-state", "2026-11-18T06:00:00Z", "ru", live) == 0
    assert text_of(live.requests[0]) in printed  # dry-run shows exactly what a live send sends
    assert '🔴 <b>Project A &lt;core&gt; &amp; "tools"</b> · security высокие 0→1 · 8 д' in printed
    assert "--- would send to group ---" in printed
    assert "baseline, not sent (3)" in printed
    assert not (tmp_path / "state").exists()


# ---- the ops chat -----------------------------------------------------------------------------


def test_outage_and_recovery_in_russian(tmp_path: Path) -> None:
    env = Env(tmp_path)
    config = config_file(tmp_path, "ru")
    for now in (T0, T3):
        assert env.run(now, FakeTransport([]), config=config) == 1
    outage = FakeTransport([ok_response()])
    assert env.run(T6, outage, config=config) == 1
    assert chats(outage) == [OPS_CHAT]
    assert text_of(outage.requests[0]) == (
        "Guard radar не может прочитать данные Guard: Guard недоступен или сбоит. "
        "Сбои с 2026-11-16 00:00 UTC (3 неудачных опроса подряд). "
        "Радар продолжает опрос и один раз сообщит здесь, когда связь восстановится."
    )
    assert env.health().outage_reported_at is not None

    env.fixtures = env.good
    recovery = FakeTransport([ok_response()])
    assert env.run(T12, recovery, config=config) == 0
    assert chats(recovery) == [OPS_CHAT]
    assert text_of(recovery.requests[0]) == (
        "Guard radar восстановился: опросы Guard снова работают (сбои с 2026-11-16 00:00 UTC)."
    )
    assert env.health().outage_reported_at is None


def test_halt_in_russian(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.state_dir.mkdir()
    env.state_file.write_bytes(b"{")
    transport = FakeTransport([ok_response()])
    assert env.run(T0, transport, config=config_file(tmp_path, "ru")) == 2
    assert chats(transport) == [OPS_CHAT]
    assert text_of(transport.requests[0]).startswith(
        "Guard radar остановлен: файл состояния повреждён"
    )
    assert env.state_file.read_bytes() == b"{"


def test_operator_action_notice_in_russian_and_english(tmp_path: Path) -> None:
    """The group chat refuses the bot (403): the ops chat gets the action in its language."""
    texts: dict[Language, str] = {}
    states: dict[Language, bytes] = {}
    for language in LANGUAGES:
        root = tmp_path / language
        root.mkdir()
        env = Env(root)
        env.fixtures = env.good
        config = config_file(root, language)
        assert env.run("2026-11-16T12:00:00Z", FakeTransport([]), config=config) == 0  # baseline
        transport = FakeTransport(
            [error_response(403, 403, "Forbidden: bot was kicked"), ok_response()]
        )
        assert env.run("2026-12-01T12:00:00Z", transport, config=config) == 1
        assert chats(transport) == [GROUP_CHAT, OPS_CHAT]
        texts[language] = text_of(transport.requests[1])
        states[language] = env.state_file.read_bytes()

    assert texts["en"] == (
        "Guard radar needs attention:\n"
        "- Telegram refused to post to chat -1001234567890 (HTTP 403): the bot was removed, "
        "blocked or cannot post there; add it back or fix TELEGRAM_CHAT_ID"
    )
    assert texts["ru"] == (
        "Guard radar: нужно вмешательство оператора:\n"
        "- Telegram не разрешил писать в чат -1001234567890 (HTTP 403): бота удалили, "
        "заблокировали или он не может там писать; верните его в чат или поправьте TELEGRAM_CHAT_ID"
    )
    assert states["en"] == states["ru"]
