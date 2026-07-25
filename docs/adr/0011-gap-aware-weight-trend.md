# ADR-0011 — Gap-aware EWMA weight trend

**Status:** Accepted (2026-07-25) · shipped in migration 0008

## Context

The weight trend (EWMA, alpha 0.10) is the signal the whole cut/bulk loop
steers by: adaptive TDEE reads its delta, the §8.6 guardrails read its slope.
The 0005/0007 `weight_trend` view smoothed **per row** — every weigh-in moved
the trend 10%, whether it arrived one day or three weeks after the previous
one.

The user travels constantly (§2.6) and weigh-ins gap accordingly (risk #3:
async, partial data). After a 14-day gap the stale trend kept 90% of its
weight, so a real 2 kg change registered as ~0.2 kg — the TDEE estimate and the
safety guardrails then consumed that lag as truth for weeks.

## Decision

Scale the smoothing to the **calendar, not the row count**: for a gap of `g`
days, the effective alpha is `1 − (1−α)^g`.

This is not a new smoothing model — it is algebraically identical to running
the plain daily EWMA `g` times toward the new observation. Consequences:

- **Evenly-sampled history is unchanged** (`g=1` → plain alpha). No trend value
  shifts for data that never had gaps; there is no discontinuity to explain.
- A 14-day gap yields `eff ≈ 0.77` — the new reading pulls hard, as it should
  after two silent weeks.
- **No interpolation.** Missing days produce no rows (§2.7 absence is absence);
  only the *decay* sees elapsed time.

Implementation is dual, cross-validated:

1. **Ground truth:** `compute/trends.py::gap_aware_ewma` — pure Python, unit
   tested against hand-computed values (§2.2).
2. **The serving path:** migration 0008 recreates the `weight_trend` view with
   `1 − POWER(0.90, JULIANDAY(cur) − JULIANDAY(prev))`, so every existing
   reader (`compute/daily`, `compute/tdee`, `tools.get_weight_trend`,
   guardrails) becomes gap-aware with zero code change.
3. **A test asserts the view reproduces the Python series exactly** on gapped
   data — the view is code, and it is held to the compute layer's standard.

## Alternatives rejected

- **Interpolate missing days, then plain EWMA.** Invents data (§2.2 “never
  interpolate”), breaks §2.7. Rejected outright.
- **Python-only trend, retire the view.** Touches every reader and moves a
  working query surface for no correctness gain over the dual approach.
- **Time-constant model (`eff = 1 − e^(−g/τ)`).** Equivalent shape, but the
  `(1−α)^g` form is *exactly* the composition of the existing daily model, so
  old and new agree perfectly on ungapped data — the least surprising choice.

## Consequences

- `weight_trend` now depends on SQLite math functions (`POWER`). These ship in
  CPython's bundled SQLite on the supported platforms; the cross-validation
  test fails loudly on any build without them.
- Guardrails judge loss-rate over gapped windows more honestly: a fortnight
  gap no longer masks a rapid loss as a gentle one (the §8.6 alert fires from
  a truer slope).
