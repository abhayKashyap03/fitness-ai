"""One plan-set seam for every surface (services/plan.py).

Regression: the CLI and the web form each carried their own copy of this logic
and drifted — the CLI recorded a coaching note on every plan change, the web form
silently did not. A plan set in the browser therefore left no trace in memory and
later looked as if it had changed by itself.
"""

from __future__ import annotations

import pytest

from coach.compute.guardrails import MAX_TARGET_LOSS_PCT_PER_WEEK
from coach.services.plan import PlanInputError, set_active_plan
from coach.store.notes import SYSTEM, recent_notes
from coach.store.plan import active_plan

TODAY = "2026-07-28"


def _seed_weight(conn, day="2026-07-20", kg=83.5):
    conn.execute(
        "INSERT INTO weight_measurement (id, user_id, day_key, source, source_app, "
        "weight_kg, raw_ref, derived_at) VALUES (?,1,?,'manual',NULL,?,NULL,?)",
        (f"wt:{day}", day, kg, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()


def test_setting_a_plan_always_records_a_note(migrated_conn):
    """The note is the audit trail; no surface may skip it."""
    set_active_plan(migrated_conn, today=TODAY, rate=-0.5, goal_weight=78.0)
    migrated_conn.commit()

    plan = active_plan(migrated_conn)
    assert plan is not None and plan.target_rate_pct_per_week == pytest.approx(-0.5)

    (note,) = recent_notes(migrated_conn)
    assert note.author == SYSTEM
    assert note.kind == "plan"
    assert "-0.50%/week" in note.text
    assert "78.0kg" in note.text


def test_the_ceiling_clamp_is_not_bypassable(migrated_conn):
    result = set_active_plan(migrated_conn, today=TODAY, rate=-3.0)
    migrated_conn.commit()
    assert result.clamped is True
    plan = active_plan(migrated_conn)
    assert plan.target_rate_pct_per_week == pytest.approx(-MAX_TARGET_LOSS_PCT_PER_WEEK)
    # and the note says so, so the clamp is visible in history
    assert "clamped" in recent_notes(migrated_conn)[0].text


def test_maintain(migrated_conn):
    set_active_plan(migrated_conn, today=TODAY, maintain=True)
    migrated_conn.commit()
    assert active_plan(migrated_conn).direction == "maintain"


def test_deadline_converts_to_a_rate(migrated_conn):
    _seed_weight(migrated_conn, kg=80.0)
    set_active_plan(migrated_conn, today=TODAY, goal_weight=76.0, by="2026-10-06")
    migrated_conn.commit()
    plan = active_plan(migrated_conn)
    assert plan.direction == "cut"
    assert plan.target_rate_pct_per_week < 0


def test_deadline_without_a_trend_is_refused(migrated_conn):
    with pytest.raises(PlanInputError, match="weight trend"):
        set_active_plan(migrated_conn, today=TODAY, goal_weight=76.0, by="2099-01-01")


def test_past_deadline_is_refused(migrated_conn):
    _seed_weight(migrated_conn, kg=80.0)
    with pytest.raises(PlanInputError, match="future date"):
        set_active_plan(migrated_conn, today=TODAY, goal_weight=76.0, by="2026-01-01")


def test_no_entry_style_is_refused(migrated_conn):
    with pytest.raises(PlanInputError, match="Specify a rate"):
        set_active_plan(migrated_conn, today=TODAY)


def test_backdated_start_anchors_from_the_trend_at_that_date(migrated_conn):
    _seed_weight(migrated_conn, day="2026-07-20", kg=83.5)
    set_active_plan(migrated_conn, today=TODAY, rate=-0.5, start_date="2026-07-20")
    migrated_conn.commit()
    plan = active_plan(migrated_conn)
    assert plan.start_day_key == "2026-07-20"
    assert plan.start_weight_kg == pytest.approx(83.5)


def test_backdate_without_a_trend_is_refused_rather_than_guessed(migrated_conn):
    with pytest.raises(PlanInputError, match="start weight"):
        set_active_plan(migrated_conn, today=TODAY, rate=-0.5, start_date="2020-01-01")


def test_web_form_and_cli_produce_the_same_result(migrated_conn, db_path, acknowledged):
    """The two surfaces must be indistinguishable — that is the whole point."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from coach.config import Settings
    from coach.web.app import create_app

    migrated_conn.commit()
    cfg = Settings(
        db_path=db_path,
        user_id=1,
        home_tz="America/New_York",
        units="metric",
        log_level="INFO",
    )
    client = TestClient(create_app(cfg))
    client.post("/plan", data={"mode": "rate", "rate": "-0.5"}, follow_redirects=False)

    plan = active_plan(migrated_conn)
    assert plan.target_rate_pct_per_week == pytest.approx(-0.5)
    # the note the CLI writes must also exist when the WEB form is used
    notes = recent_notes(migrated_conn)
    assert notes and notes[0].kind == "plan" and notes[0].author == SYSTEM
