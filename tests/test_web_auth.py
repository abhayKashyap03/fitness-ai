"""Web authentication and tenant isolation (ADR-0018).

The single most important test in this file is
``test_a_member_cannot_see_the_owners_data``. Everything else is plumbing; that
one is the actual promise of multi-tenancy, and if it ever fails the app is
leaking one person's health data to another.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="web UI needs the optional [web] extra")

from fastapi.testclient import TestClient

from coach.config import Settings
from coach.store import users as U
from coach.web.app import create_app
from coach.web.auth import SESSION_COOKIE, StartupRefused

DAY = "2026-03-15"
OWNER_PW = "owner password long enough"
MEMBER_PW = "member password long enough"


def _settings(db_path) -> Settings:
    return Settings(
        db_path=db_path, user_id=1, home_tz="America/New_York", units="metric", log_level="INFO"
    )


def _claim_owner(conn) -> None:
    U.set_email(conn, user_id=1, email="owner@example.test")
    U.set_password(conn, user_id=1, password=OWNER_PW)
    conn.commit()


def _add_member(conn, email="friend@example.test", password=MEMBER_PW):
    token = U.create_invite(conn, email=email, invited_by=1)
    user = U.accept_invite(conn, token=token, password=password)
    conn.commit()
    return user


# ---- the open-local allowance ----------------------------------------------


def test_localhost_with_no_account_claimed_still_works(migrated_conn, db_path):
    """The existing single-user laptop workflow must not break.

    Nobody has claimed an account, and we're on loopback — the owner can read
    their own data on their own machine without inventing a login first.

    Amended 2026-08-02: the §8.6 consent gate applies here too, so the first
    request lands on /safety. That is a one-time screen, not a login — no
    credentials are invented and the allowance is intact. See
    ``test_the_open_local_owner_is_gated_too`` for why this case is not exempt.
    """
    migrated_conn.commit()
    _ack_everyone(db_path)
    client = TestClient(create_app(_settings(db_path), bind_host="127.0.0.1"))
    assert client.get(f"/api/status?date={DAY}").status_code == 200
    assert client.get(f"/?date={DAY}").status_code == 200


def test_claiming_an_account_closes_the_open_allowance(migrated_conn, db_path):
    """You cannot lock the door and leave the back window open.

    Once ANY password exists the local allowance is gone, even on loopback —
    otherwise setting a password would protect the network but not the laptop.
    """
    _claim_owner(migrated_conn)
    client = TestClient(create_app(_settings(db_path), bind_host="127.0.0.1"))
    assert client.get(f"/api/status?date={DAY}").status_code == 401
    r = client.get(f"/?date={DAY}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_binding_a_network_address_unclaimed_refuses_to_start(migrated_conn, db_path):
    """The failure that must be impossible, not merely discouraged."""
    migrated_conn.commit()
    with pytest.raises(StartupRefused, match="refusing to bind"):
        create_app(_settings(db_path), bind_host="0.0.0.0")


def test_binding_a_network_address_once_claimed_is_allowed(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    app = create_app(_settings(db_path), bind_host="0.0.0.0")
    assert app.state.auth_policy.open_local is False


# ---- login / logout ---------------------------------------------------------


def _ack_everyone(db_path) -> None:
    """Pre-accept the §8.6 disclaimer for every account that exists.

    These tests are about authentication and tenant isolation. The consent gate
    sits in front of both and is tested on its own in
    ``tests/test_disclaimer_gate.py``; here it would only add a click to every
    case and hide what each one is actually asserting.
    """
    from coach.store import db as _db
    from coach.store import disclaimer as D

    conn = _db.connect(db_path)
    try:
        for row in conn.execute("SELECT id FROM app_user").fetchall():
            if not D.has_acknowledged(conn, user_id=row["id"]):
                D.acknowledge(conn, user_id=row["id"], user_agent="pytest")
        conn.commit()
    finally:
        conn.close()


def _client(db_path, host="127.0.0.1") -> TestClient:
    _ack_everyone(db_path)
    return TestClient(create_app(_settings(db_path), bind_host=host))


def test_login_sets_a_session_and_opens_the_app(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    client = _client(db_path)
    r = client.post(
        "/login",
        data={"email": "owner@example.test", "password": OWNER_PW},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.cookies.get(SESSION_COOKIE)
    assert client.get(f"/api/status?date={DAY}").status_code == 200


def test_a_wrong_password_does_not_open_a_session(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    client = _client(db_path)
    r = client.post("/login", data={"email": "owner@example.test", "password": "not the password"})
    assert r.status_code == 401
    assert client.get(f"/api/status?date={DAY}").status_code == 401


def test_login_page_does_not_reveal_whether_an_email_exists(migrated_conn, db_path):
    """Same body for a real and an unknown address."""
    _claim_owner(migrated_conn)
    client = _client(db_path)
    real = client.post(
        "/login", data={"email": "owner@example.test", "password": "wrong wrong wrong"}
    )
    fake = client.post(
        "/login", data={"email": "nobody@example.test", "password": "wrong wrong wrong"}
    )
    assert real.status_code == fake.status_code == 401
    assert real.text == fake.text


def test_logout_revokes_the_session(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    client = _client(db_path)
    client.post(
        "/login",
        data={"email": "owner@example.test", "password": OWNER_PW},
        follow_redirects=False,
    )
    assert client.get(f"/api/status?date={DAY}").status_code == 200
    client.post("/logout", follow_redirects=False)
    assert client.get(f"/api/status?date={DAY}").status_code == 401


def test_the_session_cookie_is_httponly_and_samesite(migrated_conn, db_path):
    """Page scripts must never read it; a cross-site form must not ride it."""
    _claim_owner(migrated_conn)
    client = _client(db_path)
    r = client.post(
        "/login",
        data={"email": "owner@example.test", "password": OWNER_PW},
        follow_redirects=False,
    )
    raw = r.headers["set-cookie"].lower()
    assert "httponly" in raw
    assert "samesite=lax" in raw


# ---- invite flow ------------------------------------------------------------


def test_invite_link_creates_an_account_and_signs_in(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    token = U.create_invite(migrated_conn, email="friend@example.test", invited_by=1)
    migrated_conn.commit()
    client = _client(db_path)
    assert client.get(f"/invite/{token}").status_code == 200
    r = client.post(f"/invite/{token}", data={"password": MEMBER_PW}, follow_redirects=False)
    assert r.status_code == 303
    # A brand-new account has not accepted the §8.6 notice, so the API is closed
    # to it until it does — the invite page's short notice is not the agreement.
    assert client.get(f"/api/status?date={DAY}").status_code == 403
    _ack_everyone(db_path)
    assert client.get(f"/api/status?date={DAY}").status_code == 200


def test_a_used_invite_cannot_create_a_second_account(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    token = U.create_invite(migrated_conn, email="friend@example.test", invited_by=1)
    migrated_conn.commit()
    client = _client(db_path)
    client.post(f"/invite/{token}", data={"password": MEMBER_PW}, follow_redirects=False)
    again = _client(db_path).post(f"/invite/{token}", data={"password": "another long one"})
    assert again.status_code == 400


def test_the_invite_page_does_not_confirm_a_token_exists(migrated_conn, db_path):
    """Rendering the form for any token: validity is only revealed on POST."""
    _claim_owner(migrated_conn)
    client = _client(db_path)
    assert client.get(f"/invite/{U.new_token()}").status_code == 200


# ---- the promise ------------------------------------------------------------


def test_a_member_cannot_see_the_owners_data(migrated_conn, db_path):
    """THE test. Two tenants, one database, no leakage.

    The owner has a weigh-in; the member has none. If the member's session ever
    returns the owner's number, multi-tenancy is broken and one person's health
    data is being served to another.
    """
    _claim_owner(migrated_conn)
    member = _add_member(migrated_conn)
    migrated_conn.execute(
        "INSERT INTO weight_measurement (id, user_id, day_key, source, source_app, "
        "weight_kg, raw_ref, derived_at) VALUES "
        "('wt:owner:1',1,?,'healthkit','okok',83.0,NULL,?)",
        (DAY, f"{DAY}T00:00:00+00:00"),
    )
    migrated_conn.commit()

    owner_client = _client(db_path)
    owner_client.post(
        "/login",
        data={"email": "owner@example.test", "password": OWNER_PW},
        follow_redirects=False,
    )
    owner_series = owner_client.get(f"/api/weight-trend?end={DAY}").json()["series"]
    assert owner_series and owner_series[-1]["weight_kg"] == 83.0

    member_client = _client(db_path)
    member_client.post(
        "/login",
        data={"email": "friend@example.test", "password": MEMBER_PW},
        follow_redirects=False,
    )
    member_body = member_client.get(f"/api/weight-trend?end={DAY}").json()
    assert member_body["series"] == []
    assert member_body["latest_trend_kg"] is None
    assert "83.0" not in member_client.get(f"/?date={DAY}").text
    assert member.id != 1


def test_a_members_writes_land_on_their_own_account(migrated_conn, db_path):
    """A plan set by a member must not become the owner's plan."""
    _claim_owner(migrated_conn)
    member = _add_member(migrated_conn)
    client = _client(db_path)
    client.post(
        "/login",
        data={"email": "friend@example.test", "password": MEMBER_PW},
        follow_redirects=False,
    )
    client.post("/plan", data={"mode": "rate", "rate": "-0.5"}, follow_redirects=False)
    rows = migrated_conn.execute("SELECT user_id FROM plan WHERE is_active = 1").fetchall()
    assert [r["user_id"] for r in rows] == [member.id]


