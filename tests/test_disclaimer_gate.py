"""The §8.6 consent gate in the web app (migration 0015).

Every other web test opts past this gate via the ``acknowledged`` fixture, so
this file owns it. The cases that matter are the ones where a consent gate stops
being a safety feature and becomes either theatre or a trap.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="web UI needs the optional [web] extra")

from fastapi.testclient import TestClient

from coach.config import Settings
from coach.disclaimer import DISCLAIMER_VERSION, SHORT
from coach.store import disclaimer as D
from coach.store import users as U
from coach.web.app import create_app

DAY = "2026-03-15"
OWNER_PW = "owner password long enough"


def _settings(db_path) -> Settings:
    return Settings(
        db_path=db_path, user_id=1, home_tz="America/New_York", units="metric", log_level="INFO"
    )


def _client(db_path) -> TestClient:
    return TestClient(create_app(_settings(db_path), bind_host="127.0.0.1"))


# ---- the gate closes -------------------------------------------------------


def test_an_advice_page_is_blocked_until_the_notice_is_accepted(migrated_conn, db_path):
    migrated_conn.commit()
    client = _client(db_path)
    r = client.get(f"/?date={DAY}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/safety"


def test_the_json_api_is_blocked_too(migrated_conn, db_path):
    """A 303 is useless to a fetch() caller, so the API answers in its own language.

    This also closes the obvious bypass: if only the HTML pages were gated, the
    same numbers would still be one /api/ call away.
    """
    migrated_conn.commit()
    r = _client(db_path).get(f"/api/status?date={DAY}")
    assert r.status_code == 403
    assert r.json()["see"] == "/safety"


@pytest.mark.parametrize("path", ["/", "/plan", "/coach", "/ops", "/doctor", "/account"])
def test_every_advice_surface_is_gated_not_just_the_dashboard(migrated_conn, db_path, path):
    """Gating one page and forgetting the rest is the likely failure.

    This is why the check lives in the middleware: a route added tomorrow is
    covered by existing rather than by someone remembering.
    """
    migrated_conn.commit()
    r = _client(db_path).get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/safety"


# ---- the gate opens --------------------------------------------------------


def test_accepting_the_notice_opens_the_app(migrated_conn, db_path):
    migrated_conn.commit()
    client = _client(db_path)
    assert client.post("/safety", follow_redirects=False).status_code == 303
    assert client.get(f"/?date={DAY}").status_code == 200
    assert client.get(f"/api/status?date={DAY}").status_code == 200


def test_acceptance_is_recorded_against_the_user_and_version(migrated_conn, db_path):
    """A gate that forgets is a gate that asks forever, or one that can't answer
    'what did they agree to?'. Both are failures."""
    migrated_conn.commit()
    _client(db_path).post("/safety", follow_redirects=False)
    ack = D.latest(migrated_conn, user_id=1)
    assert ack is not None
    assert ack.version == DISCLAIMER_VERSION
    assert ack.user_agent  # the client identified itself; not a placeholder


def test_the_notice_is_not_shown_again_once_accepted(migrated_conn, db_path):
    migrated_conn.commit()
    client = _client(db_path)
    client.post("/safety", follow_redirects=False)
    body = client.get("/safety").text
    assert "I have read and understood this" not in body
    assert "Nothing further is needed" in body


def test_a_revised_notice_re_prompts(migrated_conn, db_path):
    """Consent is to a specific text. A bumped version must ask again."""
    D.acknowledge(migrated_conn, user_id=1, version=DISCLAIMER_VERSION - 1)
    migrated_conn.commit()
    r = _client(db_path).get(f"/?date={DAY}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/safety"


# ---- the gate is not a trap ------------------------------------------------


def test_you_can_sign_out_without_accepting(migrated_conn, db_path):
    """A gate you cannot retreat from is a trap.

    Someone who reads the notice and decides they do not want to agree must
    still be able to leave. Refusing consent cannot mean losing the session.
    """
    U.set_email(migrated_conn, user_id=1, email="owner@example.test")
    U.set_password(migrated_conn, user_id=1, password=OWNER_PW)
    migrated_conn.commit()
    client = _client(db_path)
    client.post("/login", data={"email": "owner@example.test", "password": OWNER_PW})
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_healthz_is_not_gated(migrated_conn, db_path):
    """A reverse proxy's health check is not a person and cannot consent.

    Gating it would take the host down on deploy, for a safety property that
    does not apply to it.
    """
    migrated_conn.commit()
    assert _client(db_path).get("/healthz").status_code == 200


def test_the_safety_page_renders_the_canonical_text(migrated_conn, db_path):
    migrated_conn.commit()
    body = _client(db_path).get("/safety").text
    assert "self-reported intake typically runs 20-40%" in body
    assert "disordered eating" in body


def test_the_short_notice_appears_on_every_signed_in_page(migrated_conn, db_path):
    """The footer is the part that is actually seen on an ordinary day."""
    migrated_conn.commit()
    client = _client(db_path)
    client.post("/safety", follow_redirects=False)
    for path in ("/", "/plan", "/coach"):
        assert SHORT in client.get(path).text, f"{path} lost the disclaimer footer"
