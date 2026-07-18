"""Typed WHOOP v2 API client.

Responsibilities:
  * attach a valid bearer token (via an injected provider — no auth logic here);
  * follow WHOOP's ``next_token`` pagination;
  * back off on 429 (honoring ``Retry-After``) and retry transient 5xx;
  * log requests WITHOUT leaking the token.

Returns raw record dicts verbatim — the ingestion layer writes them untouched
to ``raw_events`` (§2.1). No normalization happens here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator

import httpx

from . import API_BASE

log = logging.getLogger("coach.whoop.client")

# WHOOP paginates in pages of <=25 and returns {"records": [...], "next_token": ...}.
_PAGE_LIMIT = 25


class WhoopAPIError(RuntimeError):
    """Non-retryable API failure (4xx other than 429)."""


class WhoopClient:
    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        http_client: httpx.Client | None = None,
        base_url: str = API_BASE,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._token_provider = token_provider
        self._client = http_client
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._sleep = sleep

    # ---- transport ---------------------------------------------------------

    def _do_get(self, url: str, params: dict) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._token_provider()}"}
        if self._client is not None:
            return self._client.get(url, params=params, headers=headers)
        with httpx.Client(timeout=30) as c:
            return c.get(url, params=params, headers=headers)

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self._base_url}{path}"
        attempt = 0
        while True:
            resp = self._do_get(url, params)
            status = resp.status_code
            # log path + params only — NEVER headers (token lives there)
            log.debug("GET %s params=%s -> %s", path, params, status)

            if status == 200:
                return resp.json()

            if status == 429 or 500 <= status < 600:
                if attempt >= self._max_retries:
                    raise WhoopAPIError(f"GET {path} failed after {attempt} retries: {status}")
                delay = self._retry_delay(resp, attempt)
                log.warning("GET %s -> %s, backing off %.1fs", path, status, delay)
                self._sleep(delay)
                attempt += 1
                continue

            raise WhoopAPIError(f"GET {path} -> {status}: {resp.text[:200]}")

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return min(2.0**attempt, 30.0)  # exponential, capped

    # ---- pagination --------------------------------------------------------

    def _paginate(self, path: str, params: dict) -> Iterator[dict]:
        params = dict(params, limit=_PAGE_LIMIT)
        while True:
            body = self._get(path, params)
            yield from body.get("records", [])
            token = body.get("next_token")
            if not token:
                return
            params = dict(params, nextToken=token)

    @staticmethod
    def _window(start: str | None, end: str | None) -> dict:
        p: dict[str, str] = {}
        if start:
            p["start"] = start
        if end:
            p["end"] = end
        return p

    # ---- endpoints ---------------------------------------------------------

    def get_recovery(self, start: str | None = None, end: str | None = None) -> list[dict]:
        return list(self._paginate("/v2/recovery", self._window(start, end)))

    def get_cycles(self, start: str | None = None, end: str | None = None) -> list[dict]:
        return list(self._paginate("/v2/cycle", self._window(start, end)))

    def get_sleep(self, start: str | None = None, end: str | None = None) -> list[dict]:
        return list(self._paginate("/v2/activity/sleep", self._window(start, end)))

    def get_workouts(self, start: str | None = None, end: str | None = None) -> list[dict]:
        return list(self._paginate("/v2/activity/workout", self._window(start, end)))

    def get_body_measurement(self) -> dict:
        """Body measurements are a single object, not a paginated collection."""
        return self._get("/v2/user/measurement/body", {})
