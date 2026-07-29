# ADR-0013 — Cut/bulk plan target model: rate is canonical

## Status
Accepted 2026-07-26 (resolves DECISIONS_NEEDED D5). Gates Phase 7.

## Context
The plan layer (Phase 7) turns the observation engine into a coach that *steers*.
Its foundational one-way door is **how a target is expressed**, because that
choice defines the `plan` schema and every downstream number (daily calorie goal,
projected timeline, on/off-track delta). Two natural framings:

- **Goal-weight + deadline** ("83 → 78 kg by Oct 1"). Intuitive; but a deadline
  can imply a physiologically unsafe rate that the §8.6 loss-rate ceiling must
  then override — the deadline can't be honored, so it becomes a lie.
- **%/week rate** ("lose 0.5%/week"). Safe by construction (rate is the direct,
  bounded lever); less intuitive; no fixed end date.

## Decision
**Option C — accept either input; store the RATE as the one canonical quantity.**

- The canonical stored driver is `target_rate_pct_per_week` (signed: negative =
  loss, positive = gain, 0 = maintain).
- A **deadline entry is convenience only**: from the current trend weight it
  computes an *initial* rate, which is **immediately clamped** to the §8.6
  sustainable-loss ceiling and then stored as the rate. The deadline itself is
  **not** persisted as a driver — the (clamped) rate is the truth.
- `goal_weight_kg` is stored as an optional endpoint for progress/projection, but
  it does not drive the daily calorie goal — the rate does.
- The daily calorie goal derives from adaptive TDEE + the rate's energy delta
  (`KCAL_PER_KG = 7700`, shared with `compute/tdee.py`), then passes through the
  §8.6 calorie-floor clamp — the floor always wins, even for a safe rate on a low
  TDEE.

## Consequences
- **Safety is structural, not advisory.** The unsafe-rate failure mode of a
  deadline is clamped away at entry; the calorie floor is re-checked at compute.
  Guardrails (`compute/guardrails.py`) stay the single source of hard limits
  (§8.6), never the prompt.
- **No fake precision.** A deadline the floor can't honor stretches the projected
  timeline honestly instead of promising a date. **This is enforced in
  `plan_status` via `effective_rate_kg_per_week`** — when the calorie floor
  binds, the achievable rate is derived from the CLAMPED goal, and the timeline
  and adherence label are both judged against that. (Found live 2026-07-28: the
  projection was using the target rate, so it promised exactly the date the
  floor forbade — the opposite of what this ADR states.)
- **One stored quantity, either entry style** — both `--rate` and
  `--goal-weight --by` reduce to the same clamped rate; no dual code paths past
  the CLI boundary.
- **Reversibility.** Had we stored a deadline as the driver and later moved to
  rate, that's a schema + recompute migration. Storing rate now avoids it.
- Bulk (gain) rate is **not** clamped here — §8.6 specifies a loss ceiling and a
  calorie floor only; an over-aggressive bulk is out of the current guardrail
  scope and left to the user (documented, not silently unbounded).
- **Mid-cut adoption.** The forward-looking numbers (daily goal, ETA) key off the
  *current* trend, so they're correct the moment a plan is set. But a user who
  already started must be able to **backdate the start anchor** (`plan set
  --start-date/--start-weight`) or "progress so far" and adherence are
  meaningless. `plan_status` therefore computes progress (kg changed, actual
  rate vs target → an `adherence` label) only when a start weight and a positive
  elapsed span exist; a same-day plan simply shows no progress yet (§2.7, not a
  fabricated zero).
