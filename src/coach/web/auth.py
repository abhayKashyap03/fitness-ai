"""Request authentication for the web app (ADR-0018).

Until now every route hardcoded ``user_id=1`` and the server bound to localhost.
This is the seam where that stops.

**The fail-closed rule.** There is exactly one situation in which a request may
proceed without a session: the server is bound to a loopback address AND nobody
has set a password yet. That is the existing single-user laptop workflow, and
breaking it would mean the owner must claim an account before they can read their
own data on their own machine.

The moment either condition stops holding, authentication is mandatory:

* Any user has a password  ->  every request needs a session, even on localhost.
  (You cannot lock the door and leave the back window open.)
* Bound to a non-loopback address  ->  the app REFUSES TO START unless the owner
  has claimed their account. Serving personal health data to a network with no
  credentials configured is the one failure mode that must be impossible, so it
  is a startup error rather than a runtime warning nobody reads.

Cookies are HttpOnly and SameSite=Lax; `Secure` is set whenever the request
arrived over HTTPS, so a proxied deployment gets it automatically without
breaking plain-HTTP localhost.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response

from ..store.users import User, get_user, session_user

SESSION_COOKIE = "coach_session"
_LOOPBACK = {"127.0.0.1", "localhost", "::1", ""}


class StartupRefused(RuntimeError):
    """Raised when a configuration would serve health data unauthenticated."""


@dataclass(frozen=True)
class AuthPolicy:
    """Whether this process may serve unauthenticated requests, and why."""

    bind_host: str
    open_local: bool  # True => loopback + nobody has claimed an account yet

    @property
    def is_loopback(self) -> bool:
        return self.bind_host in _LOOPBACK


def anyone_has_a_password(conn: sqlite3.Connection) -> bool:
    """True once any account is claimed — the switch that turns auth on."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM app_user WHERE password_hash IS NOT NULL"
    ).fetchone()
    return bool(row["n"])


def resolve_policy(conn: sqlite3.Connection, bind_host: str) -> AuthPolicy:
    """Decide the auth posture for this process, or refuse to run.

    Called once at startup so a misconfiguration surfaces before the first
    request rather than after somebody has already been served.
    """
    policy = AuthPolicy(bind_host=bind_host, open_local=False)
    claimed = anyone_has_a_password(conn)
    if policy.is_loopback:
        return AuthPolicy(bind_host=bind_host, open_local=not claimed)
    if not claimed:
        raise StartupRefused(
            f"refusing to bind {bind_host}: no account has a password yet, so every "
            "request would be served unauthenticated. Claim the owner account first:\n"
            "    coach user set-email you@example.com\n"
            "    coach user set-password\n"
            "Then restart. (On localhost this is allowed; on a network it is not.)"
        )
    return AuthPolicy(bind_host=bind_host, open_local=False)


def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    """Attach the session cookie.

    HttpOnly so page scripts can never read it; SameSite=Lax so a cross-site
    form post cannot ride it; Secure whenever the request came over HTTPS.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=30 * 24 * 3600,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def request_is_secure(request: Request) -> bool:
    """HTTPS, directly or via a proxy that sets X-Forwarded-Proto."""
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def current_user(conn: sqlite3.Connection, request: Request, policy: AuthPolicy) -> User | None:
    """The user behind this request, or None.

    A valid session always wins. Only when there is no session does the
    open-local allowance apply, and only while nobody has claimed an account —
    :func:`resolve_policy` has already refused to start otherwise.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        user = session_user(conn, token)
        if user is not None:
            return user
    if policy.open_local:
        return get_user(conn, 1)  # the local owner, unclaimed by definition here
    return None


def require_api_user(user: User | None) -> User:
    """401 for a JSON caller. Never redirects — a fetch() can't follow that."""
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require_owner(user: User) -> User:
    """Owner-only actions (issuing invites, managing accounts)."""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="owner only")
    return user
