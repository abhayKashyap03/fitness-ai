"""Users, invites, sessions (ADR-0018, migration 0014).

These are security properties, not features. The assertions that matter most are
the negative ones: that a password is never recoverable from the DB, that a
stolen dump cannot be replayed as a session, and that the login form cannot be
used to enumerate accounts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coach.store import users as U


def _rows(conn, sql, *params):
    return conn.execute(sql, params).fetchall()


# ---- password hashing -------------------------------------------------------


def test_password_round_trips():
    digest, salt = U.hash_password("correct horse battery staple")
    assert U.verify_password("correct horse battery staple", digest_hex=digest, salt_hex=salt)


def test_wrong_password_is_rejected():
    digest, salt = U.hash_password("correct horse battery staple")
    assert not U.verify_password("Correct horse battery staple", digest_hex=digest, salt_hex=salt)


def test_the_password_is_not_recoverable_from_what_is_stored():
    """The digest must not contain the password in any obvious form."""
    pw = "correct horse battery staple"
    digest, _salt = U.hash_password(pw)
    assert pw not in digest
    assert pw.encode().hex() not in digest


def test_same_password_hashes_differently_per_user():
    """Distinct salts: one cracked digest must not unlock every reuse of it."""
    d1, s1 = U.hash_password("correct horse battery staple")
    d2, s2 = U.hash_password("correct horse battery staple")
    assert s1 != s2
    assert d1 != d2


def test_short_passwords_are_refused():
    with pytest.raises(ValueError, match="at least"):
        U.hash_password("short")


def test_an_unclaimed_account_cannot_be_logged_into():
    """No password set is not 'any password works'."""
    assert not U.verify_password("anything", digest_hex=None, salt_hex=None)
    assert not U.verify_password("", digest_hex=None, salt_hex=None)


def test_a_corrupt_salt_fails_closed():
    digest, _ = U.hash_password("correct horse battery staple")
    assert not U.verify_password("correct horse battery staple", digest_hex=digest, salt_hex="zz")


# ---- the migration's seeded owner -------------------------------------------


def test_migration_seeds_the_existing_owner(migrated_conn):
    """Every canonical row already says user_id=1; it needs a row to point at."""
    owner = U.get_user(migrated_conn, 1)
    assert owner is not None
    assert owner.role == "owner"
    assert owner.status == "active"
    # ...but unclaimed: no invented email, no password (§2.7)
    assert owner.email is None
    assert owner.has_password is False


def test_the_seeded_owner_cannot_log_in_until_claimed(migrated_conn):
    U.set_email(migrated_conn, user_id=1, email="owner@example.test")
    with pytest.raises(ValueError, match="invalid email or password"):
        U.login(migrated_conn, email="owner@example.test", password="anything at all")


def test_claiming_the_owner_account(migrated_conn):
    U.set_email(migrated_conn, user_id=1, email="owner@example.test")
    U.set_password(migrated_conn, user_id=1, password="a sufficiently long one")
    user, token = U.login(
        migrated_conn, email="owner@example.test", password="a sufficiently long one"
    )
    assert user.id == 1 and user.role == "owner"
    assert U.session_user(migrated_conn, token) is not None


# ---- invites ----------------------------------------------------------------


def test_invite_token_is_not_stored(migrated_conn):
    """Only the hash reaches the DB, so a backup can't be mined for live invites."""
    token = U.create_invite(migrated_conn, email="friend@example.test", invited_by=1)
    stored = _rows(migrated_conn, "SELECT token_hash FROM user_invite")
    assert len(stored) == 1
    assert stored[0]["token_hash"] != token
    assert stored[0]["token_hash"] == U.hash_token(token)


def test_accepting_an_invite_creates_an_active_user(migrated_conn):
    token = U.create_invite(migrated_conn, email="friend@example.test", invited_by=1)
    user = U.accept_invite(migrated_conn, token=token, password="another long password")
    assert user.email == "friend@example.test"
    assert user.status == "active" and user.role == "member"
    assert user.id != 1  # a distinct tenant, not the owner


def test_an_invite_cannot_be_used_twice(migrated_conn):
    token = U.create_invite(migrated_conn, email="friend@example.test", invited_by=1)
    U.accept_invite(migrated_conn, token=token, password="another long password")
    with pytest.raises(ValueError, match="invalid, expired, or already used"):
        U.accept_invite(migrated_conn, token=token, password="yet another long one")


def test_an_expired_invite_is_refused(migrated_conn):
    token = U.create_invite(
        migrated_conn, email="friend@example.test", invited_by=1, ttl=timedelta(seconds=-1)
    )
    with pytest.raises(ValueError, match="invalid, expired, or already used"):
        U.accept_invite(migrated_conn, token=token, password="another long password")


def test_an_unknown_invite_is_refused_identically(migrated_conn):
    """Same message as expired/used: no probing which invites exist."""
    with pytest.raises(ValueError, match="invalid, expired, or already used"):
        U.accept_invite(migrated_conn, token=U.new_token(), password="another long password")


def test_cannot_invite_an_existing_account(migrated_conn):
    token = U.create_invite(migrated_conn, email="friend@example.test", invited_by=1)
    U.accept_invite(migrated_conn, token=token, password="another long password")
    with pytest.raises(ValueError, match="already has an account"):
        U.create_invite(migrated_conn, email="friend@example.test", invited_by=1)


# ---- sessions ---------------------------------------------------------------


def _member(conn, email="friend@example.test", password="another long password"):
    token = U.create_invite(conn, email=email, invited_by=1)
    return U.accept_invite(conn, token=token, password=password)


