"""MFP auth + client: cookie exchange, token caching, retry, header hygiene.

All mocked — no network (§6.2). Asserts the session cookie and bearer token
never leak into error messages (§8.4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from coach.adapters.myfitnesspal.auth import MfpAuth, MfpAuthError, MfpToken, MfpTokenStore
from coach.adapters.myfitnesspal.client import MfpAPIError, MfpClient

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
SECRET_COOKIE = "SESSIONID=super-secret-value; other=x"


# ---- auth ------------------------------------------------------------------


def _auth(handler, cookie: str = SECRET_COOKIE) -> MfpAuth:
    return MfpAuth(cookie, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_cookie_exchanged_for_bearer_token():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["cookie"] = req.headers.get("Cookie")
        seen["path"] = req.url.path
        seen["refresh"] = req.url.params.get("refresh")
        return httpx.Response(200, json={"access_token": "AT", "user_id": 42, "expires_in": 3600})

    token = _auth(handler).fetch_token(now=NOW)
    assert token.access_token == "AT"
    assert token.user_id == "42"
    assert seen["cookie"] == SECRET_COOKIE
    assert seen["path"] == "/user/auth_token"
    assert seen["refresh"] == "true"


def test_expired_cookie_401_raises_without_leaking_cookie():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    with pytest.raises(MfpAuthError) as exc:
        _auth(handler).fetch_token(now=NOW)
    assert SECRET_COOKIE not in str(exc.value)
    assert "SESSIONID" not in str(exc.value)


def test_missing_cookie_raises_before_any_request():
    with pytest.raises(MfpAuthError, match="MFP_SESSION_COOKIE"):
        MfpAuth("").fetch_token(now=NOW)


def test_valid_token_caches_and_refreshes(tmp_path: Path):
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"access_token": f"AT{calls['n']}", "user_id": 1, "expires_in": 3600})

    store = MfpTokenStore(tmp_path / "tok.json")
    auth = _auth(handler)
    # first call fetches + persists
    t1 = auth.valid_token(store, now=NOW)
    assert t1.access_token == "AT1"
    # second call (token still valid) uses cache, no new request
    t2 = auth.valid_token(store, now=NOW + timedelta(minutes=1))
    assert t2.access_token == "AT1"
    assert calls["n"] == 1
    # once expired, refetches
    t3 = auth.valid_token(store, now=NOW + timedelta(hours=2))
    assert t3.access_token == "AT2"
    assert calls["n"] == 2


def test_token_expiry_math_has_skew():
    tok = MfpToken(access_token="AT", user_id="1", expires_at=NOW + timedelta(seconds=30))
    assert tok.is_expired(now=NOW)  # within 60s skew -> treated expired


# ---- client ----------------------------------------------------------------


def _client(handler, **kw) -> MfpClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return MfpClient(lambda: ("AT", "42"), http_client=http, sleep=lambda _s: None, **kw)


def test_diary_request_carries_auth_and_user_headers():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization")
        seen["uid"] = req.headers.get("mfp-user-id")
        seen["cid"] = req.headers.get("mfp-client-id")
        seen["date"] = req.url.params.get("date")
        return httpx.Response(200, json={"items": []})

    _client(handler).get_diary("2026-06-15")
    assert seen["auth"] == "Bearer AT"
    assert seen["uid"] == "42"
    assert seen["cid"] == "mfp-main-js"
    assert seen["date"] == "2026-06-15"


def test_client_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        return httpx.Response(200, json={"items": []})

    assert _client(handler).get_diary("2026-06-15") == {"items": []}
    assert calls["n"] == 2


def test_client_gives_up_on_5xx():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with pytest.raises(MfpAPIError, match="after"):
        _client(handler, max_retries=2).get_diary("2026-06-15")


def test_client_error_does_not_leak_token():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = MfpClient(lambda: ("SECRET-BEARER-abc123", "42"), http_client=http)
    with pytest.raises(MfpAPIError) as exc:
        client.get_diary("2026-06-15")
    assert "SECRET-BEARER" not in str(exc.value)  # token value absent from error
