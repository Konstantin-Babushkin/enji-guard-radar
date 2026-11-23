"""The `claude -p` adapter behind the `Triager` port (D-01, D-02, D-11, D-13, D-15). Shell module.

It moves bytes and nothing else: the request is rendered by `triage.render_prompt`, the reply is
validated by the pipeline. Everything about the command line, the environment and the envelope
comes from the OBSERVED lines of docs/triage-notes.md (claude v2.1.325); a comment names the
notes section next to each such fact, and `ASSUMPTION:` marks what the notes leave open.

Isolation: the child sees only PATH and the Claude token, runs in a fresh empty temporary
directory with tools disabled and no session persistence, gets the prompt on stdin, and is
killed as a whole process group when it outlives `timeout_seconds`. stdout is parsed and
dropped, never logged; stderr is not read. Every failure is a typed `TriageFailed`.
"""

import contextlib
import json
import logging
import math
import os
import secrets
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from enji_guard_radar.settings import ENV_CLAUDE_TOKEN
from enji_guard_radar.triage import (
    TriageFailed,
    TriageFailure,
    TriageOutcome,
    TriageReply,
    TriageRequest,
    render_prompt,
    triage_json_schema,
)

logger = logging.getLogger(__name__)

_NONCE_ATTEMPTS = 3
_INPUT_TOKEN_FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _cost(envelope: Mapping[str, object]) -> float | None:
    # docs/triage-notes.md "JSON envelope": `total_cost_usd` (number).
    value = envelope.get("total_cost_usd")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _tokens(envelope: Mapping[str, object]) -> tuple[int | None, int | None]:
    """(input, output) from `usage` (triage-notes "JSON envelope"); input adds the two cache fields.

    The notes show most of the input arriving as cache creation or cache read, so the plain
    `input_tokens` alone would understate it. Both are None unless the two base fields are ints.
    """
    usage = envelope.get("usage")
    if not isinstance(usage, dict):
        return None, None
    plain, output = usage.get("input_tokens"), usage.get("output_tokens")
    if not _is_count(plain) or not _is_count(output):
        return None, None
    total = sum(usage[name] for name in _INPUT_TOKEN_FIELDS if _is_count(usage.get(name)))
    return total, output


def _is_auth_failure(envelope: Mapping[str, object]) -> bool:
    # Notes "Exit codes and errors": a junk token gives api_error_status 401 and
    # api_error_type authentication_error; no login at all gives the result "Not logged in ...".
    status = envelope.get("api_error_status")
    if status == 401:
        return True
    if envelope.get("api_error_type") == "authentication_error":
        return True
    result = envelope.get("result")
    return isinstance(result, str) and result.startswith("Not logged in")


