"""Token layer shell: refresh, rotation, persistence, lock. Real files in tmp_path, fake network."""

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fake_oauth import (
    FAKE_REFRESH_TOKEN,
    ROTATED_ACCESS_TOKEN,
    ROTATED_REFRESH_TOKEN,
    TOKEN_ENDPOINT,
    FakeOAuthTransport,
    make_credentials,
    token_body,
)

from enji_guard_radar.guard_auth import GUARD_MCP_URL, deserialize_credentials
from enji_guard_radar.guard_credentials import CredentialStore, RefreshingTokenSource

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def fixed_clock() -> datetime:
    return NOW


def test_expired_token_is_refreshed_and_rotation_saved_before_use(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "state")
    store.save(
        make_credentials(
            expires_at=NOW - timedelta(minutes=1), obtained_at=NOW - timedelta(hours=1)
        )
    )
    transport = FakeOAuthTransport([token_body(ROTATED_ACCESS_TOKEN, ROTATED_REFRESH_TOKEN)])
    source = RefreshingTokenSource(store, transport, fixed_clock)

    assert source.access_token() == ROTATED_ACCESS_TOKEN

    assert len(transport.requests) == 1
    url, form = transport.requests[0]
    assert url == TOKEN_ENDPOINT
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == FAKE_REFRESH_TOKEN
    assert form["resource"] == GUARD_MCP_URL
    saved = deserialize_credentials(store.path.read_bytes())
    assert saved.access_token.get_secret_value() == ROTATED_ACCESS_TOKEN
    assert saved.refresh_token.get_secret_value() == ROTATED_REFRESH_TOKEN
    assert saved.expires_at == NOW + timedelta(seconds=3600)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    assert source.access_token() == ROTATED_ACCESS_TOKEN
    assert len(transport.requests) == 1
