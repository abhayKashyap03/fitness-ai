"""Users, invites and sessions (ADR-0018, migration 0014).

Invite-only by decision: there is no public signup path, so an account cannot
exist without an invite being issued first.

Three rules here are security properties, not style choices:

* **Passwords are never stored.** ``hashlib.scrypt`` (stdlib — no dependency) with
  a per-user salt. Verification is constant-time via :func:`hmac.compare_digest`,
  so a timing side-channel can't be used to walk a digest.
* **Tokens are never stored.** Session and invite tokens are persisted as their
  SHA-256; the raw token exists only in the holder's cookie or invite link. A
  stolen database dump therefore cannot be replayed.
* **Absence stays absence (§2.7).** A user with no password is *unclaimed*, not
  a user with an empty password, and cannot authenticate at all.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# scrypt work factors. n=2**14 with r=8,p=1 is the widely used interactive
# baseline; maxmem must be set explicitly or CPython refuses the allocation
# (n * r * 128 * p = 16 MiB here).
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64
_SCRYPT_MAXMEM = 64 * 1024 * 1024

SESSION_TTL = timedelta(days=30)
INVITE_TTL = timedelta(days=7)

STATUSES = frozenset({"invited", "active", "disabled"})
ROLES = frozenset({"owner", "member"})

# Password floor. Deliberately a length rule and nothing else: composition rules
# ("one symbol, one digit") measurably push people toward weaker, more guessable
# passwords, and this is a beta for people who will use a password manager.
MIN_PASSWORD_LEN = 12


@dataclass(frozen=True)
class User:
    id: int
    email: str | None
    status: str
    role: str
    created_at: str
    activated_at: str | None
    has_password: bool


def _now() -> str:
    return datetime.now(UTC).isoformat()


def hash_token(token: str) -> str:
    """SHA-256 of a bearer token — what we persist instead of the token itself.

    Plain SHA-256 rather than a slow KDF on purpose: these are 256-bit random
    values from :func:`secrets.token_urlsafe`, not human-chosen secrets, so
    there is no dictionary to slow down and the lookup stays a fast index hit.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    """A fresh 256-bit URL-safe bearer token."""
    return secrets.token_urlsafe(32)


def hash_password(password: str, *, salt: bytes | None = None) -> tuple[str, str]:
    """(digest_hex, salt_hex) for a password. Never returns or logs the password."""
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LEN} characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return digest.hex(), salt.hex()


def verify_password(password: str, *, digest_hex: str | None, salt_hex: str | None) -> bool:
    """Constant-time password check. An unclaimed account always fails."""
    if not digest_hex or not salt_hex:
        return False  # no password set: unclaimed, not "any password works"
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    candidate = hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return hmac.compare_digest(candidate.hex(), digest_hex)


_DUMMY_SALT = b"\x00" * 16


def _burn_password_work(password: str) -> None:
    """Run the same scrypt cost as a real check, and throw the result away.

    Used when there is no account (or an unclaimed one) so that "no such email"
    takes as long as "wrong password". Without it the login endpoint is an
    account-enumeration oracle by stopwatch — which for a health app leaks who
    has an account here, not merely that a guess was wrong.
    """
    hashlib.scrypt(
        password.encode(),
        salt=_DUMMY_SALT,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )


def _to_user(r: sqlite3.Row) -> User:
    return User(
        id=r["id"],
        email=r["email"],
        status=r["status"],
        role=r["role"],
        created_at=r["created_at"],
        activated_at=r["activated_at"],
        has_password=bool(r["password_hash"]),
    )


def get_user(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute("SELECT * FROM app_user WHERE id = ?", (user_id,)).fetchone()
    return _to_user(row) if row else None


def get_user_by_email(conn: sqlite3.Connection, email: str) -> User | None:
    row = conn.execute(
        "SELECT * FROM app_user WHERE email = ? COLLATE NOCASE", (email.strip(),)
    ).fetchone()
    return _to_user(row) if row else None


def list_users(conn: sqlite3.Connection) -> list[User]:
    return [_to_user(r) for r in conn.execute("SELECT * FROM app_user ORDER BY id")]


# ---- invites ---------------------------------------------------------------


def create_invite(
    conn: sqlite3.Connection, *, email: str, invited_by: int, ttl: timedelta = INVITE_TTL
) -> str:
    """Issue an invite and return the RAW token — shown once, never stored.

    The caller is responsible for delivering it. Only its hash reaches the DB,
    so an invite cannot be recovered from a backup and re-used.
    """
    email = email.strip()
    if not email:
        raise ValueError("invite needs an email")
    if get_user_by_email(conn, email) is not None:
        raise ValueError(f"{email} already has an account")
    token = new_token()
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO user_invite (token_hash, email, invited_by, created_at, expires_at) "
        "VALUES (?,?,?,?,?)",
        (hash_token(token), email, invited_by, now.isoformat(), (now + ttl).isoformat()),
    )
    return token


