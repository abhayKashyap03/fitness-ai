-- ============================================================
--  Migration 0010 — `plan` table (Phase 7, the cut/bulk plan layer)
--
--  The first USER-AUTHORED table. Unlike every other canonical table, a plan is
--  NOT derived from raw_events (§2.1) — it's a declared goal, primary data with
--  no raw_ref and no place in the normalize rebuild or its fingerprint.
--
--  Target model = ADR-0013 (D5, option C): the ONE canonical driver is
--  `target_rate_pct_per_week` (signed: negative = loss, positive = gain, 0 =
--  maintain). A deadline entry is convenience only — it computes an initial rate
--  that is clamped to the §8.6 sustainable-loss ceiling and stored as the rate;
--  the deadline is not persisted. `goal_weight_kg` is an optional endpoint for
--  progress/projection but does NOT drive the daily calorie goal (the rate does).
--
--  Plans are APPEND-ONLY history: `plan set` inserts a new row and deactivates
--  prior ones; the active plan is the single row with is_active = 1. Nothing is
--  edited in place, so the record of what the target WAS stays intact.
-- ============================================================

CREATE TABLE plan (
  id             TEXT PRIMARY KEY,          -- plan:<user>:<created_at>
  user_id        INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL,             -- UTC ISO-8601 (authored time)
  start_day_key  TEXT NOT NULL,             -- local day the plan takes effect
  is_active      INTEGER NOT NULL DEFAULT 1,

  direction      TEXT NOT NULL CHECK (direction IN ('cut', 'bulk', 'maintain')),
  -- signed %/week; the canonical driver (ADR-0013). cut<0, bulk>0, maintain=0.
  target_rate_pct_per_week  REAL NOT NULL,

  -- anchor + endpoint (nullable — §2.7; a plan can be set before a trend exists)
  start_weight_kg  REAL,                    -- trend weight at start, for progress
  goal_weight_kg   REAL,                    -- optional endpoint for projection

  protein_g_per_kg REAL,                    -- optional macro floor (future use)
  note             TEXT,                    -- e.g. 'deadline entry 78kg by 2026-10-01 -> clamped'

  schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_plan_active ON plan (user_id, is_active);
