"""Typed settings loaded from the environment / ``.env``.

Rules (CLAUDE.md §6):
  * Secrets live in ``.env`` only, never hardcoded, never logged.
  * Missing a REQUIRED var fails loudly with a message naming the variable.
  * ``__repr__`` never prints secret values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .paths import repo_root


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Resolved, typed application settings."""

    db_path: Path
    user_id: int
    home_tz: str
    units: str
    log_level: str

    # optional / feature-gated secrets (may be empty until the feature is used)
    whoop_client_id: str = field(default="", repr=False)
    whoop_client_secret: str = field(default="", repr=False)
    whoop_redirect_uri: str = "http://localhost:8080/callback"
    mfp_session_cookie: str = field(default="", repr=False)
    anthropic_api_key: str = field(default="", repr=False)
    google_api_key: str = field(default="", repr=False)
    xai_api_key: str = field(default="", repr=False)
    llm_provider: str = "google"  # 'google' (free tier) | 'anthropic' | 'grok'
    coach_model: str = ""  # empty -> provider default (see coach.coach.llm)

    # USD per 1M tokens for the ACTIVE provider/model (§8.7). Deliberately not
    # defaulted to a built-in price table: shipping guessed per-token prices
    # would fabricate exactly the kind of number this project refuses to
    # fabricate, and a wrong rate flows straight into a "you spent $X" claim.
    # Unset -> calls are recorded with real token counts and reported UNPRICED,
    # never as free (§2.7).
    price_input_per_mtok: float | None = None
    price_output_per_mtok: float | None = None
    price_cached_input_per_mtok: float | None = None
    price_cache_write_per_mtok: float | None = None

    @property
    def llm_prices(self) -> dict[str, float | None]:
        """Rates to stamp on a recorded call (see store.llm_calls.record_call)."""
        return {
            "input": self.price_input_per_mtok,
            "output": self.price_output_per_mtok,
            "cached_input": self.price_cached_input_per_mtok,
            "cache_write": self.price_cache_write_per_mtok,
        }

    @property
    def llm_api_key(self) -> str:
        """The API key for the configured provider (never logged, §8.4)."""
        return {
            "anthropic": self.anthropic_api_key,
            "grok": self.xai_api_key,
        }.get(self.llm_provider, self.google_api_key)

    def require_llm(self) -> None:
        """Raise a clear error if the configured provider's API key is absent."""
        if self.llm_provider == "anthropic" and not self.anthropic_api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is missing (COACH_LLM_PROVIDER=anthropic). "
                "Billed per token, separate from any Claude subscription (§8.7). "
                "Create one at https://console.anthropic.com/settings/keys, or "
                "switch COACH_LLM_PROVIDER=google for the free tier."
            )
        if self.llm_provider == "google" and not self.google_api_key:
            raise ConfigError(
                "GOOGLE_API_KEY is missing (COACH_LLM_PROVIDER=google). "
                "Create a free-tier key at https://aistudio.google.com/apikey "
                "and add it to .env (see .env.example)."
            )
        if self.llm_provider == "grok" and not self.xai_api_key:
            raise ConfigError(
                "XAI_API_KEY is missing (COACH_LLM_PROVIDER=grok). "
                "Billed per token (§8.7). Create one at https://console.x.ai "
                "and add it to .env (see .env.example)."
            )

    def require_mfp(self) -> None:
        """Raise a clear error if the MFP session cookie is absent."""
        if not self.mfp_session_cookie:
            raise ConfigError(
                "MFP_SESSION_COOKIE is missing. Log in at myfitnesspal.com, copy "
                "the Cookie request header from a logged-in tab, and set it in .env "
                "(see .env.example). It lasts ~weeks; re-copy it when ingest 401s."
            )

    def require_whoop(self) -> None:
        """Raise a clear error if WHOOP OAuth credentials are absent."""
        missing = [
            name
            for name, val in (
                ("WHOOP_CLIENT_ID", self.whoop_client_id),
                ("WHOOP_CLIENT_SECRET", self.whoop_client_secret),
            )
            if not val
        ]
        if missing:
            raise ConfigError(
                "WHOOP credentials missing: "
                + ", ".join(missing)
                + ". Set them in .env (see .env.example) — get them at "
                "https://developer.whoop.com."
            )


