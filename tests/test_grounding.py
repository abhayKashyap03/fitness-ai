"""Grounding harness — deterministic substrate + helper tests (T4.2).

No network, no tokens (§6.2). Proves that for every fabrication-risk scenario the
tool layer hands the model an explicit absence marker, so a faithful coach has
nothing to hallucinate from. The live-model faithfulness eval is manual and gated
(see grounding.run_live_grounding).
"""

from __future__ import annotations

import pytest

from coach.coach import tools
from coach.coach.grounding import (
    SCENARIOS,
    SYSTEM_PROMPT,
    admits_absence,
    fabricated_numbers,
    run_live_grounding,
)

# per-scenario predicate: is the metric the query asks about honestly ABSENT
# in the tool output the model would ground on?
_HONEST_ABSENCE = {
    "recovery_absent": lambda o: o["recovery"] is None,
    "tdee_without_food": lambda o: o["estimate"] is None and o["insufficient"] is not None,
    "food_not_logged_is_not_zero": lambda o: o["food"]["logged"] is False
    and o["food"]["kcal"] is None,
}


# ---- assertion helpers -----------------------------------------------------


def test_admits_absence_detects_honest_phrasing():
    assert admits_absence("I don't have that logged for you.")
    assert admits_absence("Not enough data to estimate TDEE yet.")
    assert not admits_absence("Your recovery was 62 and trending up.")


def test_fabricated_numbers_flags_ungrounded_values():
    # an invented HRV of 45 with nothing grounded -> flagged
    assert fabricated_numbers("Your HRV was 45 ms.", allowed=[]) == ["45"]
    # a grounded value passes (within rounding tolerance)
    assert fabricated_numbers("Your weight trend is 83.0 kg.", allowed=[83.0]) == []


def test_system_prompt_keeps_faithfulness_clauses():
    # guards against silently weakening the contract
    low = SYSTEM_PROMPT.lower()
    assert "never" in low
    assert "not logged" in low
    assert "not a medical" in low


# ---- substrate guarantee: tools expose honest absence ----------------------


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_scenarios_are_well_formed(scenario):
    assert scenario.tool in {t.name for t in tools.TOOLS}
    assert scenario.name in _HONEST_ABSENCE  # every scenario has a checker


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_tool_substrate_is_honestly_absent(migrated_conn, scenario):
    scenario.seed(migrated_conn)
    out = tools.dispatch(migrated_conn, scenario.tool, scenario.tool_args)
    assert _HONEST_ABSENCE[scenario.name](out), f"{scenario.name}: tool masked the absence"


# ---- live eval is gated, never auto-run ------------------------------------


def test_live_runner_is_gated():
    with pytest.raises(NotImplementedError):
        run_live_grounding("fake-key")