def test_session_token_is_not_stored(migrated_conn):
    """A stolen dump must not be replayable as a live session."""
    user = _member(migrated_conn)
    token = U.open_session(migrated_conn, user_id=user.id)
    stored = _rows(migrated_conn, "SELECT id FROM user_session")
    assert stored[0]["id"] != token
    assert stored[0]["id"] == U.hash_token(token)


def test_session_resolves_to_its_user(migrated_conn):
    user = _member(migrated_conn)
    token = U.open_session(migrated_conn, user_id=user.id)
    got = U.session_user(migrated_conn, token)
    assert got is not None and got.id == user.id


def test_an_unknown_session_token_resolves_to_nobody(migrated_conn):
    assert U.session_user(migrated_conn, U.new_token()) is None


def test_an_expired_session_resolves_to_nobody(migrated_conn):
    user = _member(migrated_conn)
    token = U.open_session(migrated_conn, user_id=user.id, ttl=timedelta(seconds=-1))
    assert U.session_user(migrated_conn, token) is None


def test_a_revoked_session_resolves_to_nobody(migrated_conn):
    user = _member(migrated_conn)
    token = U.open_session(migrated_conn, user_id=user.id)
    U.revoke_session(migrated_conn, token)
    assert U.session_user(migrated_conn, token) is None


def test_disabling_an_account_kills_its_live_sessions(migrated_conn):
    """A disabled user must not keep browsing on yesterday's cookie."""
    user = _member(migrated_conn)
    token = U.open_session(migrated_conn, user_id=user.id)
    assert U.session_user(migrated_conn, token) is not None
    migrated_conn.execute("UPDATE app_user SET status='disabled' WHERE id=?", (user.id,))
    assert U.session_user(migrated_conn, token) is None


def test_changing_a_password_revokes_every_session(migrated_conn):
    """A password change that leaves old sessions alive locks nobody out."""
    user = _member(migrated_conn)
    a = U.open_session(migrated_conn, user_id=user.id)
    b = U.open_session(migrated_conn, user_id=user.id)
    U.set_password(migrated_conn, user_id=user.id, password="a brand new long password")
    assert U.session_user(migrated_conn, a) is None
    assert U.session_user(migrated_conn, b) is None


def test_revoke_all_reports_how_many_were_live(migrated_conn):
    user = _member(migrated_conn)
    U.open_session(migrated_conn, user_id=user.id)
    U.open_session(migrated_conn, user_id=user.id)
    assert U.revoke_all_sessions(migrated_conn, user_id=user.id) == 2
    assert U.revoke_all_sessions(migrated_conn, user_id=user.id) == 0


# ---- login ------------------------------------------------------------------


def test_login_succeeds_with_the_right_password(migrated_conn):
    user = _member(migrated_conn)
    got, token = U.login(
        migrated_conn, email="friend@example.test", password="another long password"
    )
    assert got.id == user.id
    assert U.session_user(migrated_conn, token) is not None


def test_login_failures_are_indistinguishable(migrated_conn):
    """The login form must not become an account-enumeration oracle.

    A wrong password, an unknown email and a disabled account must all fail the
    same way — otherwise the error message tells an attacker which emails have
    accounts here, which for a health app is itself sensitive.
    """
    _member(migrated_conn)
    messages = set()
    for email, pw in (
        ("friend@example.test", "the wrong password entirely"),
        ("nobody@example.test", "another long password"),
    ):
        with pytest.raises(ValueError) as exc:
            U.login(migrated_conn, email=email, password=pw)
        messages.add(str(exc.value))
    assert len(messages) == 1, messages


def test_a_disabled_account_cannot_log_in(migrated_conn):
    user = _member(migrated_conn)
    migrated_conn.execute("UPDATE app_user SET status='disabled' WHERE id=?", (user.id,))
    with pytest.raises(ValueError, match="invalid email or password"):
        U.login(migrated_conn, email="friend@example.test", password="another long password")


def test_email_lookup_is_case_insensitive(migrated_conn):
    _member(migrated_conn)
    assert U.get_user_by_email(migrated_conn, "FRIEND@EXAMPLE.TEST") is not None


def test_two_users_cannot_share_an_email(migrated_conn):
    _member(migrated_conn)
    U.set_email(migrated_conn, user_id=1, email="owner@example.test")
    with pytest.raises(ValueError, match="already in use"):
        U.set_email(migrated_conn, user_id=1, email="friend@example.test")


# ---- housekeeping -----------------------------------------------------------


def test_purge_removes_only_long_dead_sessions(migrated_conn):
    user = _member(migrated_conn)
    live = U.open_session(migrated_conn, user_id=user.id)
    old = U.hash_token(U.new_token())
    ancient = (datetime.now(UTC) - timedelta(days=365)).isoformat()
    migrated_conn.execute(
        "INSERT INTO user_session (id, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (old, user.id, ancient, ancient),
    )
    assert U.purge_expired_sessions(migrated_conn) == 1
    assert U.session_user(migrated_conn, live) is not None


def test_login_burns_equal_work_for_a_missing_account(migrated_conn):
    """Enumeration by stopwatch, not by message.

    verify_password short-circuits when no digest exists, so without a
    deliberate dummy scrypt an unknown email would answer measurably faster
    than a real one. Compared as a ratio, generously, since wall-clock on a
    shared machine is noisy — the point is 'same order', not 'identical'.
    """
    import contextlib
    import time

    _member(migrated_conn)

    def timed(email: str) -> float:
        best = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            with contextlib.suppress(ValueError):
                U.login(migrated_conn, email=email, password="the wrong password here")
            best = min(best, time.perf_counter() - t0)
        return best

    real = timed("friend@example.test")
    missing = timed("nobody@example.test")
    assert 0.25 < (missing / real) < 4.0, (real, missing)
