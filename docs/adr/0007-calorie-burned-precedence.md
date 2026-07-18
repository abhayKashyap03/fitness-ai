# ADR-0007 — Calories-burned source precedence (D2)

Status: Accepted · Date: 2026-07-18 · Implements CLAUDE.md §9 D2 · relates to T3.4

## Context

Multiple sources report "calories out" for overlapping activity (WHOOP vs Apple
Watch vs gym equipment) and disagree 30%+. A one-way-door question: which wins,
and does any of it drive the coach's decisions?

## Decision

**Wearable "calories out" never drives anything important.**

- **Adaptive TDEE** (ADR-0005) stays on **weight-trend + logged intake**. Wearable
  calorie estimates are kept in canonical (`workout.kcal_*`) for comparison only,
  never as a TDEE input.
- **On conflict, display the range — don't pick a winner.** Fake precision is
  worse than an honest range, especially once the coach reasons on top of it.
- For a single per-workout display number, the ranking is **strap > wrist >
  machine** (WHOOP chest-adjacent optical + skin temp is steadier than wrist
  optical, which beats machine estimates), always **labeled approximate**. This
  is a *display* preference, not a computation input.

## Consequences

- Today only WHOOP produces calories, so there is no live conflict. `daily.py`
  counts each workout once per `session_group_id`, choosing a representative by a
  documented source rank — the same precedence this ADR formalizes for when a
  second source lands.
- The coaching layer (Phase 4) must present calories-out as a range when sources
  disagree, never a single authoritative figure.
- Keeps the trustworthy signal (weight trend + intake) insulated from the noisiest
  input, consistent with §8 (nutrition/again-wearable garbage-in).
