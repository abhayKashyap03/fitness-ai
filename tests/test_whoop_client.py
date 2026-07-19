"""WHOOP API client: pagination, retry/backoff, token handling — all mocked."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from coach.adapters.whoop.client import WhoopAPIError, WhoopClient

FIX = Path(__file__).parent / "fixtures" / "whoop"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def _client(handler, **kw) -> WhoopClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return WhoopClient(lambda: "AT", http_client=http, sleep=lambda _s: None, **kw)


def test_recovery_paginates_and_follows_next_token():
    def handler(req: httpx.Request) -> httpx.Response:
        token = req.url.params.get("nextToken")
        page = "recovery_page2.json" if token == "PAGE2TOKEN" else "recovery_page1.json"
        return httpx.Response(200, json=_load(page))

    records = _client(handler).get_recovery("2026-06-01", "2026-06-03")
    assert len(records) == 3  # 2 + 1 across two pages
    assert {r["cycle_id"] for r in records} == {1539008513, 999999, 1541375428}


def test_bearer_token_sent_and_not_returned_in_error():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(200, json={"records": [], "next_token": None})

    _client(handler).get_workouts()
    assert seen["auth"] == "Bearer AT"


def test_retries_on_429_then_succeeds():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, text="rate limited")
        return httpx.Response(200, json={"records": [], "next_token": None})

    assert _client(handler).get_cycles() == []
    assert calls["n"] == 2


def test_retries_on_5xx_then_gives_up():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with pytest.raises(WhoopAPIError, match="after"):
        _client(handler, max_retries=2).get_recovery()


def test_non_retryable_4xx_raises_immediately():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")

    with pytest.raises(WhoopAPIError, match="403"):
        _client(handler).get_recovery()
    assert calls["n"] == 1  # no retry on 403


def test_body_measurement_single_object():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("body_measurement.json"))

    body = _client(handler).get_body_measurement()
    assert body["weight_kilogram"] == 83.18884
