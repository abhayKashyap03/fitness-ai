"""Local web dashboard (ADR-0014) — routes, honesty, and the safety default.

The web layer is a presentation boundary: these tests assert it returns exactly
what the tool layer returns (no reshaping, no invented numbers) and that absence
renders as absence (§2.7). No network — the LLM route is never exercised live.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="web UI needs the optional [web] extra")

from fastapi.testclient import TestClient

from coach.store.plan import PlanRow, insert_plan, plan_id
from coach.web.app import create_app

DAY = "2026-03-15"


@pytest.fixture
def client(migrated_conn, db_path, monkeypatch):
    """App wired to the migrated temp DB (settings injected — never the real one)."""
    from coach.config import Settings

    settings = Settings(
        db_path=db_path,
        user_id=1,
        home_tz="America/New_York",
        units="metric",
        log_level="INFO",
    )
    migrated_conn.commit()
    return TestClient(create_app(settings))


# ---- JSON API ---------------------------------------------------------------


def test_api_status_matches_the_tool_layer(client, migrated_conn):
    """The API must be a pass-through: same numbers the CLI and LLM see."""
    from coach.coach.tools import get_daily_status

    expected = get_daily_status(migrated_conn, date=DAY, user_id=1)
    got = client.get(f"/api/status?date={DAY}").json()
    assert got == expected


def test_api_endpoints_respond_on_an_empty_db(client):
    # Every surface must degrade honestly rather than 500 on no data.
    for path in (
        f"/api/status?date={DAY}",
        f"/api/tdee?end={DAY}",
        f"/api/weight-trend?end={DAY}",
        f"/api/recovery?end={DAY}",
        f"/api/sleep?end={DAY}",
        f"/api/plan?end={DAY}",
        f"/api/safety?end={DAY}",
    ):
        assert client.get(path).status_code == 200, path


def test_api_tdee_reports_insufficient_not_a_number(client):
    body = client.get(f"/api/tdee?end={DAY}").json()
    assert body["estimate"] is None
    assert body["insufficient"]["needed"] == 10


def test_api_plan_is_null_when_none_set(client):
    body = client.get(f"/api/plan?end={DAY}").json()
    assert body["plan"] is None and body["status"] is None
    # protein is part of the contract even with no plan, so the iOS client (P13)
    # can rely on the key existing rather than probing for it
    assert body["protein"] is None


def test_api_rejects_a_bad_window(client):
    assert client.get(f"/api/tdee?end={DAY}&window=0").status_code == 422


def test_api_ask_requires_a_question(client):
    assert client.post("/api/ask", json={"question": "  "}).status_code == 422


def test_api_ask_reports_missing_credentials_as_unavailable(client):
    # No API key in the injected settings: a config problem (503), not a crash.
    r = client.post("/api/ask", json={"question": "how am I doing?"})
    assert r.status_code == 503
    assert "GOOGLE_API_KEY" in r.json()["detail"]


# ---- pages ------------------------------------------------------------------


def test_dashboard_renders_and_says_not_logged(client):
    r = client.get(f"/?date={DAY}")
    assert r.status_code == 200
    # "not logged" must never be rendered as a zero (§2.7).
    assert "NOT LOGGED" in r.text
    assert "insufficient data" in r.text


def test_dashboard_day_navigation(client):
    r = client.get(f"/?date={DAY}")
    assert "/?date=2026-03-14" in r.text and "/?date=2026-03-16" in r.text


def test_plan_page_renders_without_a_plan(client):
    r = client.get("/plan")
    assert r.status_code == 200 and "none set" in r.text


def test_coach_page_renders(client):
    assert client.get("/coach").status_code == 200


# ---- plan form --------------------------------------------------------------


def test_post_plan_sets_a_plan(client, migrated_conn):
    r = client.post("/plan", data={"mode": "rate", "rate": "-0.5"}, follow_redirects=False)
    assert r.status_code == 303
    row = migrated_conn.execute(
        "SELECT direction, target_rate_pct_per_week FROM plan WHERE is_active = 1"
    ).fetchone()
    assert row["direction"] == "cut"
    assert row["target_rate_pct_per_week"] == pytest.approx(-0.5)


def test_post_plan_clamps_an_unsafe_rate(client, migrated_conn):
    """The §8.6 ceiling is enforced by compute — the form cannot bypass it."""
    from coach.compute.guardrails import MAX_TARGET_LOSS_PCT_PER_WEEK

    client.post("/plan", data={"mode": "rate", "rate": "-3.0"}, follow_redirects=False)
    row = migrated_conn.execute(
        "SELECT target_rate_pct_per_week FROM plan WHERE is_active = 1"
    ).fetchone()
    assert row["target_rate_pct_per_week"] == pytest.approx(-MAX_TARGET_LOSS_PCT_PER_WEEK)


def test_post_plan_rejects_an_empty_submission(client):
    r = client.post("/plan", data={"mode": "rate"}, follow_redirects=False)
    assert r.status_code == 400
    assert "Specify a rate" in r.text


def test_post_plan_deadline_without_a_trend_is_refused(client):
    # No weight data -> a deadline can't be converted to a rate. Say so, don't guess.
    r = client.post(
        "/plan",
        data={"mode": "deadline", "goal_weight": "78", "by": "2099-01-01"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "weight trend" in r.text


def test_dashboard_shows_an_active_plan(client, migrated_conn):
    created = "2026-03-01T00:00:00+00:00"
    insert_plan(
        migrated_conn,
        PlanRow(
            id=plan_id(1, created),
            user_id=1,
            created_at=created,
            start_day_key="2026-03-01",
            direction="cut",
            target_rate_pct_per_week=-0.5,
            start_weight_kg=80.0,
            goal_weight_kg=76.0,
        ),
    )
    migrated_conn.commit()
    r = client.get(f"/?date={DAY}")
    assert "cut" in r.text
    # No TDEE yet, so the goal must be reported as insufficient, never fabricated.
    assert "insufficient data" in r.text