class ClaudeCliTriager:
    def __init__(
        self,
        *,
        binary: str = "claude",
        token: str | None,
        model: str,
        timeout_seconds: int,
        max_cost_usd: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._binary = binary
        self._token = token
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_cost_usd = max_cost_usd
        self._monotonic = monotonic

    def triage(self, request: TriageRequest) -> TriageOutcome:
        """Never raises (D-11): every failure comes back as a TriageFailed."""
        started = self._monotonic()
        try:
            return self._triage(request, started)
        except Exception as exc:
            # The one broad except in the module (D-11): whatever goes wrong while spawning,
            # feeding or reading the child must end as a rule-only message, never as a crash.
            # Only the class name is logged: the text of an error could hold prompt or token.
            logger.warning("triage call failed unexpectedly: %s", type(exc).__name__)
            return self._failed(TriageFailure.REFUSED_OR_ERROR, started)

    def _failed(
        self,
        reason: TriageFailure,
        started: float,
        envelope: Mapping[str, object] | None = None,
    ) -> TriageFailed:
        cost = _cost(envelope) if envelope is not None else None
        input_tokens, output_tokens = _tokens(envelope) if envelope is not None else (None, None)
        return TriageFailed(
            reason=reason,
            seconds=self._monotonic() - started,
            cost_usd=cost,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def _triage(self, request: TriageRequest, started: float) -> TriageOutcome:
        token = self._token
        if not token:
            logger.info("claude token not set: no triage call")
            return self._failed(TriageFailure.NOT_LOGGED_IN, started)
        env = self._env(token)
        executable = shutil.which(self._binary, path=env["PATH"])
        if executable is None:
            logger.warning("claude binary %r not found on PATH", self._binary)
            return self._failed(TriageFailure.NOT_INSTALLED, started)
        prompt = self._render(request)
        if prompt is None:
            logger.warning(
                "triage prompt could not be built: nonce clashed %d times", _NONCE_ATTEMPTS
            )
            return self._failed(TriageFailure.REFUSED_OR_ERROR, started)
        tmpdir = Path(tempfile.mkdtemp(prefix="radar-triage-"))
        try:
            return self._run(executable, env, tmpdir, prompt, started)
        finally:
            try:
                shutil.rmtree(tmpdir)
            except OSError as exc:
                logger.warning("triage temp dir not removed: %s", type(exc).__name__)

    @staticmethod
    def _render(request: TriageRequest) -> bytes | None:
        """System prompt, blank line, data block; None when no nonce is free of the payload.

        ASSUMPTION: the system prompt rides on stdin above the data block. The notes list
        `--system-prompt` in `--help` only ("Flags"); the working flag set was never run with it.
        """
        for _ in range(_NONCE_ATTEMPTS):
            try:
                system, user = render_prompt(request, secrets.token_hex(8))
            except ValueError:
                continue
            return f"{system}\n\n{user}".encode()
        return None

    def _env(self, token: str) -> dict[str, str]:
        # Notes "Login and environment": `env -i PATH=... CLAUDE_CODE_OAUTH_TOKEN=...` is enough,
        # HOME is not needed. Nothing else is inherited (D-15): no TELEGRAM_*, no Guard variables.
        return {"PATH": os.environ.get("PATH", os.defpath), ENV_CLAUDE_TOKEN: token}

    def _argv(self, executable: str, schema: str) -> list[str]:
        return [
            executable,
            "-p",  # triage-notes "Flags": the working flag set; prompt on stdin
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--tools",
            "",  # triage-notes "Flags": tools disabled
            "--no-session-persistence",
            # Never `--bare`: it ignores the OAuth token (triage-notes "Flags").
            "--model",
            self._model,  # triage-notes "Model": the radar always passes --model
            # Notes "Budget flag": honoured only after the cost is spent, so a per-call
            # backstop; the per-day cost file is the real limit.
            "--max-budget-usd",
            f"{self._max_cost_usd:g}",
        ]

    def _run(
        self,
        executable: str,
        env: dict[str, str],
        tmpdir: Path,
        prompt: bytes,
        started: float,
    ) -> TriageOutcome:
        argv = self._argv(executable, triage_json_schema())
        logger.info("claude call: model %s, timeout %ds", self._model, self._timeout_seconds)
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=tmpdir,
            env=env,
            start_new_session=True,  # own process group, so a timeout can kill all of it (D-13)
        )
        try:
            stdout, _ = process.communicate(prompt, timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired:
            logger.warning(
                "claude call timed out after %ds: process group killed", self._timeout_seconds
            )
            return self._failed(TriageFailure.TIMEOUT, started)
        finally:
            self._kill_group(process)
        return self._interpret(process.returncode, stdout, started)

    @staticmethod
    def _kill_group(process: "subprocess.Popen[bytes]") -> None:
        """Kill the whole group and reap the child, unless it already finished."""
        if process.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()

    def _interpret(self, returncode: int, stdout: bytes, started: float) -> TriageOutcome:
        """Exit code and envelope to an outcome. Nothing of stdout is logged."""
        try:
            parsed = json.loads(stdout)
        except ValueError:
            parsed = None
        envelope = parsed if isinstance(parsed, dict) else None
        if returncode != 0:
            # Notes "Exit codes and errors": junk token and budget exceeded both exit 1 with
            # is_error true; only the auth signature is told apart.
            auth = envelope is not None and _is_auth_failure(envelope)
            reason = TriageFailure.NOT_LOGGED_IN if auth else TriageFailure.EXIT_NONZERO
            return self._failed(reason, started, envelope)
        if envelope is None:
            return self._failed(TriageFailure.NOT_JSON, started)
        # Notes "JSON envelope": `is_error` is the error flag. A missing flag is not trusted.
        # ASSUMPTION: a model refusal was not probed (triage-notes "Exit codes and errors"); it is
        # treated like any other is_error call.
        if envelope.get("is_error") is not False:
            return self._failed(TriageFailure.REFUSED_OR_ERROR, started, envelope)
        # Notes "JSON envelope": `structured_output` is the parsed object matching the schema.
        structured = envelope.get("structured_output")
        if not isinstance(structured, dict):
            return self._failed(TriageFailure.NOT_JSON, started, envelope)
        input_tokens, output_tokens = _tokens(envelope)
        return TriageReply(
            text=json.dumps(structured, separators=(",", ":"), ensure_ascii=False),
            seconds=self._monotonic() - started,
            cost_usd=_cost(envelope),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
