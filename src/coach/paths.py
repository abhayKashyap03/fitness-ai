"""Filesystem locations, resolved relative to the repo root.

Single source of truth for where things live so nothing hardcodes paths.
The repo root is found by walking up from this file until we see the
``schema/migrations`` directory (works for both editable installs and
running straight from a clone).
"""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    """Return the project root (the dir containing ``schema/migrations``)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "schema" / "migrations").is_dir():
            return parent
    raise RuntimeError(
        "Could not locate repo root (no schema/migrations found above "
        f"{here}). Are the SQL migrations present?"
    )


def migrations_dir() -> Path:
    return repo_root() / "schema" / "migrations"


def credentials_dir() -> Path:
    """Gitignored directory for local secrets (tokens). Never committed."""
    return repo_root() / ".credentials"


def user_credentials_dir(user_id: int = 1) -> Path:
    """Per-user secrets directory — ``.credentials/u<user_id>/``.

    Token files are namespaced by ``user_id`` for the same reason every
    canonical row carries one (§2.4): it is multi-tenancy insurance that costs
    nothing today. Unnamespaced files silently COLLIDE — a second user
    authorizing WHOOP would overwrite the first user's refresh token.
    """
    return credentials_dir() / f"u{user_id}"


def _token_path(name: str, user_id: int) -> Path:
    """Namespaced token path, transparently adopting a legacy global file.

    Pre-namespacing installs wrote ``.credentials/<name>``. If that file exists
    and the namespaced one does not, it is MOVED into the user's directory so a
    live session keeps working without a re-auth. Never deletes a token.
    """
    new = user_credentials_dir(user_id) / name
    legacy = credentials_dir() / name
    if not new.exists() and legacy.is_file():
        new.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(new)
    return new


def whoop_token_path(user_id: int = 1) -> Path:
    return _token_path("whoop_token.json", user_id)


def mfp_token_path(user_id: int = 1) -> Path:
    return _token_path("mfp_token.json", user_id)
