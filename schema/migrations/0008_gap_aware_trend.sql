-- ============================================================
--  Migration 0008 — gap-aware EWMA weight trend (ADR-0011)
--
--  PROBLEM: the 0005/0007 weight_trend smoothed per ROW (alpha 0.10 per
--  weigh-in), ignoring the calendar. After a 14-day travel gap the stale trend
--  kept 90% of its weight, so the trend lagged reality for weeks — and the
--  adaptive TDEE + §8.6 guardrails read that lag as truth (risk #3).
--
--  FIX: effective alpha for a gap of g days = 1 - (1-0.10)^g. Algebraically
--  identical to running the daily EWMA g times toward the new value, so
--  evenly-sampled history is byte-identical to the old view (g=1 -> 0.10) and
--  only gapped stretches change. No interpolation — missing days stay missing
--  (§2.7); only the DECAY sees elapsed time.
--
--  Views hold no data; recreation is safe and this needs no table rebuild.
--  Ground truth lives in compute/trends.py::gap_aware_ewma — a test asserts
--  this view matches the pure-Python series exactly (§2.2: code computes; the
--  view IS code, cross-validated).
--
--  NOTE: uses SQLite math functions (POWER, JULIANDAY is core). Math functions
--  ship enabled in CPython's bundled SQLite on this project's supported
--  platforms; the cross-validation test fails loudly on any build without them.
-- ============================================================

DROP VIEW IF EXISTS weight_trend;

CREATE VIEW weight_trend AS
WITH RECURSIVE
ordered AS (
  SELECT user_id, day_key, weight_kg,
         ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY day_key) AS rn
  FROM weight_resolved_daily
),
ewma(user_id, day_key, weight_kg, rn, trend_kg) AS (
  SELECT user_id, day_key, weight_kg, rn, weight_kg
  FROM ordered WHERE rn = 1
  UNION ALL
  SELECT o.user_id, o.day_key, o.weight_kg, o.rn,
         -- gap-aware: eff = 1 - 0.90^gap_days; gap>=1 by construction
         (1 - POWER(0.90, JULIANDAY(o.day_key) - JULIANDAY(e.day_key))) * o.weight_kg
           + POWER(0.90, JULIANDAY(o.day_key) - JULIANDAY(e.day_key)) * e.trend_kg
  FROM ordered o
  JOIN ewma e ON o.user_id = e.user_id AND o.rn = e.rn + 1
)
SELECT user_id, day_key, weight_kg,
       ROUND(trend_kg, 4) AS trend_kg
FROM ewma
ORDER BY user_id, day_key;
