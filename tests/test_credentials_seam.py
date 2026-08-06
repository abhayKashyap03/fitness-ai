"""Source credentials: file vs encrypted, and the one-way move between them.

ADR-0018 §3. The interesting cases are all about the migration, because that is
where a mistake either loses a token or quietly leaves a plaintext copy on disk
— the exact thing the move exists to prevent.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from coach.adapters.whoop.auth import TokenSet, TokenStore
from coach.config import Settings
from coach.services.credentials import (
    WHOOP_SECRET_NAME,
    DbTokenStore,
    credential_backend,
    whoop_token_store,
)
from coach.store import db
from coach.store.secrets_store import get_secret


@pytest.fixture
def key(monkeypatch) -> str:
    k = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("COACH_SECRET_KEY", k)
    return k


def _tokens() -> TokenSet:
    return TokenSet(
        access_token="access-abc",
        refresh_token="refresh-xyz",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope="read:recovery",
        token_type="bearer",
    )


def _settings(db_path, secret_key: str = "") -> Settings:
    return Settings(
        db_path=db_path,
        user_id=1,
        home_tz="America/New_York",
        units="metric",
        log_level="INFO",
        secret_key=secret_key,
    )


# ---- which backend is chosen ----------------------------------------------


def test_no_key_configured_keeps_the_file_backend(db_path):
    """The laptop workflow must keep working untouched."""
    store = whoop_token_store(_settings(db_path))
    assert isinstance(store, TokenStore)
    assert not isinstance(store, DbTokenStore)
    assert credential_backend(_settings(db_path)) == "file"


def test_a_configured_key_switches_to_encrypted_without_a_flag(db_path):
    """Deliberately not opt-in: a flag makes the secure path the one you have to
    remember, and this runs on a host where forgetting is expensive."""
    s = _settings(db_path, secret_key="irrelevant-for-selection")
    assert isinstance(whoop_token_store(s), DbTokenStore)
    assert credential_backend(s) == "encrypted (user_secret)"


# ---- the encrypted backend -------------------------------------------------


def test_round_trip_through_the_encrypted_store(migrated_conn, db_path, key):
    migrated_conn.commit()
    store = DbTokenStore(db_path, user_id=1)
    assert store.exists() is False
    assert store.load() is None

    store.save(_tokens())
    assert store.exists() is True
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "access-abc"
    assert loaded.refresh_token == "refresh-xyz"


def test_the_token_is_not_readable_without_the_key(migrated_conn, db_path, key):
    """The whole point (ADR-0018): a stolen dump yields ciphertext.

    Asserted against the raw column rather than the API, because the API would
    happily decrypt for us and prove nothing.
    """
    migrated_conn.commit()
    DbTokenStore(db_path, user_id=1).save(_tokens())

    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT ciphertext FROM user_secret WHERE user_id=1 AND name=?",
            (WHOOP_SECRET_NAME,),
        ).fetchone()
    finally:
        conn.close()
    blob = bytes(row["ciphertext"])
    assert b"refresh-xyz" not in blob
    assert b"access-abc" not in blob


def test_saving_twice_replaces_rather_than_accumulating(migrated_conn, db_path, key):
    """`user_secret` is the one deliberately non-append-only table (ADR-0018):
    a superseded refresh token has no analytical value and keeping every
    ciphertext only widens the blast radius."""
    migrated_conn.commit()
    store = DbTokenStore(db_path, user_id=1)
    store.save(_tokens())
    store.save(_tokens())

    conn = db.connect(db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM user_secret WHERE user_id=1 AND name=?",
            (WHOOP_SECRET_NAME,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


# ---- the migration ---------------------------------------------------------


def test_an_existing_file_token_is_adopted_on_first_load(migrated_conn, db_path, key, tmp_path):
    """Nobody should have to re-authorize a source they are already authorized
    for just because the storage backend changed."""
    migrated_conn.commit()
    legacy = tmp_path / "whoop_token.json"
    legacy.write_text(json.dumps(_tokens().to_dict()), encoding="utf-8")

    store = DbTokenStore(db_path, user_id=1, legacy_path=legacy)
    loaded = store.load()

    assert loaded is not None
    assert loaded.refresh_token == "refresh-xyz"
    conn = db.connect(db_path)
    try:
        assert get_secret(conn, user_id=1, name=WHOOP_SECRET_NAME) is not None
    finally:
        conn.close()


def test_the_plaintext_file_is_moved_aside_not_left_in_place(migrated_conn, db_path, key, tmp_path):
    """A credential in two places means one is a stale copy nobody watches — and
    the file is the copy that leaves the machine inside a backup."""
    migrated_conn.commit()
    legacy = tmp_path / "whoop_token.json"
    legacy.write_text(json.dumps(_tokens().to_dict()), encoding="utf-8")

    DbTokenStore(db_path, user_id=1, legacy_path=legacy).load()

    assert not legacy.exists()
    retired = legacy.with_suffix(legacy.suffix + ".migrated")
    assert retired.exists(), "the file must be renamed, never deleted (§8.5)"
    # ...and it still contains the token, so a botched migration is recoverable.
    assert "refresh-xyz" in retired.read_text(encoding="utf-8")


def test_the_database_wins_once_it_has_a_value(migrated_conn, db_path, key, tmp_path):
    """A stale file must never override a rotated token in the DB."""
    migrated_conn.commit()
    store = DbTokenStore(db_path, user_id=1, legacy_path=tmp_path / "whoop_token.json")
    store.save(_tokens())

    stale = tmp_path / "whoop_token.json"
    stale.write_text(
        json.dumps(
            TokenSet(
                access_token="OLD",
                refresh_token="OLD-REFRESH",
                expires_at=datetime.now(UTC),
                scope="",
                token_type="bearer",
            ).to_dict()
        ),
        encoding="utf-8",
    )
    loaded = store.load()
    assert loaded is not None
    assert loaded.refresh_token == "refresh-xyz"


def test_one_users_ciphertext_cannot_be_read_as_anothers(migrated_conn, db_path, key):
    """AAD binds user_id+name, so a row moved between users fails to decrypt
    rather than silently handing over someone else's credential."""
    from coach.store.secrets_store import SecretsUnavailable

    migrated_conn.commit()
    DbTokenStore(db_path, user_id=1).save(_tokens())

    conn = db.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO app_user (id, email, status, role, created_at) "
            "VALUES (2,'other@example.test','active','member','2026-08-03T00:00:00+00:00')"
        )
        conn.execute("UPDATE user_secret SET user_id=2 WHERE user_id=1")
        conn.commit()
        with pytest.raises(SecretsUnavailable):
            get_secret(conn, user_id=2, name=WHOOP_SECRET_NAME)
    finally:
        conn.close()