def accept_invite(conn: sqlite3.Connection, *, token: str, password: str) -> User:
    """Redeem an invite, creating an active user with a password.

    Rejects an unknown, expired or already-redeemed token with the same generic
    message — a caller must not be able to probe which invites exist.
    """
    row = conn.execute(
        "SELECT * FROM user_invite WHERE token_hash = ?", (hash_token(token),)
    ).fetchone()
    now = datetime.now(UTC)
    if row is None or row["accepted_at"] is not None or row["expires_at"] <= now.isoformat():
        raise ValueError("invite is invalid, expired, or already used")

    digest, salt = hash_password(password)
    cur = conn.execute(
        "INSERT INTO app_user (email, password_hash, password_salt, status, role, "
        "created_at, activated_at) VALUES (?,?,?,'active','member',?,?)",
        (row["email"], digest, salt, now.isoformat(), now.isoformat()),
    )
    user_id = int(cur.lastrowid or 0)
    conn.execute(
        "UPDATE user_invite SET accepted_at = ?, accepted_user = ? WHERE token_hash = ?",
        (now.isoformat(), user_id, row["token_hash"]),
    )
    user = get_user(conn, user_id)
    assert user is not None
    return user


def set_password(conn: sqlite3.Connection, *, user_id: int, password: str) -> None:
    """Set or replace a password, and mark an unclaimed account active.

    Every existing session is revoked: a password change that leaves old
    sessions alive doesn't actually lock anyone out.
    """
    digest, salt = hash_password(password)
    now = _now()
    conn.execute(
        "UPDATE app_user SET password_hash = ?, password_salt = ?, status = 'active', "
        "activated_at = COALESCE(activated_at, ?) WHERE id = ?",
        (digest, salt, now, user_id),
    )
    revoke_all_sessions(conn, user_id=user_id)


def set_email(conn: sqlite3.Connection, *, user_id: int, email: str) -> None:
    """Claim an account's email (the local owner starts with none)."""
    email = email.strip()
    if not email:
        raise ValueError("email cannot be empty")
    existing = get_user_by_email(conn, email)
    if existing is not None and existing.id != user_id:
        raise ValueError(f"{email} is already in use")
    conn.execute("UPDATE app_user SET email = ? WHERE id = ?", (email, user_id))


# ---- sessions --------------------------------------------------------------


def login(
    conn: sqlite3.Connection, *, email: str, password: str, user_agent: str | None = None
) -> tuple[User, str]:
    """Authenticate and open a session. Returns (user, RAW session token).

    A wrong password, an unknown email, a disabled account and an unclaimed one
    all raise the same message on purpose: distinguishing them turns the login
    form into an account-enumeration oracle.
    """
    user = get_user_by_email(conn, email)
    row = (
        conn.execute(
            "SELECT password_hash, password_salt FROM app_user WHERE id = ?", (user.id,)
        ).fetchone()
        if user
        else None
    )
    digest = row["password_hash"] if row else None
    salt = row["password_salt"] if row else None

    if digest and salt:
        password_ok = verify_password(password, digest_hex=digest, salt_hex=salt)
    else:
        # No account, or an unclaimed one. Burn the SAME scrypt work anyway:
        # verify_password() short-circuits on a missing digest, so without this
        # a nonexistent email would answer measurably faster than a real one and
        # the login form would leak which addresses have accounts here.
        _burn_password_work(password)
        password_ok = False

    if not password_ok or user is None or user.status != "active":
        raise ValueError("invalid email or password")
    return user, open_session(conn, user_id=user.id, user_agent=user_agent)


def open_session(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    ttl: timedelta = SESSION_TTL,
    user_agent: str | None = None,
) -> str:
    """Create a session and return the RAW token (only its hash is stored)."""
    token = new_token()
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO user_session (id, user_id, created_at, expires_at, last_seen_at, "
        "user_agent) VALUES (?,?,?,?,?,?)",
        (
            hash_token(token),
            user_id,
            now.isoformat(),
            (now + ttl).isoformat(),
            now.isoformat(),
            (user_agent or "")[:200] or None,
        ),
    )
    return token


def session_user(conn: sqlite3.Connection, token: str) -> User | None:
    """The user behind a session token, or None if it is invalid.

    Expired, revoked and unknown all return None identically. Touches
    ``last_seen_at`` so the human can review their own sessions.
    """
    now = _now()
    row = conn.execute(
        "SELECT user_id FROM user_session WHERE id = ? AND revoked_at IS NULL AND expires_at > ?",
        (hash_token(token), now),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE user_session SET last_seen_at = ? WHERE id = ?", (now, hash_token(token)))
    user = get_user(conn, row["user_id"])
    if user is None or user.status != "active":
        return None  # disabled mid-session: the session dies with the account
    return user


def revoke_session(conn: sqlite3.Connection, token: str) -> None:
    """Log out one session (idempotent)."""
    conn.execute(
        "UPDATE user_session SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (_now(), hash_token(token)),
    )


def revoke_all_sessions(conn: sqlite3.Connection, *, user_id: int) -> int:
    """Log out everywhere. Returns how many sessions were live."""
    cur = conn.execute(
        "UPDATE user_session SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
        (_now(), user_id),
    )
    return cur.rowcount or 0


def purge_expired_sessions(conn: sqlite3.Connection) -> int:
    """Delete sessions that are long dead. Housekeeping, not a security control."""
    cutoff = (datetime.now(UTC) - SESSION_TTL).isoformat()
    cur = conn.execute("DELETE FROM user_session WHERE expires_at < ?", (cutoff,))
    return cur.rowcount or 0
