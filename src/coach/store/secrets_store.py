"""Per-user source credentials, encrypted at rest (ADR-0018, migration 0014).

On a laptop a WHOOP refresh token sits in a file only its owner can read. On a
host it sits in a database that can be dumped, backed up, or restored somewhere
else — and it is a credential to somebody else's health account. This module is
the difference between those two situations.

**The key never touches the database.** It comes from the host environment
(`COACH_SECRET_KEY`), so a stolen dump, a leaked backup or a restored snapshot
yields ciphertext and nothing else.

AES-256-GCM via ``cryptography`` — the one new runtime dependency this feature
adds, signed off in ADR-0018 because the standard library ships no AEAD and
hand-rolling one is precisely the wrong instinct in a file like this.

``user_secret`` is the single table in this schema deliberately WITHOUT history
(§2.1 keeps everything else append-only). A superseded refresh token has no
analytical value, and retaining every historical ciphertext only widens the
blast radius of a future key compromise. Secrets are updated in place.
"""

from __future__ import annotations

import base64
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

KEY_ENV = "COACH_SECRET_KEY"
_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # GCM standard


class SecretsUnavailable(RuntimeError):
    """Raised when no usable encryption key is configured.

    Deliberately fatal rather than falling back to plaintext: a system that
    silently stores credentials unencrypted when misconfigured is worse than one
    that refuses to store them at all.
    """


@dataclass(frozen=True)
class SecretRef:
    """Metadata about a stored secret. Never carries the plaintext."""

    user_id: int
    name: str
    key_id: str
    created_at: str
    updated_at: str


def generate_key() -> str:
    """A fresh base64 AES-256 key, for the operator to put in the host env."""
    import secrets as _secrets

    return base64.urlsafe_b64encode(_secrets.token_bytes(_KEY_BYTES)).decode()


def _load_key(key_b64: str | None = None) -> bytes:
    raw = key_b64 if key_b64 is not None else os.environ.get(KEY_ENV, "")
    raw = raw.strip()
    if not raw:
        raise SecretsUnavailable(
            f"{KEY_ENV} is not set. Generate one with `coach user genkey` and put it in "
            "the host environment — never in the database or the repo."
        )
    try:
        key = base64.urlsafe_b64decode(raw)
    except Exception as exc:  # any decode failure is the same problem
        raise SecretsUnavailable(f"{KEY_ENV} is not valid base64") from exc
    if len(key) != _KEY_BYTES:
        raise SecretsUnavailable(
            f"{KEY_ENV} must decode to {_KEY_BYTES} bytes (AES-256), got {len(key)}"
        )
    return key


def key_id(key: bytes) -> str:
    """Short, non-reversible identifier for a key, so rotation is auditable.

    A truncated SHA-256 of the key: enough to tell two keys apart in a row,
    far too little to help recover one.
    """
    import hashlib

    return hashlib.sha256(key).hexdigest()[:12]


def _aead(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency is declared
        raise SecretsUnavailable(
            "the `cryptography` package is required to store per-user credentials "
            "(ADR-0018). Install it with `pip install -e .`"
        ) from exc
    return AESGCM(key)


def put_secret(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    name: str,
    value: str,
    key_b64: str | None = None,
) -> SecretRef:
    """Encrypt and store one credential, replacing any previous value.

    ``user_id`` and ``name`` are bound in as authenticated additional data, so a
    ciphertext lifted from one user's row cannot be replayed into another's —
    decryption fails rather than silently returning someone else's credential.
    """
    if not name.strip():
        raise ValueError("a secret needs a name")
    key = _load_key(key_b64)
    import secrets as _secrets

    nonce = _secrets.token_bytes(_NONCE_BYTES)
    aad = f"{user_id}:{name}".encode()
    ct = _aead(key).encrypt(nonce, value.encode(), aad)
    now = datetime.now(UTC).isoformat()
    kid = key_id(key)
    conn.execute(
        "INSERT INTO user_secret (user_id, name, ciphertext, nonce, key_id, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id, name) DO UPDATE SET ciphertext=excluded.ciphertext, "
        "nonce=excluded.nonce, key_id=excluded.key_id, updated_at=excluded.updated_at",
        (user_id, name, ct, nonce, kid, now, now),
    )
    row = conn.execute(
        "SELECT created_at, updated_at FROM user_secret WHERE user_id=? AND name=?",
        (user_id, name),
    ).fetchone()
    return SecretRef(
        user_id=user_id, name=name, key_id=kid, created_at=row["created_at"], updated_at=now
    )


def get_secret(
    conn: sqlite3.Connection, *, user_id: int, name: str, key_b64: str | None = None
) -> str | None:
    """Decrypt one credential, or None if this user has no such secret.

    A wrong key raises rather than returning None: "you configured the wrong
    key" and "this user never stored one" are different problems, and quietly
    treating the first as the second would send the app off to re-authenticate
    against a source it is already authorized for.
    """
    row = conn.execute(
        "SELECT ciphertext, nonce FROM user_secret WHERE user_id=? AND name=?",
        (user_id, name),
    ).fetchone()
    if row is None:
        return None
    key = _load_key(key_b64)
    aad = f"{user_id}:{name}".encode()
    try:
        return _aead(key).decrypt(bytes(row["nonce"]), bytes(row["ciphertext"]), aad).decode()
    except Exception as exc:  # InvalidTag and friends are all fatal here
        raise SecretsUnavailable(
            f"could not decrypt secret {name!r} for user {user_id} — wrong "
            f"{KEY_ENV}, or the stored value was tampered with"
        ) from exc


def delete_secret(conn: sqlite3.Connection, *, user_id: int, name: str) -> bool:
    """Remove one credential. True if something was deleted."""
    cur = conn.execute("DELETE FROM user_secret WHERE user_id=? AND name=?", (user_id, name))
    return bool(cur.rowcount)


def list_secrets(conn: sqlite3.Connection, *, user_id: int) -> list[SecretRef]:
    """What this user has stored — names and metadata only, never values."""
    return [
        SecretRef(
            user_id=user_id,
            name=r["name"],
            key_id=r["key_id"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in conn.execute(
            "SELECT name, key_id, created_at, updated_at FROM user_secret "
            "WHERE user_id=? ORDER BY name",
            (user_id,),
        )
    ]
