-- ============================================================
--  Migration 0002 — food / nutrition canonical shape (T0.1)
--
--  DESIGN (see docs/adr/0002-food-entry-vs-rollup.md):
--   * The ENTRY is the fact. A day's totals are a DERIVED VIEW,
--     never a stored row (§2.1: canonical is regenerable; a stored
--     total is a second source of truth that drifts).
--   * "Not logged yet" vs "genuinely ate nothing" MUST be
--     distinguishable — the coach's advice depends on it:
--       - NO rows for a day_key            => NOT LOGGED (unknown).
--       - a row with entry_type='fast'     => KNOWN ZERO (declared).
--     A 0-kcal 'item' (black coffee) is NOT a fast; only an explicit
--     'fast' row asserts "I ate nothing (more) today".
--   * Partial macros are first-class: every macro column is nullable.
--     Completeness is surfaced by the view, not hidden by SUM().
--   * Same provenance pattern as recovery/workout: source, raw_ref,
--     user_id, day_key, tz_name. Sibling rows across sources; the
--     resolver picks ONE authoritative source per day at READ time.
-- ============================================================

CREATE TABLE food_entry (
  id             TEXT PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  day_key        TEXT NOT NULL,             -- local physiological-day 'YYYY-MM-DD'
  source         TEXT NOT NULL CHECK (source IN
                   ('manual','healthkit','health_connect',
                    'usda','openfoodfacts','other')),

  -- item      : one logged food/portion
  -- daily_total: a source that only exposes a per-day total (passthrough)
  -- fast      : explicit "ate nothing (more) today" — a KNOWN ZERO
  entry_type     TEXT NOT NULL DEFAULT 'item'
                   CHECK (entry_type IN ('item','daily_total','fast')),

  consumed_at    TEXT,                      -- UTC ISO-8601 (nullable: some sources give only a day)
  tz_name        TEXT,                      -- IANA (travel-proof)

  description    TEXT,                      -- food name / label (nullable)
  quantity       REAL,                      -- portion amount (nullable)
  unit           TEXT,                      -- 'g','ml','serving'... (nullable)

  -- macros — ALL nullable; a partial log (kcal only, or macros only) is valid
  kcal           REAL,
  protein_g      REAL,
  carbs_g        REAL,
  fat_g          REAL,
  fiber_g        REAL,
  alcohol_g      REAL,

  raw_ref        TEXT REFERENCES raw_events(id),
  derived_at     TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_food_day        ON food_entry (user_id, day_key);
CREATE INDEX idx_food_day_source ON food_entry (user_id, day_key, source);

-- ------------------------------------------------------------
-- food_daily_by_source — per (user, day, source) totals + completeness.
--   SUM() silently skips NULLs, so we EXPOSE completeness counts rather
--   than let a partial log masquerade as a complete one.
-- ------------------------------------------------------------
CREATE VIEW food_daily_by_source AS
SELECT
  user_id,
  day_key,
  source,
  -- a fast day is a known zero; SUM over its 0/NULL macros already yields 0
  SUM(kcal)      AS kcal_total,
  SUM(protein_g) AS protein_g_total,
  SUM(carbs_g)   AS carbs_g_total,
  SUM(fat_g)     AS fat_g_total,
  SUM(fiber_g)   AS fiber_g_total,
  SUM(alcohol_g) AS alcohol_g_total,
  COUNT(*)                                              AS entries_n,
  SUM(CASE WHEN entry_type = 'fast' THEN 1 ELSE 0 END)  AS fast_n,
  -- completeness: how many food-bearing entries are missing kcal / macros
  SUM(CASE WHEN entry_type IN ('item','daily_total') AND kcal IS NULL
           THEN 1 ELSE 0 END)                            AS items_missing_kcal_n,
  SUM(CASE WHEN entry_type IN ('item','daily_total')
                AND (protein_g IS NULL OR carbs_g IS NULL OR fat_g IS NULL)
           THEN 1 ELSE 0 END)                            AS items_missing_macros_n,
  MAX(CASE WHEN entry_type = 'fast' THEN 1 ELSE 0 END)  AS is_fast,
  -- complete = nothing food-bearing is missing kcal (fast-only days count complete)
  CASE WHEN SUM(CASE WHEN entry_type IN ('item','daily_total') AND kcal IS NULL
                     THEN 1 ELSE 0 END) = 0
       THEN 1 ELSE 0 END                                 AS is_complete
FROM food_entry
GROUP BY user_id, day_key, source;

-- ------------------------------------------------------------
-- food_daily — ONE authoritative row per (user, day).
--   Picks the highest-precedence source PRESENT that day, so two
--   sources logging the same meal never double-count (§2.3).
--   Precedence lives here, in one obvious place (mirrors
--   recovery_resolved). See ADR-0002 for the "pick one source vs
--   merge" tradeoff.
-- ------------------------------------------------------------
CREATE VIEW food_daily AS
WITH ranked AS (
  SELECT s.*,
         ROW_NUMBER() OVER (
           PARTITION BY user_id, day_key
           ORDER BY CASE source
                      WHEN 'manual'         THEN 1  -- user-logged is most trusted for intent
                      WHEN 'healthkit'      THEN 2
                      WHEN 'health_connect' THEN 2
                      WHEN 'usda'           THEN 3
                      WHEN 'openfoodfacts'  THEN 3
                      ELSE 9
                    END
         ) AS rnk
  FROM food_daily_by_source s
)
SELECT * FROM ranked WHERE rnk = 1;
