"""The one-time `auth` command: the operator's interactive, read-only Guard login.

The shell around `guard_login`: HTTP through a `LoginTransport`, the redirect address through a
hidden prompt (never argv, env or a log), the result into the credential file under its lock.
Nothing secret is printed or logged; the authorize URL goes to stdout only.
"""

import argparse
import getpass
import logging
import secrets
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from enji_guard_radar.guard_auth import (
    GUARD_PRM_URL,
    READ_SCOPES,
    classify_token_response,
)
from enji_guard_radar.guard_credentials import CredentialStore, LoginTransport
from enji_guard_radar.guard_login import (
    AS_METADATA_PATH,
    LoginError,
    ServerMetadata,
    authorize_url,
    code_challenge,
    code_exchange_form,
    credentials_from_grant,
    grant_summary,
    parse_document,
    parse_redirect,
    parse_registration,
    registration_request,
    validate_protected_resource,
    validate_server_metadata,
)
from enji_guard_radar.ports import Clock, GuardCallError
from enji_guard_radar.secret_guard import configure_logging
from enji_guard_radar.telegram import TransportFailure

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "state"
TIMEOUT_S = 30.0
VERIFIER_BYTES = 64
STATE_BYTES = 32
PROMPT_TEXT = "Redirect address: "
ABORTED_TEXT = "aborted; nothing stored"


class _AbortedError(Exception):
    """The operator pressed Ctrl-C or closed the input at the prompt."""


def add_parser(commands: Any) -> None:
    auth = commands.add_parser(
        "auth",
        help=(
            "create the radar's own read-only Guard login (interactive, run once on the radar host)"
        ),
    )
    auth.add_argument(
        "--state-dir",
        type=Path,
        default=Path(DEFAULT_STATE_DIR),
        metavar="DIR",
        help=f"directory for guard-auth.json (default: {DEFAULT_STATE_DIR})",
    )
    auth.add_argument(
        "--no-resource-indicator",
        action="store_true",
        help="leave the RFC 8707 resource parameter out of the login and later refreshes",
    )


def _discover(transport: LoginTransport) -> ServerMetadata:
    prm = transport.get_json(GUARD_PRM_URL, TIMEOUT_S)
    issuer = validate_protected_resource(
        parse_document(prm.status, prm.body, what="protected resource metadata")
    )
    answer = transport.get_json(issuer + AS_METADATA_PATH, TIMEOUT_S)
    meta = validate_server_metadata(
        parse_document(answer.status, answer.body, what="authorization server metadata"),
        expected_issuer=issuer,
    )
    logger.info("Guard login: discovery ok")
    return meta


def _login(
    args: argparse.Namespace,
    *,
    out: TextIO,
    transport: LoginTransport,
    prompt: Callable[[str], str],
    clock: Clock,
    new_secret: Callable[[int], str],
    store: CredentialStore,
) -> None:
    send_resource = not args.no_resource_indicator
    meta = _discover(transport)
    registered = transport.post_json(meta.registration_endpoint, registration_request(), TIMEOUT_S)
    client_id = parse_registration(registered.status, registered.body)
    logger.info("Guard login: client registered")

    verifier = new_secret(VERIFIER_BYTES)
    state = new_secret(STATE_BYTES)
    url = authorize_url(
        meta,
        client_id=client_id,
        state=state,
        challenge=code_challenge(verifier),
        send_resource=send_resource,
    )
    scopes = ", ".join(READ_SCOPES)
    print(
        "Open this address in a browser on your own computer and approve read-only access "
        f"({scopes}):",
        file=out,
    )
    print(url, file=out)
    print(
        "The browser then fails to load a 127.0.0.1 page. Copy the full address from its "
        "address bar and paste it below. The input is hidden; never paste it into a chat.",
        file=out,
    )
    out.flush()
    try:
        pasted = prompt(PROMPT_TEXT)
    except (KeyboardInterrupt, EOFError):
        raise _AbortedError from None
    code = parse_redirect(pasted, expected_state=state, issuer=meta.issuer)
    del pasted

    form = code_exchange_form(
        code=code, client_id=client_id, verifier=verifier, send_resource=send_resource
    )
    del code
    response = transport.post_form(meta.token_endpoint, form, TIMEOUT_S)
    now = clock()
    outcome = classify_token_response(response.status, response.body, now)
    credentials = credentials_from_grant(
        outcome, meta, client_id=client_id, send_resource=send_resource, now=now
    )
    try:
        store.save(credentials)
    except OSError as exc:
        message = f"the login could not be written to {store.path} ({type(exc).__name__})"
        raise LoginError(message) from None
    logger.info("Guard login: login stored in %s", store.path)
    print(grant_summary(credentials), file=out)
    print("Next: run `enji-guard-radar doctor`.", file=out)


def run_auth(
    args: argparse.Namespace,
    *,
    out: TextIO,
    transport: LoginTransport,
    prompt: Callable[[str], str] | None,
    clock: Clock,
    new_secret: Callable[[int], str] | None,
) -> int:
    configure_logging(level=logging.INFO, known_secrets=(), stream=sys.stderr)
    store = CredentialStore(args.state_dir)
    try:
        with store.lock():
            _login(
                args,
                out=out,
                transport=transport,
                prompt=prompt if prompt is not None else getpass.getpass,
                clock=clock,
                new_secret=new_secret if new_secret is not None else secrets.token_urlsafe,
                store=store,
            )
    except _AbortedError:
        logger.info("Guard login aborted at the prompt; nothing stored")
        print(ABORTED_TEXT, file=out)
        return 1
    except LoginError as error:
        logger.error("Guard login failed: %s", error.reason)  # noqa: TRY400 - no traceback
        print(f"error: {error.operator_text()}", file=sys.stderr)
        return 1
    except TransportFailure as error:
        reason = f"the login service is unreachable ({error.kind})"
        logger.error("Guard login failed: %s", reason)  # noqa: TRY400 - no traceback
        print(f"error: {reason}", file=sys.stderr)
        return 1
    except GuardCallError as error:
        logger.error("Guard login failed: %s", error)  # noqa: TRY400 - no traceback
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0
