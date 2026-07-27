"""Source-client factories, shared by every consumer (CLI, web, future backend).

These lived in ``cli/main.py``, which meant a second consumer had to either
import from the CLI or reimplement the auth wiring. They are domain plumbing,
not presentation, so they belong in the service layer — same reasoning that moved
sync orchestration into :mod:`coach.services.sync`.

Each factory returns a client that resolves credentials lazily, so constructing
one never performs I/O and never raises for missing credentials; the failure
surfaces at first use, where the caller can report it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..config import Settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..adapters.myfitnesspal.client import MfpClient
    from ..adapters.whoop.client import WhoopClient


def whoop_client(settings: Settings) -> WhoopClient:
    """WHOOP API client backed by the stored OAuth token (auto-refreshing)."""
    from ..adapters.whoop.auth import TokenStore, WhoopOAuth
    from ..adapters.whoop.client import WhoopClient
    from ..paths import whoop_token_path

    oauth = WhoopOAuth(
        settings.whoop_client_id,
        settings.whoop_client_secret,
        settings.whoop_redirect_uri,
    )
    store = TokenStore(whoop_token_path(settings.user_id))
    return WhoopClient(lambda: oauth.valid_access_token(store))


def mfp_client(settings: Settings) -> MfpClient:
    """MyFitnessPal v2 client backed by the cached session-cookie token."""
    from ..adapters.myfitnesspal.auth import MfpAuth, MfpTokenStore
    from ..adapters.myfitnesspal.client import MfpClient
    from ..paths import mfp_token_path

    auth = MfpAuth(settings.mfp_session_cookie)
    store = MfpTokenStore(mfp_token_path(settings.user_id))

    def creds() -> tuple[str, str]:
        tok = auth.valid_token(store)
        return tok.access_token, tok.user_id

    return MfpClient(creds)
