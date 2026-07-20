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
