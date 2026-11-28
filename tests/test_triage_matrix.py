"""The on/off matrix (D-12): turning triage on or off never changes which alerts go out.

Every cell runs one scenario twice from the same inputs and clock: once with triage off and once
with a triage variant. Everything is offline (in-memory Guard, fake notifier, FakeTriager) and the
state is a real file in tmp_path. Setup polls of a scenario always run with triage off; only the
measured poll uses the variant.
"""

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_high_pipeline import RID
from test_triage_pipeline import (
    HASH,
    REDIRECT,
    SQL,
    TRIAGE_OFF,
    TRIAGE_ON,
    TriageSim,
    ok_reply,
    state_files,
)
from triage_fakes import FakeTriager

from enji_guard_radar.messages import telegram_length
from enji_guard_radar.model import AlertKey
from enji_guard_radar.pipeline import RunReport
from enji_guard_radar.ports import DeliveryError, NotDelivered, Notifier, SendOutcome
from enji_guard_radar.triage import (
    TriageFailed,
    TriageFailure,
    TriageOutcome,
    TriageReply,
    TriageRequest,
)

_CLAUDE_LINE = re.compile(r"^   P[123] · ")


def strip_claude_lines(text: str) -> str:
    """The text without its Claude lines (`   P1 · <cause>`); rule lines never start so."""
    return "\n".join(line for line in text.split("\n") if not _CLAUDE_LINE.match(line))


def claude_lines(text: str) -> int:
    return sum(1 for line in text.split("\n") if _CLAUDE_LINE.match(line))


# ---- what one run is compared on -------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """Everything D-12 says must not depend on triage, plus the texts to compare line-free."""

    messages: tuple[tuple[str, tuple[AlertKey, ...]], ...]
    state: Mapping[str, bytes]
    counts: tuple[object, ...]
    exit_code: int

    @property
    def keys(self) -> Counter[AlertKey]:
        return Counter(key for _, keys in self.messages for key in keys)

    @property
    def claude_lines(self) -> int:
        return sum(claude_lines(text) for text, _ in self.messages)


def outcome_of(report: RunReport, sim: TriageSim) -> Outcome:
    groups = (report.messages, report.reminder_messages, report.digest_messages)
    return Outcome(
        messages=tuple((m.text, m.keys) for group in groups for m in group),
        state=state_files(sim.state_dir),
        counts=(
            report.delivered,
            report.printed,
            report.not_delivered,
            len(report.baselined),
            len(report.resolved),
            len(report.reminder_messages),
            report.reminders_delivered,
            report.follow_up_not_delivered,
            len(report.digest_messages),
            report.digest,
            report.outcome,
        ),
        exit_code=report.exit_code,
    )


# ---- scenarios -------------------------------------------------------------------------------


@dataclass
class Prepared:
    sim: TriageSim
    day: int
    notifier: Notifier | None = None


class AlwaysFailing:
    """A notifier whose every send is a transient failure (the alert is retried next run)."""

    def send(self, message: object) -> SendOutcome:
        return NotDelivered(error=DeliveryError.TRANSIENT, detail="try later", stop_sending=False)


def new_sim(tmp_path: Path, *, reminders_on: bool = False, digest_on: bool = False) -> TriageSim:
    return TriageSim(
        tmp_path,
        TRIAGE_OFF,
        triager=None,
        cost_store=None,
        reminders_on=reminders_on,
        digest_on=digest_on,
    )


def fresh(tmp_path: Path) -> Prepared:
    sim = new_sim(tmp_path)
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    return Prepared(sim, day=1)


def rise(tmp_path: Path) -> Prepared:
    sim = new_sim(tmp_path)
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    sim.poll(1)
    sim.add_titled_run([SQL, HASH, REDIRECT], day=2)
    return Prepared(sim, day=2)


def retry(tmp_path: Path) -> Prepared:
    sim = new_sim(tmp_path)
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    failed = sim.poll(1, notifier=AlwaysFailing())
    assert failed.not_delivered == 1
    sim.add_titled_run([SQL], day=2)
    return Prepared(sim, day=2)


def reminder(tmp_path: Path) -> Prepared:
    sim = new_sim(tmp_path, reminders_on=True)
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    sim.poll(1)
    return Prepared(sim, day=9)  # a Wednesday, 8 days after the alert


