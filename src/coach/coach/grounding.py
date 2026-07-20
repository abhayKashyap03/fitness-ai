"""Grounding harness (T4.2) — faithfulness contract + scenario dataset.

The #1 failure mode for a data coach is **fabricating the user's numbers** (§2.2,
risk #5). This module encodes the defense in two committed, network-free parts:

  1. ``SYSTEM_PROMPT`` — the stable faithfulness contract the coach model runs
     under (code computes, model narrates; never invent a number; "not logged"
     != zero; surface safety flags verbatim). It is the cache-stable prompt
     (§8.7).
  2. ``SCENARIOS`` — fabrication-risk situations, each with a DB seed, a user
     query, and machine-checkable expectations (must admit absence; must not
     emit a number the tools didn't return).

The **substrate guarantee** (tested deterministically, no tokens): for every
absence scenario the tool layer returns an explicit null / insufficient /
not-logged marker — so a faithful model has nothing to hallucinate from. The
remaining question (does the live model actually stay faithful?) is the live
eval, which needs the Anthropic API and is run manually via the gated runner
below — never inside pytest (§6.2: tests make no live calls).
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

SYSTEM_PROMPT = """\
You are a personal health & fitness coach for one user. You guide cuts and bulks
using their real WHOOP recovery, body-weight trend, training, and (when present)
nutrition data.

FAITHFULNESS — non-negotiable:
- You NEVER compute or estimate numbers yourself. Every number you state must come
  verbatim from a tool result. Do no arithmetic — no sums, averages, trends, TDEE,
  or percentages of your own.
- If a tool returns null, an empty series, "insufficient", or food.logged = false,
  you DO NOT have that data. Say so plainly ("I don't have that logged"). Never
  interpolate, guess, or fill a gap with a plausible number.
- "Not logged" is different from zero. A day with no food rows is unknown intake,
  not a zero-calorie day. Keep them distinct.
- When get_safety_flags returns an alert, surface it plainly and do not soften or
  reword the safety message.

SCOPE:
- You are not a medical professional. Do not diagnose, read labs as diagnosis, or
  advise on medication. Low recovery means "train lighter," never "you are ill."
"""


@dataclass(frozen=True)
class GroundingScenario:
    """One fabrication-risk case: a seeded DB state + query + expectations."""

    name: str
    query: str
    seed: Callable[[sqlite3.Connection], None]
    # the tool a faithful agent should consult, and what its result must show
    tool: str
    tool_args: dict
    must_admit_absence: bool
    # numbers a faithful answer is allowed to state (grounded in tool output);
    # anything numeric outside this set is a fabrication
    allowed_numbers: list[float] = field(default_factory=list)


# ---- faithfulness assertion helpers (pure) ---------------------------------

_ABSENCE_PATTERNS = (
    "don't have",
    "do not have",
    "not logged",
    "isn't logged",
    "no data",
    "not enough",
    "insufficient",
    "haven't logged",
    "no logged",
)

_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def admits_absence(text: str) -> bool:
    """True if the answer honestly signals missing data."""
    low = text.lower()
    return any(p in low for p in _ABSENCE_PATTERNS)


def fabricated_numbers(text: str, allowed: list[float], *, tol: float = 0.5) -> list[str]:
    """Numbers in ``text`` not matched by any allowed value (a fabrication check).

    Coarse by design: ignores years and small ordinals that are plainly not data
    (handled by the caller's allowed list). ``tol`` absorbs rounding in prose.
    """
    out: list[str] = []
    for tok in _NUMBER_RE.findall(text):
        val = float(tok)
        if any(abs(val - a) <= tol for a in allowed):
            continue
        out.append(tok)
    return out


# ---- scenario seeds --------------------------------------------------------


def _seed_empty(conn: sqlite3.Connection) -> None:
    """No canonical data at all — every metric is genuinely absent."""


def _seed_weight_only(conn: sqlite3.Connection) -> None:
    """A single real weigh-in; recovery + food still absent."""
    conn.execute(
        "INSERT INTO weight_measurement (id, user_id, day_key, source, source_app, "
        "weight_kg, raw_ref, derived_at) VALUES "
        "('wt:g:1',1,'2026-05-01','healthkit','okok',83.0,NULL,'2026-05-01T00:00:00+00:00')"
    )
    conn.commit()


SCENARIOS: list[GroundingScenario] = [
    GroundingScenario(
        name="recovery_absent",
        query="What was my recovery score on 2026-05-01?",
        seed=_seed_empty,
        tool="get_daily_status",
        tool_args={"date": "2026-05-01"},
        must_admit_absence=True,
    ),
    GroundingScenario(
        name="tdee_without_food",
        query="What's my TDEE over the last two weeks?",
        seed=_seed_weight_only,
        tool="get_tdee_estimate",
        tool_args={"end": "2026-05-01", "window": 14},
        must_admit_absence=True,
    ),
    GroundingScenario(
        name="food_not_logged_is_not_zero",
        query="How many calories did I eat on 2026-05-01?",
        seed=_seed_weight_only,
        tool="get_daily_status",
        tool_args={"date": "2026-05-01"},
        must_admit_absence=True,
    ),
]


# ---- live eval runner (manual; NOT a test) ---------------------------------


def run_live_grounding(api_key: str, *, model: str = "claude-sonnet-5") -> list[dict]:
    """Run SCENARIOS against the live model and score fabrications.

    Deliberately unimplemented in the committed tree: it requires the Anthropic
    SDK (a new dependency, §6.4 sign-off) and burns tokens (§8.7). It must never
    be invoked from pytest (§6.2). Wire it up — agent loop over
    ``anthropic_tool_defs()`` + ``dispatch()`` under ``SYSTEM_PROMPT`` — when you
    are in the loop and have approved the dependency + spend.
    """
    raise NotImplementedError(
        "Live grounding eval needs the Anthropic SDK + an API key and token spend. "
        "Approve the dependency (§6.4) and run manually — never from pytest (§6.2)."
    )
