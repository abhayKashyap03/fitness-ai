"""Where source credentials live — one seam, two backends (ADR-0018 §3).

On a laptop a WHOOP refresh token in `.credentials/u1/whoop_token.json` is fine:
it is a file only the owner can read. On a host it is not. The database gets
backed up, pulled to another machine and restored elsewhere
([ADR-0019](../../../docs/adr/0019-hosting-the-owners-instance.md) §5-6), and a
credential sitting beside it in plaintext travels with every copy.

ADR-0018 built `user_secret` — AES-256-GCM, key held in the host environment and
never in the database — for exactly this. This module is the seam that decides
which backend is in play, so no caller has to.

**The rule: if a key is configured, the database wins.** Not a flag, because a
flag means the secure path is the one you have to remember. Configuring
`COACH_SECRET_KEY` is already a deliberate act.

**Migration moves, never copies.** A credential existing in two places means one
of them is a stale copy nobody is watching, and the file is the copy that leaves
the machine in a backup. The file is renamed aside — never deleted (§8.5), since
a botched migration must not cost the token.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ..adapters.whoop.auth import TokenSet, TokenStore
from ..store import db
from ..store.secrets_store import SecretsUnavailable, get_secret, put_secret

WHOOP_SECRET_NAME = "whoop_token"


class DbTokenStore(TokenStore):
    """A :class:`TokenStore` whose bytes live encrypted in ``user_secret``.

    Subclasses the file store so it satisfies the same interface every caller
    already uses (``exists``/``load``/``save``) — the swap is invisible to the
    OAuth client, the ingest path and the doctor.

    ``path`` is retained only so diagnostics can still name a location for the
    human. It is not read from or written to.
    """

    def __init__(self, db_path: Path, user_id: int, *, legacy_path: Path | None = None):
        super().__init__(legacy_path or db_path)
        self._db_path = db_path
        self._user_id = user_id
        self._legacy = legacy_path

    def _conn(self) -> sqlite3.Connection:
        return db.connect(self._db_path)

    def exists(self) -> bool:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM user_secret WHERE user_id=? AND name=?",
                (self._user_id, WHOOP_SECRET_NAME),
            ).fetchone()
        finally:
            conn.close()
        return row is not None or (self._legacy is not None and self._legacy.exists())

    def load(self) -> TokenSet | None:
        conn = self._conn()
        try:
            raw = get_secret(conn, user_id=self._user_id, name=WHOOP_SECRET_NAME)
            if raw is not None:
                return TokenSet.from_dict(json.loads(raw))
            # Nothing stored yet. If a legacy file is sitting there, this is the
            # first run after the switch — adopt it now rather than making the
            # user re-authorize a source they are already authorized for.
            if self._legacy is not None and self._legacy.exists():
                tokens = TokenSet.from_dict(json.loads(self._legacy.read_text(encoding="utf-8")))
                self._save_to_db(conn, tokens)
                conn.commit()
                self._retire_legacy()
                return tokens
        finally:
            conn.close()
        return None

    def save(self, tokens: TokenSet) -> None:
        conn = self._conn()
        try:
            self._save_to_db(conn, tokens)
            conn.commit()
        finally:
            conn.close()
        self._retire_legacy()

    def _save_to_db(self, conn: sqlite3.Connection, tokens: TokenSet) -> None:
        put_secret(
            conn,
            user_id=self._user_id,
            name=WHOOP_SECRET_NAME,
            value=json.dumps(tokens.to_dict()),
        )

    def _retire_legacy(self) -> None:
        """Rename the plaintext file aside once the DB holds the credential.

        Renamed, not deleted (§8.5). Leaving it in place would keep a plaintext
        refresh token on disk indefinitely, which is the whole thing this move
        exists to stop; deleting it would mean a mistake here costs the token.
        """
        if self._legacy is None or not self._legacy.exists():
            return
        retired = self._legacy.with_suffix(self._legacy.suffix + ".migrated")
        if retired.exists():
            retired.unlink()  # a previous retirement; the DB is authoritative now
        self._legacy.replace(retired)


def whoop_token_store(settings) -> TokenStore:
    """The right WHOOP token store for this configuration.

    Encrypted-in-database when ``COACH_SECRET_KEY`` is set, plain file otherwise
    — so the laptop workflow keeps working untouched and a host gets the secure
    path without anyone having to opt in.
    """
    from ..paths import whoop_token_path

    file_path = whoop_token_path(settings.user_id)
    if not getattr(settings, "secret_key", None):
        return TokenStore(file_path)
    return DbTokenStore(settings.db_path, settings.user_id, legacy_path=file_path)


def credential_backend(settings) -> str:
    """Human-readable name of the active backend, for `coach doctor`."""
    return "encrypted (user_secret)" if getattr(settings, "secret_key", None) else "file"


__all__ = [
    "WHOOP_SECRET_NAME",
    "DbTokenStore",
    "SecretsUnavailable",
    "credential_backend",
    "whoop_token_store",
]
