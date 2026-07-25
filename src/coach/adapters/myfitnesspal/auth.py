"""MyFitnessPal auth: browser session cookie -> short-lived bearer token.

Durable credential is the user's browser **session cookie** (pasted into .env as
MFP_SESSION_COOKIE; lasts ~weeks). We exchange it at ``/user/auth_token`` for a
short-lived bearer token + user_id, cache that token to a gitignored 0600 file,
and refresh it from the cookie when it expires. When the COOKIE itself expires,
the exchange 401s and we raise :class:`MfpAuthError` telling the user to re-paste.

No ``browser_cookie3``, no scraping, no vendor SDK — one cookie in, stdlib +
httpx out. The testable core injects ``now`` and an httpx client; nothing here
touches a global clock or the network in tests. The cookie/token are never
logged (§8.4).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from . import API_BASE, AUTH_TOKEN_PATH, CLIENT_ID


class MfpAuthError(RuntimeError):
    """The session cookie is missing/expired — user must re-paste MFP_SESSION_COOKIE."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class MfpToken:
    access_token: str
    user_id: str
    expires_at: datetime  # UTC

    def is_expired(self, now: datetime | None = None, skew_s: int = 60) -> bool:
        now = now or _utcnow()
        return now >= (self.expires_at - timedelta(seconds=skew_s))

    @classmethod
    def from_response(cls, data: dict, now: datetime | None = None) -> MfpToken:
        now = now or _utcnow()
        expires_in = int(data.get("expires_in", 3600))
        return cls(
            access_token=data["access_token"],
            user_id=str(data.get("user_id", "")),
            expires_at=now + timedelta(seconds=expires_in),
        )

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "user_id": self.user_id,
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> MfpToken:
        return cls(
            access_token=d["access_token"],
            user_id=d.get("user_id", ""),
            expires_at=datetime.fromisoformat(d["expires_at"]),
        )


class MfpTokenStore:
    """Persist an :class:`MfpToken` to a gitignored JSON file (0600)."""

    def __init__(self, path: Path):
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> MfpToken | None:
        if not self.path.exists():
            return None
        return MfpToken.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, token: MfpToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(token.to_dict(), indent=2), encoding="utf-8")
        os.chmod(self.path, 0o600)


class MfpAuth:
    """Exchange the session cookie for a bearer token; cache + refresh it."""

    def __init__(self, session_cookie: str, *, client: httpx.Client | None = None):
        self._cookie = session_cookie
        self._client = client  # injectable for tests; None => build per-call

    def fetch_token(self, now: datetime | None = None) -> MfpToken:
        """Trade the session cookie for a fresh bearer token.

        Raises :class:`MfpAuthError` on any non-2xx (an expired cookie 401s) —
        the request never echoes the cookie value into the error.
        """
        if not self._cookie:
            raise MfpAuthError(
                "No MFP session cookie — set MFP_SESSION_COOKIE in .env "
                "(copy the Cookie header from a logged-in myfitnesspal.com tab)."
            )
        url = f"{API_BASE}{AUTH_TOKEN_PATH}"
        params = {"refresh": "true"}
        headers = {
            "Cookie": self._cookie,
            "mfp-client-id": CLIENT_ID,
            "Accept": "application/json",
        }
        if self._client is not None:
            resp = self._client.get(url, params=params, headers=headers)
        else:
            with httpx.Client(timeout=30) as c:
                resp = c.get(url, params=params, headers=headers)
        if resp.status_code >= 400:
            # Never echo headers (the cookie lives there).
            raise MfpAuthError(
                f"MFP auth_token returned {resp.status_code}: {resp.text[:200]}. "
                "The session cookie is likely expired — re-copy MFP_SESSION_COOKIE."
            )
        return MfpToken.from_response(resp.json(), now)

    def valid_token(self, store: MfpTokenStore, now: datetime | None = None) -> MfpToken:
        """Return a non-expired token, exchanging/refreshing from the cookie as needed."""
        token = store.load()
        if token is None or token.is_expired(now):
            token = self.fetch_token(now)
            store.save(token)
        return token
