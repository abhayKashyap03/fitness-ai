"""WHOOP OAuth 2.0 authorization-code flow + token persistence/refresh.

Split so the testable core (URL building, code exchange, refresh, expiry math)
has NO interactive or global-clock dependencies: ``now`` is injected and the
HTTP client is injectable. The interactive browser dance lives in the CLI.

Tokens are stored in a gitignored JSON file with 0600 permissions. They are
never logged.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import AUTH_URL, DEFAULT_SCOPES, TOKEN_URL


class ReauthRequired(RuntimeError):
    """No usable token and none can be refreshed — user must re-run auth."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    token_type: str
    scope: str
    expires_at: datetime  # UTC

    def is_expired(self, now: datetime | None = None, skew_s: int = 60) -> bool:
        now = now or _utcnow()
        return now >= (self.expires_at - timedelta(seconds=skew_s))

    @classmethod
    def from_token_response(cls, data: dict, now: datetime | None = None) -> TokenSet:
        now = now or _utcnow()
        expires_in = int(data.get("expires_in", 3600))
        return cls(
            access_token=data["access_token"],
            # WHOOP omits refresh_token on refresh only if 'offline' wasn't granted;
            # keep the old one when absent (caller passes prev via merge()).
            refresh_token=data.get("refresh_token", ""),
            token_type=data.get("token_type", "bearer"),
            scope=data.get("scope", ""),
            expires_at=now + timedelta(seconds=expires_in),
        )

    def merge_refresh(self, refreshed: TokenSet) -> TokenSet:
        """Carry the old refresh_token forward if the refresh response omitted one."""
        if refreshed.refresh_token:
            return refreshed
        return TokenSet(
            access_token=refreshed.access_token,
            refresh_token=self.refresh_token,
            token_type=refreshed.token_type,
            scope=refreshed.scope or self.scope,
            expires_at=refreshed.expires_at,
        )

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "scope": self.scope,
            "expires_at": self.expires_at.astimezone(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> TokenSet:
        return cls(
            access_token=d["access_token"],
            refresh_token=d["refresh_token"],
            token_type=d.get("token_type", "bearer"),
            scope=d.get("scope", ""),
            expires_at=datetime.fromisoformat(d["expires_at"]),
        )


class TokenStore:
    """Persist a :class:`TokenSet` to a gitignored JSON file (0600)."""

    def __init__(self, path: Path):
        self.path = path

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> TokenSet | None:
        if not self.path.exists():
            return None
        return TokenSet.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(tokens.to_dict(), indent=2), encoding="utf-8")
        os.chmod(self.path, 0o600)


class WhoopOAuth:
    """Stateless OAuth operations against WHOOP's token endpoint."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        client: httpx.Client | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._client = client  # injectable for tests; None => build per-call

    # ---- authorize ---------------------------------------------------------

    def authorize_url(self, state: str, scopes: tuple[str, ...] = DEFAULT_SCOPES) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    # ---- token exchange ----------------------------------------------------

    def _post_token(self, data: dict) -> dict:
        if self._client is not None:
            resp = self._client.post(TOKEN_URL, data=data)
        else:
            with httpx.Client(timeout=30) as c:
                resp = c.post(TOKEN_URL, data=data)
        if resp.status_code >= 400:
            # Never echo the request body (contains the secret).
            raise ReauthRequired(
                f"WHOOP token endpoint returned {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()

    def exchange_code(self, code: str, now: datetime | None = None) -> TokenSet:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
        }
        return TokenSet.from_token_response(self._post_token(data), now)

    def refresh(self, tokens: TokenSet, now: datetime | None = None) -> TokenSet:
        if not tokens.refresh_token:
            raise ReauthRequired("No refresh token available — run `coach auth whoop`.")
        data = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "offline",
        }
        refreshed = TokenSet.from_token_response(self._post_token(data), now)
        return tokens.merge_refresh(refreshed)

    # ---- the one call the API client uses ----------------------------------

    def valid_access_token(self, store: TokenStore, now: datetime | None = None) -> str:
        """Return a non-expired access token, refreshing + persisting if needed."""
        tokens = store.load()
        if tokens is None:
            raise ReauthRequired("No stored WHOOP token — run `coach auth whoop` first.")
        if tokens.is_expired(now):
            tokens = self.refresh(tokens, now)
            store.save(tokens)
        return tokens.access_token