# ---- closed by default ------------------------------------------------------


def test_every_api_route_refuses_an_anonymous_caller(migrated_conn, db_path):
    """A new route must be protected by existing, not by remembering."""
    _claim_owner(migrated_conn)
    client = _client(db_path)
    for path in (
        f"/api/status?date={DAY}",
        f"/api/tdee?end={DAY}",
        f"/api/weight-trend?end={DAY}",
        f"/api/recovery?end={DAY}",
        f"/api/sleep?end={DAY}",
        f"/api/plan?end={DAY}",
        f"/api/training?date={DAY}",
        f"/api/safety?end={DAY}",
        "/api/doctor",
        "/api/jobs",
    ):
        assert client.get(path).status_code == 401, path


def test_health_check_stays_public_and_says_nothing(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    r = _client(db_path).get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_jobs_cannot_be_started_anonymously(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    client = _client(db_path)
    assert client.post("/api/jobs/normalize", json={}).status_code == 401


# ---- /account ---------------------------------------------------------------
#
# The nav linked /account before the route existed, so the tab 404'd with
# {"detail":"Not found"}. Shipped in the same PR that added the link.


def _signed_in(db_path, email="owner@example.test", password=OWNER_PW) -> TestClient:
    client = _client(db_path)
    client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    return client


def test_account_page_exists_for_every_link_in_the_nav(migrated_conn, db_path):
    """A nav link with no route behind it is a 404 the user finds, not a test."""
    _claim_owner(migrated_conn)
    client = _signed_in(db_path)
    body = client.get("/").text
    for href in ("/", "/plan", "/coach", "/ops", "/doctor", "/account"):
        if f'href="{href}"' in body:
            assert client.get(href).status_code == 200, href


def test_account_shows_who_you_are(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    r = _signed_in(db_path).get("/account")
    assert r.status_code == 200
    assert "owner@example.test" in r.text


def test_changing_the_password_requires_the_current_one(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    client = _signed_in(db_path)
    r = client.post(
        "/account/password",
        data={"current": "not my password", "new": "a fresh long password"},
    )
    assert r.status_code == 200
    assert "current password is incorrect" in r.text
    # the old password must still work
    assert _signed_in(db_path).get("/account").status_code == 200


def test_changing_the_password_signs_you_out(migrated_conn, db_path):
    """Every session is revoked, which is the point of changing it."""
    _claim_owner(migrated_conn)
    client = _signed_in(db_path)
    r = client.post(
        "/account/password",
        data={"current": OWNER_PW, "new": "a fresh long password"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert client.get("/api/status", follow_redirects=False).status_code == 401
    # and the NEW password works
    fresh = _signed_in(db_path, password="a fresh long password")
    assert fresh.get("/account").status_code == 200


def test_owner_can_mint_an_invite_link_from_the_page(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    r = _signed_in(db_path).post("/account/invite", data={"email": "friend@example.test"})
    assert r.status_code == 200
    assert "/invite/" in r.text


def test_a_member_cannot_mint_invites(migrated_conn, db_path):
    """Invite-only means the owner decides who joins."""
    _claim_owner(migrated_conn)
    _add_member(migrated_conn)
    client = _signed_in(db_path, email="friend@example.test", password=MEMBER_PW)
    assert client.post("/account/invite", data={"email": "gatecrasher@x.test"}).status_code == 403


def test_a_member_does_not_see_the_account_list(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    _add_member(migrated_conn)
    body = _signed_in(db_path, email="friend@example.test", password=MEMBER_PW).get("/account").text
    assert "owner@example.test" not in body


def test_revoke_all_sessions_signs_you_out(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    client = _signed_in(db_path)
    r = client.post("/account/sessions/revoke", follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/status").status_code == 401


def test_password_forms_advertise_the_real_minimum(migrated_conn, db_path):
    """The templates hardcoded 12 while the constant said 8.

    A browser would then refuse a password the server accepts — a silent,
    confusing block on a form that looks like it is working.
    """
    from coach.store.users import MIN_PASSWORD_LEN

    _claim_owner(migrated_conn)
    token = U.create_invite(migrated_conn, email="friend@example.test", invited_by=1)
    migrated_conn.commit()
    invite_body = _client(db_path).get(f"/invite/{token}").text
    account_body = _signed_in(db_path).get("/account").text
    for body in (invite_body, account_body):
        assert f'minlength="{MIN_PASSWORD_LEN}"' in body
        assert f"At least {MIN_PASSWORD_LEN} characters" in body


def test_a_member_can_reach_their_own_account_page(migrated_conn, db_path):
    """The Account tab was owner-gated in the nav, so a member had no route to
    their own password. On a hosted box they have no CLI either — that left them
    with no way to change it at all."""
    _claim_owner(migrated_conn)
    _add_member(migrated_conn)
    client = _signed_in(db_path, email="friend@example.test", password=MEMBER_PW)
    body = client.get("/").text
    assert 'href="/account"' in body, "members need the Account tab in the nav"
    r = client.get("/account")
    assert r.status_code == 200
    assert "friend@example.test" in r.text
    assert "Change password" in r.text


def test_a_member_can_actually_change_their_password(migrated_conn, db_path):
    _claim_owner(migrated_conn)
    _add_member(migrated_conn)
    client = _signed_in(db_path, email="friend@example.test", password=MEMBER_PW)
    r = client.post(
        "/account/password",
        data={"current": MEMBER_PW, "new": "a brand new member password"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    fresh = _signed_in(db_path, email="friend@example.test", password="a brand new member password")
    assert fresh.get("/account").status_code == 200


def test_a_member_sees_no_owner_only_controls(migrated_conn, db_path):
    """Ungating the tab must not ungate what is behind it."""
    _claim_owner(migrated_conn)
    _add_member(migrated_conn)
    body = _signed_in(db_path, email="friend@example.test", password=MEMBER_PW).get("/account").text
    assert "Create invite link" not in body
    assert "Invite someone" not in body
    assert "owner@example.test" not in body
