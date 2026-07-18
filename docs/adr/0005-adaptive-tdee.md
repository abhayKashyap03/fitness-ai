# ADR-0005 — Adaptive TDEE from intake + weight trend

Status: Accepted · Date: 2026-07-18

## Context

We need daily expenditure (TDEE) to steer cuts/bulks. Wearable calorie estimates
are unreliable (§8) and MacroFactor deliberately ignores them. The trustworthy
signal is the energy-balance identity over time.

## Decision

Estimate TDEE from **mean logged intake** and the **change in smoothed (EWMA)
weight trend** over a rolling window:

```
TDEE = mean_intake - (Δ trend_weight_kg * KCAL_PER_KG) / span_days
```

- `KCAL_PER_KG = 7700` — standard mixed-tissue approximation.
- Weight uses the **EWMA trend**, not raw scale weight, so water/glycogen noise
  doesn't corrupt the estimate.
- Wearable "calories out" is kept in canonical (`workout.kcal_*`) for comparison
  but is **not** an input to this estimate.

**Graceful degradation:** returns `Insufficient` (not a number) unless there are
≥ `min_intake_days` logged-intake days (default 10) and a weight trend at both
ends of a non-zero calendar span.

## Validation

`tests/test_tdee.py` builds a synthetic dataset with a known true TDEE (intake at
a fixed deficit/surplus, trend weight moving exactly with the balance) and
asserts the estimate recovers it **within ±25 kcal (~1%)** on a clean linear
trend. Direction is checked both ways (deficit → TDEE > intake; surplus → TDEE <
intake).

## Consequences

- Accuracy depends on logging honesty (§8 nutrition garbage-in) and a stable
  weigh-in cadence. The window smooths both but cannot fix a systematically
  biased food log.
- `KCAL_PER_KG` and the window/min-days are parameters, tunable as real data
  accumulates. The default 7700 is a population constant, not this user's
  measured value — revisit once we have enough paired intake/trend history.
- This is the *primary* expenditure number for the coach; the per-workout
  wearable kcal stays advisory.
