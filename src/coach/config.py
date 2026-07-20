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
    anthropic_api_key: str = field(default="", repr=False)
    coach_model: str = "claude-opus-4-8"

    def require_anthropic(self) -> None:
        """Raise a clear error if the Anthropic API key is absent."""
        if not self.anthropic_api_key:
            raise ConfigError(
                "ANTHROPIC_API_KEY is missing. The coach LLM layer bills per token "
                "and is separate from any Claude subscription (§8.7). Add the key "
                "to .env (see .env.example) — create one at "
                "https://console.anthropic.com/settings/keys."
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
        anthropic_api_key=_get(env, "ANTHROPIC_API_KEY", required=False),
        coach_model=_get(env, "COACH_MODEL", required=False, default="claude-opus-4-8"),
    )
