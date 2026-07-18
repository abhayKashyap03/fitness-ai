"""WHOOP OAuth: expiry math, code exchange, refresh, token store — all mocked."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from coach.adapters.whoop.auth import (
    ReauthRequired,
    TokenSet,
    TokenStore,
    WhoopOAuth,
)

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)


def _oauth(handler) -> WhoopOAuth:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return WhoopOAuth("cid", "secret", "http://localhost:8080/callback", client=client)


def test_authorize_url_has_required_params():
    o = WhoopOAuth("cid", "sec", "http://localhost:8080/callback")
    url = o.authorize_url(state="xyz")
    assert "response_type=code" in url
    assert "client_id=cid" in url
    assert "state=xyz" in url
    assert "offline" in url  # needed for refresh token


def test_token_expiry_with_skew():
    t = TokenSet("a", "r", "bearer", "", NOW + timedelta(seconds=100))
    assert t.is_expired(NOW, skew_s=60) is False
    assert t.is_expired(NOW, skew_s=120) is True  # within skew => treat as expired
    assert t.is_expired(NOW + timedelta(seconds=200)) is True


def test_exchange_code_builds_tokenset():
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode()
        assert "grant_type=authorization_code" in body
        assert "code=THECODE" in body
        return httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
                "token_type": "bearer",
                "scope": "offline read:recovery",
            },
        )

    tokens = _oauth(handler).exchange_code("THECODE", now=NOW)
    assert tokens.access_token == "AT"
    assert tokens.refresh_token == "RT"
    assert tokens.expires_at == NOW + timedelta(seconds=3600)


def test_refresh_carries_forward_missing_refresh_token():
    def handler(req: httpx.Request) -> httpx.Response:
        assert "grant_type=refresh_token" in req.content.decode()
        return httpx.Response(
            200,
            json={"access_token": "AT2", "expires_in": 3600, "token_type": "bearer"},
        )

    old = TokenSet("AT1", "RT1", "bearer", "offline", NOW - timedelta(seconds=10))
    new = _oauth(handler).refresh(old, now=NOW)
    assert new.access_token == "AT2"
    assert new.refresh_token == "RT1"  # preserved because response omitted it


def test_refresh_without_refresh_token_raises():
    o = WhoopOAuth("cid", "sec", "http://localhost:8080/callback")
    with pytest.raises(ReauthRequired):
        o.refresh(TokenSet("AT", "", "bearer", "", NOW))


def test_token_endpoint_error_raises_reauth_without_leaking_secret():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid_grant")

    with pytest.raises(ReauthRequired) as exc:
        _oauth(handler).exchange_code("bad", now=NOW)
    assert "secret" not in str(exc.value)


def test_token_store_roundtrip_and_perms(tmp_path):
    store = TokenStore(tmp_path / ".credentials" / "whoop.json")
    assert store.load() is None
    t = TokenSet("AT", "RT", "bearer", "offline", NOW + timedelta(hours=1))
    store.save(t)
    loaded = store.load()
    assert loaded == t
    # 0600 perms
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_valid_access_token_refreshes_when_expired(tmp_path):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200, json={"access_token": "FRESH", "expires_in": 3600, "token_type": "bearer"}
        )

    store = TokenStore(tmp_path / "whoop.json")
    store.save(TokenSet("STALE", "RT", "bearer", "offline", NOW - timedelta(seconds=1)))
    tok = _oauth(handler).valid_access_token(store, now=NOW)
    assert tok == "FRESH"
    assert calls["n"] == 1
    assert store.load().access_token == "FRESH"  # persisted


def test_valid_access_token_no_store_raises(tmp_path):
    o = WhoopOAuth("cid", "sec", "http://localhost:8080/callback")
    with pytest.raises(ReauthRequired):
        o.valid_access_token(TokenStore(tmp_path / "missing.json"), now=NOW)
