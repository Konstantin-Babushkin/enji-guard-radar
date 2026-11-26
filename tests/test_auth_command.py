"""`enji-guard-radar auth` through cli.main with fake Fleet answers and a fake hidden prompt."""

import fcntl
import io
import json
import logging
import os
import stat
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fake_oauth import (
    FAKE_ACCESS_TOKEN,
    FAKE_REFRESH_TOKEN,
    REGISTRATION_ENDPOINT,
    TOKEN_ENDPOINT,
    FakeLoginTransport,
    json_response,
    login_routes,
    oauth_error,
    token_body,
)

from enji_guard_radar import cli, cli_auth
from enji_guard_radar.guard_auth import GuardCredentials
from enji_guard_radar.guard_credentials import (
    CREDENTIALS_LOCK,
    CredentialStore,
    UrllibOAuthTransport,
)
from enji_guard_radar.guard_login import REDIRECT_URI, LoginError
from enji_guard_radar.telegram import TransportFailure


@pytest.fixture(autouse=True)
def restore_root_logging() -> Iterator[None]:
    """`auth` installs a redacting handler on the root logger; undo it after each test."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


FAKE_CODE = "fake-code-0001"
ISS = "https%3A%2F%2Ffleet.enji.ai"


def authorize_query(out: io.StringIO) -> dict[str, list[str]]:
    urls = [line for line in out.getvalue().splitlines() if line.startswith("https://")]
    assert len(urls) == 1
    return urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)


def good_redirect(out: io.StringIO) -> str:
    state = authorize_query(out)["state"][0]
    return f"{REDIRECT_URI}?code={FAKE_CODE}&state={state}&iss={ISS}"


def answering(build: Callable[[io.StringIO], str]) -> tuple[io.StringIO, Callable[[str], str]]:
    out = io.StringIO()
    return out, lambda _text: build(out)


def run_auth(
    state_dir: Path,
    transport: FakeLoginTransport,
    build: Callable[[io.StringIO], str],
    *extra: str,
) -> tuple[int, io.StringIO]:
    out, prompt = answering(build)
    code = cli.main(
        ["auth", "--state-dir", str(state_dir), *extra],
        oauth_transport=transport,
        prompt=prompt,
        stdout=out,
    )
    return code, out


def test_auth_stores_a_read_only_login(tmp_path: Path) -> None:
    fake = FakeLoginTransport(login_routes())

    code, out = run_auth(tmp_path, fake, good_redirect)

    assert code == 0
    stored_file = tmp_path / "guard-auth.json"
    assert stat.S_IMODE(stored_file.stat().st_mode) == 0o600
    stored = json.loads(stored_file.read_text())
    assert stored["refresh_token"] == FAKE_REFRESH_TOKEN
    assert stored["send_resource"] is True
    registration = fake.payloads(REGISTRATION_ENDPOINT)[0]
    assert isinstance(registration, dict)
    assert registration["scope"] == "projects:read audits:read"
    assert registration["redirect_uris"] == [REDIRECT_URI]
    query = authorize_query(out)
    assert query["scope"] == ["projects:read audits:read"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == ["https://guard.enji.ai/mcp"]
    form = fake.payloads(TOKEN_ENDPOINT)[0]
    assert isinstance(form, dict)
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == FAKE_CODE
    assert form["redirect_uri"] == REDIRECT_URI
    assert form["resource"] == "https://guard.enji.ai/mcp"
    assert len(form["code_verifier"]) >= 43
    assert "refresh token issued: yes" in out.getvalue()
    assert "Next: run `enji-guard-radar doctor`." in out.getvalue()


# --- refusals: each exits 1, says why on stderr, stores nothing -------------------------------


def with_state(out: io.StringIO, tail: str) -> str:
    return f"{REDIRECT_URI}?{tail}&state={authorize_query(out)['state'][0]}"


REDIRECT_ROWS: dict[str, Callable[[io.StringIO], str]] = {
    "wrong_state": lambda out: f"{REDIRECT_URI}?code={FAKE_CODE}&state=nope&iss={ISS}",
    "missing_state": lambda out: f"{REDIRECT_URI}?code={FAKE_CODE}&iss={ISS}",
    "two_state_values": lambda out: good_redirect(out) + "&state=other",
    "missing_iss": lambda out: with_state(out, f"code={FAKE_CODE}"),
    "foreign_iss": lambda out: with_state(out, f"code={FAKE_CODE}&iss=https%3A%2F%2Fevil.example"),
    "error_param": lambda out: with_state(out, f"error=access_denied&iss={ISS}"),
    "missing_code": lambda out: with_state(out, f"iss={ISS}"),
    "https_scheme": lambda out: good_redirect(out).replace("http://", "https://", 1),
    "other_host": lambda out: good_redirect(out).replace("127.0.0.1", "localhost", 1),
    "other_port": lambda out: good_redirect(out).replace(":8765", ":8766", 1),
    "other_path": lambda out: good_redirect(out).replace("/callback", "/other", 1),
}


@pytest.mark.parametrize("row", sorted(REDIRECT_ROWS))
def test_auth_rejects_a_bad_redirect_state_iss_or_address(
    row: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeLoginTransport(login_routes())

    code, out = run_auth(tmp_path, fake, REDIRECT_ROWS[row])

    assert code == 1
    err = capsys.readouterr().err
    assert "error: " in err
    assert not (tmp_path / "guard-auth.json").exists()
    assert fake.payloads(TOKEN_ENDPOINT) == []
    assert FAKE_CODE not in err + out.getvalue()


def test_auth_refuses_a_grant_without_a_refresh_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeLoginTransport(login_routes(token=token_body(FAKE_ACCESS_TOKEN)))

    code, _out = run_auth(tmp_path, fake, good_redirect)

    assert code == 1
    assert "no refresh token was issued" in capsys.readouterr().err
    assert not (tmp_path / "guard-auth.json").exists()


@pytest.mark.parametrize("scope", ["projects:read audits:read audits:run", "projects:write"])
def test_auth_refuses_audits_run_or_broader_scope(
    scope: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = token_body(FAKE_ACCESS_TOKEN, FAKE_REFRESH_TOKEN, scope=scope)
    fake = FakeLoginTransport(login_routes(token=token))

    code, out = run_auth(tmp_path, fake, good_redirect)

    assert code == 1
    assert "broader than read-only" in capsys.readouterr().err
    assert not (tmp_path / "guard-auth.json").exists()
    registration = fake.payloads(REGISTRATION_ENDPOINT)[0]
    assert isinstance(registration, dict)
    assert "audits:run" not in json.dumps(registration)
    assert "audits%3Arun" not in out.getvalue()


def test_auth_audits_run_is_never_requested(tmp_path: Path) -> None:
    fake = FakeLoginTransport(login_routes())

    _code, out = run_auth(tmp_path, fake, good_redirect)

    assert "audits:run" not in json.dumps(fake.requests)
    assert "audits%3Arun" not in out.getvalue()


def test_auth_reports_a_registration_refusal_with_a_cut_description(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = login_routes()
    refusal = {"error": "invalid_redirect_uri", "error_description": "x" * 500}
    routes[("POST", REGISTRATION_ENDPOINT)] = [json_response(refusal, status=400)]

    code, _out = run_auth(tmp_path, FakeLoginTransport(routes), good_redirect)

    assert code == 1
    err = capsys.readouterr().err
    assert "invalid_redirect_uri" in err
    assert "x" * 200 in err
    assert "x" * 201 not in err


def test_auth_refuses_discovery_metadata_that_is_not_fleet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    routes = login_routes()
    bad = {
        "resource": "https://guard.enji.ai/mcp",
        "authorization_servers": ["https://evil.example"],
    }
    (key,) = [k for k in routes if k[1].endswith("oauth-protected-resource/mcp")]
    routes[key] = [json_response(bad)]

    code, _out = run_auth(tmp_path, FakeLoginTransport(routes), good_redirect)

    assert code == 1
    assert "unexpected authorization server" in capsys.readouterr().err


def test_auth_reports_a_rejected_code_exchange(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeLoginTransport(login_routes(token=oauth_error(400, "invalid_grant")))

    code, _out = run_auth(tmp_path, fake, good_redirect)

    assert code == 1
    assert "invalid_grant" in capsys.readouterr().err
    assert not (tmp_path / "guard-auth.json").exists()


def test_auth_reports_an_unreachable_login_service(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeLoginTransport(login_routes(token=TransportFailure("timeout", "request timed out")))

    code, _out = run_auth(tmp_path, fake, good_redirect)

    assert code == 1
    assert "unreachable (timeout)" in capsys.readouterr().err


def test_auth_reports_a_store_that_cannot_write_without_a_chained_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_save(_self: CredentialStore, _credentials: GuardCredentials) -> None:
        message = "disk detail that must not be shown"
        raise PermissionError(message)

    raised: list[BaseException] = []
    real_login = cli_auth._login

    def spying_login(*args: Any, **kwargs: Any) -> None:
        try:
            real_login(*args, **kwargs)
        except BaseException as exc:
            raised.append(exc)
            raise

    monkeypatch.setattr(CredentialStore, "save", failing_save)
    monkeypatch.setattr(cli_auth, "_login", spying_login)
    caplog.set_level(logging.INFO)
    fake = FakeLoginTransport(login_routes())

    code, out = run_auth(tmp_path, fake, good_redirect)

    expected = f"the login could not be written to {tmp_path / 'guard-auth.json'} (PermissionError)"
    assert code == 1
    assert len(fake.payloads(TOKEN_ENDPOINT)) == 1
    assert not (tmp_path / "guard-auth.json").exists()
    err = capsys.readouterr().err
    assert f"error: {expected}\n" in err
    assert "disk detail" not in err + out.getvalue() + caplog.text
    assert "Traceback" not in err + caplog.text
    assert "refresh token issued" not in out.getvalue()
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.getMessage() == f"Guard login failed: {expected}"
    assert record.exc_info is None
    [error] = raised
    assert isinstance(error, LoginError)
    assert error.reason == expected
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_auth_reports_a_login_lock_that_stays_held(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def impatient_store(directory: Path) -> CredentialStore:
        return CredentialStore(directory, sleep=lambda _s: None, lock_wait_s=0.0)

    monkeypatch.setattr(cli_auth, "CredentialStore", impatient_store)
    caplog.set_level(logging.INFO)
    fake = FakeLoginTransport(login_routes())
    holder = tmp_path / CREDENTIALS_LOCK
    holder_fd = os.open(holder, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        code, out = run_auth(tmp_path, fake, good_redirect)
    finally:
        os.close(holder_fd)

    assert code == 1
    busy = "Guard login is busy in another radar process"
    err = capsys.readouterr().err
    assert f"error: {busy}\n" in err
    assert "Traceback" not in err + caplog.text
    assert out.getvalue() == ""
    assert fake.requests == []
    assert not (tmp_path / "guard-auth.json").exists()
    [record] = [r for r in caplog.records if r.getMessage() == f"Guard login failed: {busy}"]
    assert record.levelno == logging.ERROR
    assert record.exc_info is None
    # the failed attempt released nothing it did not hold: the lock is free again afterwards
    code, _out = run_auth(tmp_path, FakeLoginTransport(login_routes()), good_redirect)
    assert code == 0


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
def test_auth_abort_at_the_prompt_stores_nothing(
    interrupt: type[BaseException], tmp_path: Path
) -> None:
    out = io.StringIO()

    def prompt(_text: str) -> str:
        raise interrupt

    code = cli.main(
        ["auth", "--state-dir", str(tmp_path)],
        oauth_transport=FakeLoginTransport(login_routes()),
        prompt=prompt,
        stdout=out,
    )

    assert code == 1
    assert "aborted; nothing stored" in out.getvalue()
    assert not (tmp_path / "guard-auth.json").exists()


def test_auth_keeps_an_existing_login_when_a_new_one_fails(tmp_path: Path) -> None:
    existing = tmp_path / "guard-auth.json"
    existing.write_bytes(b'{"keep": "me"}\n')
    for build in (REDIRECT_ROWS["wrong_state"], REDIRECT_ROWS["foreign_iss"]):
        code, _out = run_auth(tmp_path, FakeLoginTransport(login_routes()), build)
        assert code == 1
    code, _out = run_auth(
        tmp_path,
        FakeLoginTransport(login_routes(token=token_body(FAKE_ACCESS_TOKEN))),
        good_redirect,
    )
    assert code == 1

    assert existing.read_bytes() == b'{"keep": "me"}\n'


def test_auth_no_resource_indicator_drops_resource_and_records_it(tmp_path: Path) -> None:
    fake = FakeLoginTransport(login_routes())

    code, out = run_auth(tmp_path, fake, good_redirect, "--no-resource-indicator")

    assert code == 0
    assert "resource" not in authorize_query(out)
    form = fake.payloads(TOKEN_ENDPOINT)[0]
    assert isinstance(form, dict)
    assert "resource" not in form
    assert json.loads((tmp_path / "guard-auth.json").read_text())["send_resource"] is False


def test_auth_secrecy_no_secret_in_output_or_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    rows = [REDIRECT_ROWS["wrong_state"], REDIRECT_ROWS["foreign_iss"], good_redirect]
    pasted: list[str] = []
    verifiers: list[str] = []
    shown: list[str] = []
    for index, row in enumerate(rows):
        fake = FakeLoginTransport(login_routes())

        def recording(out: io.StringIO, row: Callable[[io.StringIO], str] = row) -> str:
            pasted.append(row(out))
            return pasted[-1]

        code, out = run_auth(tmp_path / str(index), fake, recording)

        assert code == (0 if row is good_redirect else 1)
        lines = out.getvalue().splitlines()
        authorize = [line for line in lines if line.startswith("https://")]
        assert len(authorize) == 1
        assert authorize[0] not in caplog.text
        shown += [line for line in lines if line != authorize[0]]
        shown.append(capsys.readouterr().err)
        for form in fake.payloads(TOKEN_ENDPOINT):
            assert isinstance(form, dict)
            verifiers.append(form["code_verifier"])
    everything = "\n".join(shown) + caplog.text
    assert verifiers
    for secret in (FAKE_CODE, *verifiers, *pasted, FAKE_ACCESS_TOKEN, FAKE_REFRESH_TOKEN):
        assert secret not in everything


def test_auth_help_lists_its_options(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["auth", "--help"])

    help_text = capsys.readouterr().out
    assert "--state-dir" in help_text
    assert "--no-resource-indicator" in help_text


def test_urllib_login_transport_gets_and_posts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[urllib.request.Request] = []

    class Answer:
        status = 201

        def __enter__(self) -> "Answer":
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> Answer:
        del timeout
        seen.append(request)
        return Answer()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    transport = UrllibOAuthTransport()
    got = transport.get_json(TOKEN_ENDPOINT, 1.0)
    posted = transport.post_json(TOKEN_ENDPOINT, {"client_name": "x"}, 1.0)

    assert (got.status, posted.status) == (201, 201)
    get_request, post_request = seen
    assert get_request.get_method() == "GET"
    assert get_request.data is None
    assert get_request.get_header("Accept") == "application/json"
    assert post_request.get_method() == "POST"
    assert post_request.data == b'{"client_name": "x"}'
    assert post_request.get_header("Content-type") == "application/json"
