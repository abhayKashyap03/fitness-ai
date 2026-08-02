"""Per-user credentials encrypted at rest (ADR-0018, migration 0014).

The property under test is what a stolen database is worth. These are other
people's WHOOP and MyFitnessPal credentials, so the negative assertions —
plaintext never present, wrong key fails loudly, one user's ciphertext useless
to another — are the point of the module.
"""

from __future__ import annotations

import pytest

from coach.store import secrets_store as S

KEY = S.generate_key()
OTHER_KEY = S.generate_key()
TOKEN = "whoop-refresh-abc123-do-not-leak"


def test_generate_key_is_a_valid_aes256_key():
    import base64

    assert len(base64.urlsafe_b64decode(S.generate_key())) == 32


def test_two_generated_keys_differ():
    assert S.generate_key() != S.generate_key()


def test_round_trip(migrated_conn):
    S.put_secret(migrated_conn, user_id=1, name="whoop_refresh_token", value=TOKEN, key_b64=KEY)
    got = S.get_secret(migrated_conn, user_id=1, name="whoop_refresh_token", key_b64=KEY)
    assert got == TOKEN


def test_plaintext_never_appears_in_the_database(migrated_conn):
    """The whole point: a dump must be worth nothing without the host key."""
    S.put_secret(migrated_conn, user_id=1, name="whoop_refresh_token", value=TOKEN, key_b64=KEY)
    row = migrated_conn.execute("SELECT ciphertext, nonce FROM user_secret").fetchone()
    blob = bytes(row["ciphertext"])
    assert TOKEN.encode() not in blob
    assert TOKEN.encode() not in bytes(row["nonce"])
    # and nothing anywhere else in the table either
    dump = b"".join(
        bytes(v) if isinstance(v, bytes | memoryview) else str(v).encode()
        for r in migrated_conn.execute("SELECT * FROM user_secret")
        for v in tuple(r)
    )
    assert TOKEN.encode() not in dump


def test_the_wrong_key_fails_loudly_not_silently(migrated_conn):
    """Wrong key and 'never stored one' are different problems.

    Returning None for a wrong key would send the app off to re-authenticate
    against a source it is already authorized for.
    """
    S.put_secret(migrated_conn, user_id=1, name="whoop_refresh_token", value=TOKEN, key_b64=KEY)
    with pytest.raises(S.SecretsUnavailable, match="could not decrypt"):
        S.get_secret(migrated_conn, user_id=1, name="whoop_refresh_token", key_b64=OTHER_KEY)


def test_a_missing_secret_is_none_not_an_error(migrated_conn):
    assert S.get_secret(migrated_conn, user_id=1, name="never_set", key_b64=KEY) is None


def test_one_users_ciphertext_cannot_be_replayed_into_another(migrated_conn):
    """user_id and name are authenticated additional data.

    Moving a ciphertext row between users must fail decryption rather than
    quietly handing over someone else's credential.
    """
    from coach.store import users as U

    tok = U.create_invite(migrated_conn, email="friend@example.test", invited_by=1)
    other = U.accept_invite(migrated_conn, token=tok, password="another long password")

    S.put_secret(migrated_conn, user_id=1, name="whoop_refresh_token", value=TOKEN, key_b64=KEY)
    row = migrated_conn.execute(
        "SELECT ciphertext, nonce, key_id, created_at, updated_at FROM user_secret WHERE user_id=1"
    ).fetchone()
    # transplant user 1's ciphertext onto the other user
    migrated_conn.execute(
        "INSERT INTO user_secret (user_id, name, ciphertext, nonce, key_id, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?)",
        (
            other.id,
            "whoop_refresh_token",
            row["ciphertext"],
            row["nonce"],
            row["key_id"],
            row["created_at"],
            row["updated_at"],
        ),
    )
    with pytest.raises(S.SecretsUnavailable, match="could not decrypt"):
        S.get_secret(migrated_conn, user_id=other.id, name="whoop_refresh_token", key_b64=KEY)


def test_a_secret_under_a_different_name_cannot_be_swapped_in(migrated_conn):
    """The name is bound too, so a cookie can't be served as a refresh token."""
    S.put_secret(migrated_conn, user_id=1, name="mfp_session_cookie", value="cookie", key_b64=KEY)
    row = migrated_conn.execute("SELECT ciphertext, nonce FROM user_secret").fetchone()
    migrated_conn.execute(
        "UPDATE user_secret SET name='whoop_refresh_token' WHERE user_id=1",
    )
    with pytest.raises(S.SecretsUnavailable, match="could not decrypt"):
        S.get_secret(migrated_conn, user_id=1, name="whoop_refresh_token", key_b64=KEY)
    assert row is not None  # (row read only to prove the ciphertext was untouched)


