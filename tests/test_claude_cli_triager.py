"""The claude -p adapter against a fake `claude` executable: no real CLI, no network."""

import json
import logging
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fake_claude import (
    DEFAULT_CAUSE,
    SESSION_ID,
    error_envelope,
    install_fake_claude,
    ok_envelope,
    read_record,
    with_path,
)

from enji_guard_radar import claude_cli_triager
from enji_guard_radar.claude_cli_triager import ClaudeCliTriager
from enji_guard_radar.model import AlertKey, Severity
from enji_guard_radar.triage import (
    BuiltRequest,
    TriageCandidate,
    TriageFailed,
    TriageFailure,
    TriageOutcome,
    TriageReply,
    build_request,
    render_prompt,
    triage_json_schema,
    validate_reply,
)

TOKEN = "fake-claude-oauth-token-for-the-radar-tests"
TITLE = "SQL injection in order search"
NONCE = "00112233aabbccdd"


class Ticks:
    """A monotonic clock that moves 1.5 s per reading."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.5
        return self.now


def built_request() -> BuiltRequest:
    key = AlertKey(project_id="p-1", repo_id="r-1", action_key="audit.security", level="high")
    candidate = TriageCandidate(
        key=key,
        action_key="audit.security",
        high_count=1,
        age_days=3,
        titles=((Severity.HIGH, TITLE),),
    )
    return build_request([candidate], language="en", max_items=30)


def make_triager(
    *, token: str | None = TOKEN, timeout_seconds: int = 20, monotonic: Ticks | None = None
) -> ClaudeCliTriager:
    return ClaudeCliTriager(
        token=token,
        model="haiku",
        timeout_seconds=timeout_seconds,
        max_cost_usd=0.5,
        monotonic=monotonic or Ticks(),
    )


def call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    envelope: str,
    *,
    exit_code: int = 0,
    sleep: float = 0.0,
    token: str | None = TOKEN,
    timeout_seconds: int = 20,
) -> tuple[TriageOutcome, Path]:
    bin_dir = install_fake_claude(tmp_path, envelope=envelope, exit_code=exit_code, sleep=sleep)
    monkeypatch.setenv("PATH", with_path(bin_dir))
    outcome = make_triager(token=token, timeout_seconds=timeout_seconds).triage(
        built_request().request
    )
    return outcome, bin_dir


def failed(outcome: TriageOutcome) -> TriageFailed:
    assert isinstance(outcome, TriageFailed), outcome
    return outcome


def test_ok_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = install_fake_claude(tmp_path, envelope=ok_envelope(cost=0.0123))
    monkeypatch.setenv("PATH", with_path(bin_dir))
    built = built_request()

    outcome = make_triager(monotonic=Ticks()).triage(built.request)

    assert isinstance(outcome, TriageReply)
    assert outcome.cost_usd == pytest.approx(0.0123)
    assert outcome.input_tokens == 10 + 300 + 4000
    assert outcome.output_tokens == 20
    assert outcome.seconds == pytest.approx(1.5)
    accepted = validate_reply(built, outcome.text).accepted
    assert [item.cause for item in accepted.values()] == [DEFAULT_CAUSE]


def test_reply_without_cost_or_usage_keeps_them_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.loads(ok_envelope(cost=None))
    del body["usage"]
    outcome, _ = call(tmp_path, monkeypatch, json.dumps(body))
    assert isinstance(outcome, TriageReply)
    assert (outcome.cost_usd, outcome.input_tokens, outcome.output_tokens) == (None, None, None)


# --- isolation: environment, argv, cwd, stdin ---


def test_env_holds_only_path_and_the_claude_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:telegram-secret")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("GUARD_REFRESH_TOKEN", "guard-secret")
    monkeypatch.setenv("HOME", str(tmp_path))
    bin_dir = install_fake_claude(tmp_path, envelope=ok_envelope())
    monkeypatch.setenv("PATH", with_path(bin_dir))

    outcome = make_triager().triage(built_request().request)

    assert isinstance(outcome, TriageReply)
    assert read_record(bin_dir)["env_keys"] == ["CLAUDE_CODE_OAUTH_TOKEN", "PATH"]


def test_argv_follows_the_observed_flag_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bin_dir = call(tmp_path, monkeypatch, ok_envelope())
    argv: Any = read_record(bin_dir)["argv"]

    assert argv == [
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        triage_json_schema(),
        "--tools",
        "",
        "--no-session-persistence",
        "--model",
        "haiku",
        "--max-budget-usd",
        "0.5",
    ]
    assert "--bare" not in argv
    joined = "\n".join(argv)
    assert TOKEN not in joined
    assert TITLE not in joined


def test_cwd_is_an_empty_directory_removed_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bin_dir = call(tmp_path, monkeypatch, ok_envelope())
    record = read_record(bin_dir)

    assert record["cwd_listing"] == []
    assert not Path(str(record["cwd"])).exists()


def test_cwd_is_removed_after_a_failure_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, bin_dir = call(tmp_path, monkeypatch, "nope", exit_code=1)
    assert not Path(str(read_record(bin_dir)["cwd"])).exists()


def test_stdin_is_the_system_prompt_then_the_data_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secrets, "token_hex", lambda _n: NONCE)
    _, bin_dir = call(tmp_path, monkeypatch, ok_envelope())
    system, user = render_prompt(built_request().request, NONCE)

    assert read_record(bin_dir)["stdin"] == f"{system}\n\n{user}"


# --- failures: every class is a typed TriageFailed ---


def test_fail_binary_missing_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    outcome = make_triager().triage(built_request().request)
    assert failed(outcome).reason is TriageFailure.NOT_INSTALLED


@pytest.mark.parametrize("token", [None, ""])
def test_fail_no_token_is_not_logged_in_and_starts_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    outcome, bin_dir = call(tmp_path, monkeypatch, ok_envelope(), token=token)
    assert failed(outcome).reason is TriageFailure.NOT_LOGGED_IN
    assert not (bin_dir / "record.json").exists()


def test_fail_timeout_kills_the_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = time.monotonic()
    outcome, bin_dir = call(tmp_path, monkeypatch, ok_envelope(), sleep=30.0, timeout_seconds=1)
    assert failed(outcome).reason is TriageFailure.TIMEOUT
    assert time.monotonic() - started < 15
    record = read_record(bin_dir)
    pid = int(str(record["pid"]))
    for _ in range(50):  # the child is reaped before triage returns; the poll only guards a race
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not Path(str(record["cwd"])).exists()


@pytest.mark.parametrize(
    "envelope",
    [
        error_envelope(api_error_status=401, api_error_type="authentication_error"),
        error_envelope(result="Not logged in · Please run /login"),
    ],
)
def test_fail_auth_failure_is_not_logged_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, envelope: str
) -> None:
    outcome, _ = call(tmp_path, monkeypatch, envelope, exit_code=1)
    assert failed(outcome).reason is TriageFailure.NOT_LOGGED_IN


def test_fail_budget_exceeded_is_exit_nonzero_and_keeps_the_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope = error_envelope(
        subtype="error_max_budget_usd",
        terminal_reason="budget_exhausted",
        errors=["Reached maximum budget ($0.0001)"],
        total_cost_usd=0.0106,
    )
    outcome = failed(call(tmp_path, monkeypatch, envelope, exit_code=1)[0])
    assert outcome.reason is TriageFailure.EXIT_NONZERO
    assert outcome.cost_usd == pytest.approx(0.0106)
    assert (outcome.input_tokens, outcome.output_tokens) == (5, 0)


def test_fail_other_nonzero_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, _ = call(tmp_path, monkeypatch, "something broke", exit_code=1)
    assert failed(outcome).reason is TriageFailure.EXIT_NONZERO


def test_fail_stdout_that_is_not_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, _ = call(tmp_path, monkeypatch, "nope")
    assert failed(outcome).reason is TriageFailure.NOT_JSON


def test_fail_json_that_is_not_an_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, _ = call(tmp_path, monkeypatch, "[1, 2]")
    assert failed(outcome).reason is TriageFailure.NOT_JSON


def test_fail_error_flag_is_refused_or_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, _ = call(tmp_path, monkeypatch, error_envelope())
    assert failed(outcome).reason is TriageFailure.REFUSED_OR_ERROR


def test_fail_envelope_without_structured_output_is_not_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.loads(ok_envelope())
    del body["structured_output"]
    outcome, _ = call(tmp_path, monkeypatch, json.dumps(body))
    assert failed(outcome).reason is TriageFailure.NOT_JSON


def test_fail_nonce_clash_three_times_starts_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def clash(_request: object, nonce: str) -> tuple[str, str]:
        attempts.append(nonce)
        message = "nonce occurs in the request payload"
        raise ValueError(message)

    monkeypatch.setattr(claude_cli_triager, "render_prompt", clash)
    outcome, bin_dir = call(tmp_path, monkeypatch, ok_envelope())

    assert failed(outcome).reason is TriageFailure.REFUSED_OR_ERROR
    assert len(attempts) == 3
    assert len(set(attempts)) == 3
    assert not (bin_dir / "record.json").exists()


def test_fail_unexpected_exception_logs_only_its_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        message = f"spawn failed with {TOKEN} and {TITLE}"
        raise RuntimeError(message)

    monkeypatch.setattr(subprocess, "Popen", boom)
    with caplog.at_level(logging.DEBUG):
        outcome, _ = call(tmp_path, monkeypatch, ok_envelope())

    assert failed(outcome).reason is TriageFailure.REFUSED_OR_ERROR
    assert "RuntimeError" in caplog.text
    assert TOKEN not in caplog.text
    assert TITLE not in caplog.text
    assert "spawn failed" not in caplog.text


# --- logs ---


@pytest.mark.parametrize("scenario", ["ok", "failure"])
def test_logs_never_hold_the_token_the_prompt_or_the_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
) -> None:
    if scenario == "ok":
        envelope, code = ok_envelope(), 0
    else:
        envelope, code = error_envelope(result=f"secret-looking {TOKEN} {DEFAULT_CAUSE}"), 1
    with caplog.at_level(logging.DEBUG):
        outcome, _ = call(tmp_path, monkeypatch, envelope, exit_code=code)
    assert isinstance(outcome, TriageReply if scenario == "ok" else TriageFailed)

    system, _user = render_prompt(built_request().request, NONCE)
    for secret in (
        TOKEN,
        TITLE,
        SESSION_ID,
        DEFAULT_CAUSE,
        system[:40],
        "<data id=",
        "structured_output",
    ):
        assert secret not in caplog.text