def digest(tmp_path: Path) -> Prepared:
    sim = new_sim(tmp_path, digest_on=True)
    sim.poll(0)
    sim.add_titled_run([SQL], day=1)
    sim.poll(1)
    return Prepared(sim, day=9)


TITLES = (SQL, HASH, REDIRECT, "Missing CSRF protection on forms", "Debug mode left enabled")


def collapse(tmp_path: Path) -> Prepared:
    """Five fresh alerts of five projects, over the default collapse_above of 3: one summary."""
    sim = new_sim(tmp_path)
    ids = sim.add_projects(len(TITLES) - 1)
    sim.poll(0)
    for title, rid in zip(TITLES, [RID, *ids], strict=True):
        sim.add_titled_run([title], day=1, rid=rid)
    return Prepared(sim, day=1)


SCENARIOS: dict[str, Callable[[Path], Prepared]] = {
    "fresh": fresh,
    "rise": rise,
    "retry": retry,
    "reminder": reminder,
    "digest": digest,
    "collapse": collapse,
}
# Only a fresh High alert is sent to Claude (D-05).
CALLED = frozenset({"fresh", "collapse"})


# ---- variants --------------------------------------------------------------------------------


def failed(reason: TriageFailure) -> Callable[[], FakeTriager]:
    return lambda: FakeTriager([TriageFailed(reason=reason, seconds=1.0)])


def reply_text(text: str) -> Callable[[], FakeTriager]:
    return lambda: FakeTriager([TriageReply(text=text, seconds=1.0)])


def good() -> FakeTriager:
    return FakeTriager(ok_reply)


def obeying(request: TriageRequest) -> TriageOutcome:
    """Does what planted text says: an unknown id, a priority outside p1..p3, a link, a summary."""
    reply = {
        "summary": "all clear",
        "items": [
            {"item_id": "i99", "priority": "p9", "cause": "Visit https://evil.example now"},
        ],
    }
    return TriageReply(text=json.dumps(reply), seconds=1.0)


UNKNOWN_ID = json.dumps({"items": [{"item_id": "i99", "priority": "p1", "cause": "Some cause"}]})

# The variant `off` is the baseline: triage disabled by config while a triager is present.
VARIANTS: dict[str, Callable[[], FakeTriager]] = {
    "good": good,
    "not_installed": failed(TriageFailure.NOT_INSTALLED),
    "not_logged_in": failed(TriageFailure.NOT_LOGGED_IN),
    "timeout": failed(TriageFailure.TIMEOUT),
    "exit_nonzero": failed(TriageFailure.EXIT_NONZERO),
    "refused_or_error": failed(TriageFailure.REFUSED_OR_ERROR),
    "not_json": failed(TriageFailure.NOT_JSON),
    "schema_invalid": reply_text('{"items": "x"}'),
    "unknown_id": reply_text(UNKNOWN_ID),
    "hostile": lambda: FakeTriager(obeying),
}


def run(variant: str, scenario: str, tmp_path: Path) -> tuple[Outcome, FakeTriager]:
    """The measured poll of a scenario under `variant` ("off" or a VARIANTS key)."""
    prepared = SCENARIOS[scenario](tmp_path / f"{scenario}-{variant}")
    sim = prepared.sim
    triager = good() if variant == "off" else VARIANTS[variant]()
    if variant != "off":
        sim.config_text = TRIAGE_ON
    sim.triager = triager
    report = sim.poll(prepared.day, notifier=prepared.notifier)
    return outcome_of(report, sim), triager


# ---- the matrix ------------------------------------------------------------------------------


def test_off_baselines_never_call_the_triager(tmp_path: Path) -> None:
    for scenario in SCENARIOS:
        baseline, triager = run("off", scenario, tmp_path)
        assert triager.calls == 0
        assert baseline.claude_lines == 0