def test_rotating_a_secret_replaces_it_in_place(migrated_conn):
    """The one deliberately non-append-only table (ADR-0018).

    A superseded refresh token has no analytical value and keeping every
    historical ciphertext only widens the blast radius of a key compromise.
    """
    S.put_secret(migrated_conn, user_id=1, name="whoop_refresh_token", value="old", key_b64=KEY)
    S.put_secret(migrated_conn, user_id=1, name="whoop_refresh_token", value="new", key_b64=KEY)
    rows = migrated_conn.execute(
        "SELECT COUNT(*) n FROM user_secret WHERE user_id=1 AND name='whoop_refresh_token'"
    ).fetchone()
    assert rows["n"] == 1  # replaced, not versioned
    assert S.get_secret(migrated_conn, user_id=1, name="whoop_refresh_token", key_b64=KEY) == "new"


def test_each_write_uses_a_fresh_nonce(migrated_conn):
    """Nonce reuse under the same key is the classic way to break GCM."""
    seen = set()
    for i in range(5):
        S.put_secret(
            migrated_conn, user_id=1, name="whoop_refresh_token", value=f"v{i}", key_b64=KEY
        )
        row = migrated_conn.execute("SELECT nonce FROM user_secret WHERE user_id=1").fetchone()
        seen.add(bytes(row["nonce"]))
    assert len(seen) == 5


def test_missing_key_is_fatal_not_a_plaintext_fallback(migrated_conn, monkeypatch):
    """Storing credentials unencrypted when misconfigured is worse than refusing."""
    monkeypatch.delenv(S.KEY_ENV, raising=False)
    with pytest.raises(S.SecretsUnavailable, match="not set"):
        S.put_secret(migrated_conn, user_id=1, name="whoop_refresh_token", value=TOKEN)


def test_a_malformed_key_is_rejected(migrated_conn):
    with pytest.raises(S.SecretsUnavailable, match=r"base64|AES-256"):
        S.put_secret(migrated_conn, user_id=1, name="x", value="y", key_b64="not-valid-base64!!")


def test_a_short_key_is_rejected(migrated_conn):
    import base64

    short = base64.urlsafe_b64encode(b"tooshort").decode()
    with pytest.raises(S.SecretsUnavailable, match="AES-256"):
        S.put_secret(migrated_conn, user_id=1, name="x", value="y", key_b64=short)


def test_key_id_identifies_without_revealing(migrated_conn):
    import base64

    key = base64.urlsafe_b64decode(KEY)
    kid = S.key_id(key)
    assert len(kid) == 12
    assert kid != KEY
    assert S.key_id(key) == kid  # stable
    assert S.key_id(base64.urlsafe_b64decode(OTHER_KEY)) != kid


def test_list_secrets_never_returns_values(migrated_conn):
    S.put_secret(migrated_conn, user_id=1, name="whoop_refresh_token", value=TOKEN, key_b64=KEY)
    refs = S.list_secrets(migrated_conn, user_id=1)
    assert [r.name for r in refs] == ["whoop_refresh_token"]
    assert TOKEN not in repr(refs)


def test_delete_removes_the_secret(migrated_conn):
    S.put_secret(migrated_conn, user_id=1, name="whoop_refresh_token", value=TOKEN, key_b64=KEY)
    assert S.delete_secret(migrated_conn, user_id=1, name="whoop_refresh_token") is True
    assert S.get_secret(migrated_conn, user_id=1, name="whoop_refresh_token", key_b64=KEY) is None
    assert S.delete_secret(migrated_conn, user_id=1, name="whoop_refresh_token") is False


def test_secrets_are_scoped_per_user(migrated_conn):
    from coach.store import users as U

    tok = U.create_invite(migrated_conn, email="friend@example.test", invited_by=1)
    other = U.accept_invite(migrated_conn, token=tok, password="another long password")
    S.put_secret(migrated_conn, user_id=1, name="mfp_session_cookie", value="mine", key_b64=KEY)
    S.put_secret(
        migrated_conn, user_id=other.id, name="mfp_session_cookie", value="theirs", key_b64=KEY
    )
    assert S.get_secret(migrated_conn, user_id=1, name="mfp_session_cookie", key_b64=KEY) == "mine"
    assert (
        S.get_secret(migrated_conn, user_id=other.id, name="mfp_session_cookie", key_b64=KEY)
        == "theirs"
    )