def _get(env: dict[str, str], name: str, *, required: bool, default: str | None = None) -> str:
    val = env.get(name, "").strip()
    if val:
        return val
    if default is not None:
        return default
    if required:
        raise ConfigError(
            f"Required configuration variable {name!r} is missing or empty. "
            f"Add it to your .env (see .env.example)."
        )
    return ""


def _get_price(env: dict[str, str], name: str) -> float | None:
    """An optional USD-per-1M-token rate. Absent stays absent (§2.7).

    A malformed rate is an error, not a silent fallback to None: reading
    ``"12.oo"`` as unpriced would hide a typo behind a plausible-looking report.
    """
    raw = env.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number (USD per 1M tokens), got {raw!r}.") from exc
    if value < 0:
        raise ConfigError(f"{name} must not be negative, got {value}.")
    return value


def load_settings(env: dict[str, str] | None = None, *, load_dotenv_file: bool = True) -> Settings:
    """Build :class:`Settings` from the environment.

    Args:
        env: explicit mapping to read from (used by tests). Defaults to
            ``os.environ`` after loading ``.env``.
        load_dotenv_file: when True (and ``env`` is None), populate os.environ
            from the repo ``.env`` first.
    """
    if env is None:
        if load_dotenv_file:
            dotenv_path = repo_root() / ".env"
            if dotenv_path.exists():
                load_dotenv(dotenv_path)
        env = dict(os.environ)

    db_raw = _get(env, "COACH_DB_PATH", required=False, default="./data/coach.db")
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = repo_root() / db_path

    user_id_raw = _get(env, "COACH_USER_ID", required=False, default="1")
    try:
        user_id = int(user_id_raw)
    except ValueError as exc:
        raise ConfigError(f"COACH_USER_ID must be an integer, got {user_id_raw!r}.") from exc

    units = _get(env, "COACH_UNITS", required=False, default="metric")
    if units not in {"metric", "imperial"}:
        raise ConfigError(f"COACH_UNITS must be 'metric' or 'imperial', got {units!r}.")

    provider = _get(env, "COACH_LLM_PROVIDER", required=False, default="google").lower()
    if provider not in {"google", "anthropic", "grok"}:
        raise ConfigError(
            f"COACH_LLM_PROVIDER must be 'google', 'anthropic', or 'grok', got {provider!r}."
        )

    return Settings(
        db_path=db_path,
        user_id=user_id,
        home_tz=_get(env, "COACH_HOME_TZ", required=False, default="America/New_York"),
        units=units,
        log_level=_get(env, "COACH_LOG_LEVEL", required=False, default="INFO"),
        whoop_client_id=_get(env, "WHOOP_CLIENT_ID", required=False),
        whoop_client_secret=_get(env, "WHOOP_CLIENT_SECRET", required=False),
        whoop_redirect_uri=_get(
            env,
            "WHOOP_REDIRECT_URI",
            required=False,
            default="http://localhost:8080/callback",
        ),
        mfp_session_cookie=_get(env, "MFP_SESSION_COOKIE", required=False),
        anthropic_api_key=_get(env, "ANTHROPIC_API_KEY", required=False),
        google_api_key=_get(env, "GOOGLE_API_KEY", required=False),
        xai_api_key=_get(env, "XAI_API_KEY", required=False),
        llm_provider=provider,
        # empty -> the provider's own default model (coach.coach.llm)
        coach_model=_get(env, "COACH_MODEL", required=False),
        price_input_per_mtok=_get_price(env, "COACH_PRICE_INPUT_PER_MTOK"),
        price_output_per_mtok=_get_price(env, "COACH_PRICE_OUTPUT_PER_MTOK"),
        price_cached_input_per_mtok=_get_price(env, "COACH_PRICE_CACHED_INPUT_PER_MTOK"),
        price_cache_write_per_mtok=_get_price(env, "COACH_PRICE_CACHE_WRITE_PER_MTOK"),
    )