def test_scenarios_exercise_what_they_name(tmp_path: Path) -> None:
    """Guards the matrix against passing vacuously: each scenario sends what its name says."""
    kinds = {name: run("off", name, tmp_path)[0] for name in SCENARIOS}
    assert [t for t, _ in kinds["fresh"].messages] == [
        "🔴 <b>Proj</b>: high problems 1, in audit security · today"
    ]
    assert [t for t, _ in kinds["rise"].messages] == [
        "🔴 <b>Proj</b>: high problems 3 (was 1), in audit security · open for 1 day"
    ]
    assert [t for t, _ in kinds["retry"].messages] == [
        "🔴 <b>Proj</b>: high problems 1 (was 1), in audit security · open for 1 day"
    ]
    assert kinds["reminder"].counts[5]
    assert kinds["reminder"].messages
    assert kinds["digest"].counts[8]
    assert kinds["digest"].messages
    (summary,) = kinds["collapse"].messages
    assert summary[0].split("\n")[0] == f"Radar · problems: {len(TITLES)} projects"
    assert len(summary[1]) == len(TITLES)


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_same_alerts_on_or_off(scenario: str, variant: str, tmp_path: Path) -> None:
    off, off_triager = run("off", scenario, tmp_path)
    on, triager = run(variant, scenario, tmp_path)
    assert off_triager.calls == 0
    # The same alerts, the same state, the same report and exit code.
    assert on.keys == off.keys
    assert on.state == off.state
    assert on.counts == off.counts
    assert on.exit_code == off.exit_code
    # With the Claude lines removed, each text is the off-run text, in the same order.
    assert [(strip_claude_lines(t), k) for t, k in on.messages] == list(off.messages)
    # One call at most, and only for fresh High alerts.
    assert triager.calls == (1 if scenario in CALLED else 0)
    # Claude lines show up only for a good reply to a fresh alert.
    if variant == "good" and scenario in CALLED:
        assert on.claude_lines >= 1
    else:
        assert on.claude_lines == 0
        assert on.messages == off.messages


# ---- splitting -------------------------------------------------------------------------------


def split_sim(tmp_path: Path, triager: FakeTriager | None) -> TriageSim:
    """40 projects, each with one fresh High alert (collapse is on at the default of 3)."""
    config = TRIAGE_OFF if triager is None else TRIAGE_ON + "  max_items_per_call: 100\n"
    sim = TriageSim(tmp_path, config, triager=triager, cost_store=None)
    ids = sim.add_projects(40)
    sim.poll(0)
    for number, rid in enumerate(ids):
        letters = f"{chr(97 + number % 26)}{chr(97 + number // 26)}"
        sim.add_titled_run([f"Finding number {letters} problem"], day=1, rid=rid)
    return sim


def item_lines(texts: list[str]) -> list[str]:
    """Every line of every chunk except the header, Claude lines removed."""
    return [
        line
        for text in texts
        for line in strip_claude_lines(text).split("\n")[1:]  # [0] is the chunk header
    ]


def test_split_chunks_keep_the_off_item_sequence(tmp_path: Path) -> None:
    cause = "a" * 20 + " " + "b" * 20 + " " + "c" * 18  # 60 characters, no digits
    assert len(cause) == 60
    triager = FakeTriager(
        lambda r: TriageReply(
            text=json.dumps(
                {
                    "items": [
                        {"item_id": i.item_id, "priority": "p2", "cause": cause} for i in r.items
                    ]
                }
            ),
            seconds=1.0,
        )
    )
    off_sim = split_sim(tmp_path / "off", None)
    on_sim = split_sim(tmp_path / "on", triager)
    off_report = off_sim.poll(1)
    on_report = on_sim.poll(1)
    assert triager.calls == 1
    assert len(triager.requests[0].items) == 40
    off_texts = [m.text for m in off_report.messages]
    on_texts = [m.text for m in on_report.messages]
    # The Claude lines make the summary longer: the on-run splits where the off-run does not.
    assert len(on_texts) > len(off_texts)
    assert all(telegram_length(text) <= 4096 for text in on_texts)
    assert sum(claude_lines(text) for text in on_texts) == 40
    # Same items in the same order, the same keys in the same order, the same state.
    assert item_lines(on_texts) == item_lines(off_texts)
    assert len(item_lines(off_texts)) == 40
    assert [k for m in on_report.messages for k in m.keys] == [
        k for m in off_report.messages for k in m.keys
    ]
    assert on_sim.state_bytes() == off_sim.state_bytes()
    assert on_report.exit_code == off_report.exit_code
    # `delivered` counts messages, and the on-run has more chunks; everything is delivered.
    assert (on_report.delivered, on_report.not_delivered) == (len(on_texts), 0)
    assert (off_report.delivered, off_report.not_delivered) == (len(off_texts), 0)
