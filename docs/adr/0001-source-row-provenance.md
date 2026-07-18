# ADR-0001 — Sibling source rows + read-time resolution

Status: Accepted · Date: 2026-07-18

## Context

Every real-world fact in this system (a morning's recovery, a workout, a day's
intake, a body weight) can arrive from more than one source, and the sources
disagree — WHOOP vs Apple Watch vs gym-machine calories can differ 30%+, two
scales differ by 0.5 kg, WHOOP's proprietary recovery score is a different
*number* than the recovery metric we recompute from raw HRV.

We had two ways to store this:

1. **Merge on write** — decide the "true" value at ingest, keep one row per fact.
2. **Sibling rows** — keep one row per (fact, source), decide which wins when we
   *read*, via a view.

This is a one-way door: merge-on-write destroys the losing value permanently, so
you cannot change your mind later without re-fetching (often impossible — the
membership lapsed, the API is gone, the day passed).

## Decision

**Store sibling rows. Resolve at read time. Never merge or overwrite on write.**

Concretely:

- Each canonical table carries a `source` column. Multiple sources for the same
  `(user_id, day_key)` coexist as separate rows.
- A **view** (`recovery_resolved`, `food_daily`, `weight_resolved_daily`) applies
  a **precedence rule** to pick the authoritative row per day. Precedence lives
  in exactly one `CASE` expression per view — an obvious, greppable, documented
  place.
- Swapping which source wins is a one-line reorder of that `CASE`. No data
  migration, no schema change, no gap in the series.

We also **separate objective measurements from derived composite scores** in
`recovery`:

- Objective physical quantities (`hrv_rmssd_ms`, `resting_hr_bpm`, `spo2_pct`,
  `skin_temp_c`, `resp_rate_bpm`) are **comparable across sources**. They are the
  honest calibration currency: WHOOP's HRV and our BLE-computed HRV measure the
  same physical thing.
- The composite `score` is **not** comparable across sources, because WHOOP's
  weighting is proprietary and ≠ our textbook formula. It is tagged with
  `score_method` + `is_official` and never silently compared to a foreign score.

## Why this beats merge-on-write

- **The WHOOP calibration play depends on it.** During the paid window we run the
  official API (Adapter A) and the local BLE recompute (Adapter B) in parallel,
  both writing sibling rows. Official scores are ground truth for tuning our own
  metric. When the membership ends we flip precedence — the objective series has
  no seam. Merge-on-write would have thrown away exactly the comparison we need.
- **Canonical is regenerable (§2.1).** Because we never destroy a source's value,
  we can re-derive better numbers from raw at any time and backfill history.
- **Provenance is first-class (§2.3).** "Where did this number come from?" is
  always answerable — the winning row names its `source` and `raw_ref`.

## Consequences

- More rows, and a `GROUP BY` / `ROW_NUMBER()` cost at read time. At n=1 this is
  free; at scale it is a materialized-view problem, not an architecture problem.
- Aggregations must be **source-aware** to avoid double-counting the same real
  fact logged twice (one run via Watch + Strava + WHOOP = 3× calories if summed
  naively). Handled by `session_group_id` for workouts and by resolving to one
  source per day for food. This is a known, tested bug class, not an accident.
- Precedence is a policy decision that will change (post-membership adapter swap).
  Keeping it in a single documented `CASE` per view makes that change trivial and
  auditable.
