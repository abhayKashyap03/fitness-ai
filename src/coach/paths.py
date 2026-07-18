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


def whoop_token_path() -> Path:
    return credentials_dir() / "whoop_token.json"
