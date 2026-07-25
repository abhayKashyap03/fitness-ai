"""Typed MyFitnessPal v2 client.

Mirrors :class:`coach.adapters.whoop.client.WhoopClient`: an injected credentials
provider (no auth logic here), bounded retry with ``Retry-After`` on 429/5xx, and
logging that never leaks the token or cookie. Returns raw diary dicts verbatim —
ingestion writes them untouched to ``raw_events`` (§2.1); no normalization here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from . import API_BASE, CLIENT_ID, DIARY_PATH, MEASUREMENTS_PATH

log = logging.getLogger("coach.mfp.client")

# provider returns (access_token, user_id) — both go into request headers.
CredentialsProvider = Callable[[], tuple[str, str]]


class MfpAPIError(RuntimeError):
    """Non-retryable API failure (4xx other than 429)."""


class MfpClient:
    def __init__(
        self,
        credentials_provider: CredentialsProvider,
        *,
        http_client: httpx.Client | None = None,
        base_url: str = API_BASE,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._creds = credentials_provider
        self._client = http_client
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._sleep = sleep

    # ---- transport ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        token, user_id = self._creds()
        headers = {
            "Authorization": f"Bearer {token}",
            "mfp-client-id": CLIENT_ID,
            "Accept": "application/json",
        }
        if user_id:
            headers["mfp-user-id"] = user_id
        return headers

    def _do_get(self, url: str, params: dict) -> httpx.Response:
        headers = self._headers()
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
            # log path + params only — NEVER headers (token/cookie live there)
            log.debug("GET %s params=%s -> %s", path, params, status)

            if status == 200:
                return resp.json()

            if status == 429 or 500 <= status < 600:
                if attempt >= self._max_retries:
                    raise MfpAPIError(f"GET {path} failed after {attempt} retries: {status}")
                delay = self._retry_delay(resp, attempt)
                log.warning("GET %s -> %s, backing off %.1fs", path, status, delay)
                self._sleep(delay)
                attempt += 1
                continue

            raise MfpAPIError(f"GET {path} -> {status}: {resp.text[:200]}")

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return min(2.0**attempt, 30.0)  # exponential, capped

    # ---- endpoints ---------------------------------------------------------

    def get_diary(self, day: str) -> dict:
        """Return one day's raw diary payload (``YYYY-MM-DD``).

        The exact path/param is our reconstruction of the web client's read and
        RECONCILES ON FIRST LIVE CONTACT (§10.2, see ``__init__``). Returns the
        JSON verbatim; the normalizer owns field extraction.
        """
        return self._get(DIARY_PATH, {"entry_date": day})

    def get_weight(self, day: str) -> dict:
        """Return one day's raw weight measurement (``YYYY-MM-DD``).

        MFP keys this off ``entry_date`` and this adapter only requests
        ``type=weight``. Returns the JSON verbatim; the normalizer owns field
        extraction (value/unit/date/updated_at).
        """
        return self._get(MEASUREMENTS_PATH, {"type": "weight", "entry_date": day})
