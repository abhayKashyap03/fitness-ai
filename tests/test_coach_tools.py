"""Coach tool-calling contract tests (T4.1).

Deterministic — no Anthropic call. Verifies structured output, explicit
provenance, and honest absence/insufficient markers (§2.2/§2.7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coach.adapters.healthkit.ingest import ingest_healthkit
from coach.coach import tools
from coach.normalize.runner import normalize_all

FIX = Path(__file__).parent / "fixtures" / "healthkit" / "export_sample.xml"
WEIGH_DAY = "2026-01-02"  # the fixture's body-record day


@pytest.fixture
def seeded_conn(migrated_conn):
    """Migrated DB with the fixture weight data ingested + normalized."""
    ingest_healthkit(migrated_conn, FIX)
    normalize_all(migrated_conn)
    return migrated_conn


def _json_roundtrips(obj) -> bool:
    return json.loads(json.dumps(obj)) == obj


# ---- registry / API surface ------------------------------------------------


def test_anthropic_tool_defs_shape():
    defs = tools.anthropic_tool_defs()
    assert {d["name"] for d in defs} == {
        "get_daily_status",
        "get_weight_trend",
        "get_recovery_history",
        "get_tdee_estimate",
        "get_safety_flags",
    }
    for d in defs:
        assert set(d) == {"name", "description", "input_schema"}  # no handler leaked
        assert d["input_schema"]["type"] == "object"


def test_dispatch_unknown_tool_raises(seeded_conn):
    with pytest.raises(KeyError, match="unknown tool"):
        tools.dispatch(seeded_conn, "get_everything", {})


def test_dispatch_routes_to_handler(seeded_conn):
    out = tools.dispatch(seeded_conn, "get_weight_trend", {"end": WEIGH_DAY, "window": 7})
    assert out["series"]  # non-empty


# ---- every tool returns JSON-serializable structured data ------------------


def test_all_tool_outputs_json_serializable(seeded_conn):
    outs = [
        tools.get_daily_status(seeded_conn, date=WEIGH_DAY),
        tools.get_weight_trend(seeded_conn, end=WEIGH_DAY, window=7),
        tools.get_recovery_history(seeded_conn, end=WEIGH_DAY, window=7),
        tools.get_tdee_estimate(seeded_conn, end=WEIGH_DAY, window=14),
    ]
    for o in outs:
        assert isinstance(o, dict)
        assert _json_roundtrips(o)


# ---- provenance + real numbers ---------------------------------------------


def test_weight_trend_carries_provenance(seeded_conn):
    out = tools.get_weight_trend(seeded_conn, end=WEIGH_DAY, window=7)
    pt = out["series"][-1]
    assert pt["source"] == "healthkit"
    assert pt["source_app"] == "okok"  # scale wins over MFP-mirrored (D3)
    assert out["latest_trend_kg"] is not None
    assert out["insufficient"] is None


def test_daily_status_weight_present(seeded_conn):
    out = tools.get_daily_status(seeded_conn, date=WEIGH_DAY)
    assert out["weight"]["source"] == "healthkit"
    assert out["weight"]["weight_kg"] is not None


# ---- honest absence (§2.7) -------------------------------------------------


def test_daily_status_food_not_logged_is_not_zero(seeded_conn):
    out = tools.get_daily_status(seeded_conn, date=WEIGH_DAY)
    food = out["food"]
    assert food["logged"] is False
    assert food["kcal"] is None  # NOT 0
    assert food["is_fast"] is False


def test_weight_trend_empty_window_is_insufficient(seeded_conn):
    out = tools.get_weight_trend(seeded_conn, end="2000-01-01", window=7)
    assert out["series"] == []
    assert out["latest_trend_kg"] is None
    assert out["insufficient"] == {"have": 0, "needed": 1}


def test_recovery_history_empty_is_insufficient(seeded_conn):
    out = tools.get_recovery_history(seeded_conn, end=WEIGH_DAY, window=7)
    assert out["series"] == []
    assert out["insufficient"] == {"have": 0, "needed": 1}


def test_tdee_insufficient_returns_null_estimate_not_a_number(seeded_conn):
    out = tools.get_tdee_estimate(seeded_conn, end=WEIGH_DAY, window=14)
    assert out["estimate"] is None
    assert out["insufficient"]["needed"] == 10  # no intake logged


# ---- §8.6 safety flags tool ------------------------------------------------


def test_safety_flags_insufficient_with_single_weigh_in(seeded_conn):
    # fixture has one weigh-in day -> not enough trend to judge; no false alarm
    out = tools.get_safety_flags(seeded_conn, end=WEIGH_DAY, window=30)
    assert out["alerts"] == []
    assert out["insufficient"] == {"have": 1, "needed": 2}


def _seed_declining_series(conn, *, days, start_kg, per_day):
    from datetime import date, timedelta

    d0 = date(2026, 3, 1)
    for i in range(days):
        day = (d0 + timedelta(days=i)).isoformat()
        conn.execute(
            "INSERT INTO weight_measurement (id, user_id, day_key, source, source_app, "
            "weight_kg, raw_ref, derived_at) VALUES (?,1,?,?,?,?,?,?)",
            (f"wt:test:{day}", day, "manual", None, start_kg - per_day * i, None,
             "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    return (d0 + timedelta(days=days - 1)).isoformat()


def test_safety_flags_surfaces_unsafe_loss(migrated_conn):
    # ~0.25 kg/day settled decline (~1.9%/wk on ~90 kg) -> above the hard limit
    end = _seed_declining_series(migrated_conn, days=40, start_kg=100.0, per_day=0.25)
    out = tools.get_safety_flags(migrated_conn, end=end, window=14)
    assert out["insufficient"] is None
    assert len(out["alerts"]) == 1
    assert out["alerts"][0]["code"] in {"fast_weight_loss", "rapid_weight_loss"}


def test_safety_flags_quiet_on_maintenance(migrated_conn):
    # flat weight -> no alert, no false alarm
    end = _seed_declining_series(migrated_conn, days=40, start_kg=90.0, per_day=0.0)
    out = tools.get_safety_flags(migrated_conn, end=end, window=14)
    assert out["alerts"] == []
    assert out["insufficient"] is None
