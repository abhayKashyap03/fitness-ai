-- ============================================================
--  Migration 0005 — per-app source discriminator `source_app` (D3)
--
--  CONTEXT (docs/adr/0008-healthkit-source-app.md): a single `source`
--  (e.g. 'healthkit') can carry MULTIPLE writer apps for the SAME metric —
--  OKOK scale + MyFitnessPal both write BodyMass; MFP + Foodnoms both write
--  food. Per §2.3 these must stay SIBLING rows and resolve at read time, so we
--  need to tell the apps apart WITHOUT collapsing them.
--
--  DECISION (D3 option 1): add a NON-DESTRUCTIVE `source_app` TEXT column to
--  `food_entry` and `weight_measurement` (same safe `ALTER TABLE ADD COLUMN`
--  pattern as 0004's `utc_offset`). `source` stays 'healthkit'; `source_app`
--  holds 'okok' | 'myfitnesspal' | 'foodnoms' | ... . NULL when the source has
--  no sub-app (single-writer sources). raw_events is UNTOUCHED — no rebuild of
--  the sacred table (§8.5). Two-way door, reversible.
--
--  The resolver views must be RECREATED (not just re-read) because they were
--  authored with `SELECT *` / a fixed GROUP BY that predates the new column.
--  Recreating them lets siblings split by (source, source_app) and lets
--  precedence prefer a real scale (okok) over an app that merely mirrors a
--  weight (myfitnesspal). Views are disposable (no data) — safe to drop.
-- ============================================================

-- 1) Non-destructive column adds (raw_events NOT touched).
ALTER TABLE weight_measurement ADD COLUMN source_app TEXT;  -- 'okok'|'myfitnesspal'|...; NULL if none
ALTER TABLE food_entry         ADD COLUMN source_app TEXT;

-- §2.6 travel-proof time: recovery/workout already carry utc_offset (0004);
-- weight/food did not. Add it so a HealthKit weigh-in's local day is exact and
-- these tables are §2.6-complete. NULL when the source gives no offset.
ALTER TABLE weight_measurement ADD COLUMN utc_offset TEXT;
ALTER TABLE food_entry         ADD COLUMN utc_offset TEXT;

-- ------------------------------------------------------------
-- 2) weight_resolved_daily — recreated with source_app in precedence.
--    Within a source, a real scale (okok/withings/renpho) outranks an app
--    that only mirrors a manually-typed weight (myfitnesspal). Ties then fall
--    to the earliest reading of the day (morning-fasted proxy), as before.
-- ------------------------------------------------------------
DROP VIEW IF EXISTS weight_trend;
DROP VIEW IF EXISTS weight_resolved_daily;

CREATE VIEW weight_resolved_daily AS
WITH ranked AS (
  SELECT w.*,
         ROW_NUMBER() OVER (
           PARTITION BY user_id, day_key
           ORDER BY CASE source
                      WHEN 'withings'       THEN 1
                      WHEN 'renpho'         THEN 2
                      WHEN 'healthkit'      THEN 3
                      WHEN 'health_connect' THEN 3
                      WHEN 'manual'         THEN 4
                      ELSE 9
                    END,
                    -- within a source: real scale before app-mirrored weight
                    CASE
                      WHEN source_app = 'myfitnesspal' THEN 1
                      WHEN source_app = 'manual'       THEN 1
                      ELSE 0
                    END,
                    (measured_at IS NULL),   -- 0 (has ts) before 1 (null)
                    measured_at ASC          -- earliest reading of the day
         ) AS rnk
  FROM weight_measurement w
  WHERE weight_kg IS NOT NULL
)
SELECT * FROM ranked WHERE rnk = 1;

-- weight_trend — unchanged logic, recreated after its dependency.
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
         0.10 * o.weight_kg + 0.90 * e.trend_kg
  FROM ordered o
  JOIN ewma e ON o.user_id = e.user_id AND o.rn = e.rn + 1
)
SELECT user_id, day_key, weight_kg,
       ROUND(trend_kg, 4) AS trend_kg
FROM ewma
ORDER BY user_id, day_key;

-- ------------------------------------------------------------
-- 3) food views — recreated so two apps under one source (e.g. MFP + Foodnoms
--    both 'healthkit') stay SIBLINGS instead of merging into one SUM (which
--    would double-count a shared day). GROUP BY now includes source_app; the
--    daily picker ranks by (source, source_app).
-- ------------------------------------------------------------
DROP VIEW IF EXISTS food_daily;
DROP VIEW IF EXISTS food_daily_by_source;

CREATE VIEW food_daily_by_source AS
SELECT
  user_id,
  day_key,
  source,
  source_app,
  SUM(kcal)      AS kcal_total,
  SUM(protein_g) AS protein_g_total,
  SUM(carbs_g)   AS carbs_g_total,
  SUM(fat_g)     AS fat_g_total,
  SUM(fiber_g)   AS fiber_g_total,
  SUM(alcohol_g) AS alcohol_g_total,
  COUNT(*)                                              AS entries_n,
  SUM(CASE WHEN entry_type = 'fast' THEN 1 ELSE 0 END)  AS fast_n,
  SUM(CASE WHEN entry_type IN ('item','daily_total') AND kcal IS NULL
           THEN 1 ELSE 0 END)                            AS items_missing_kcal_n,
  SUM(CASE WHEN entry_type IN ('item','daily_total')
                AND (protein_g IS NULL OR carbs_g IS NULL OR fat_g IS NULL)
           THEN 1 ELSE 0 END)                            AS items_missing_macros_n,
  MAX(CASE WHEN entry_type = 'fast' THEN 1 ELSE 0 END)  AS is_fast,
  CASE WHEN SUM(CASE WHEN entry_type IN ('item','daily_total') AND kcal IS NULL
                     THEN 1 ELSE 0 END) = 0
       THEN 1 ELSE 0 END                                 AS is_complete
FROM food_entry
GROUP BY user_id, day_key, source, source_app;

CREATE VIEW food_daily AS
WITH ranked AS (
  SELECT s.*,
         ROW_NUMBER() OVER (
           PARTITION BY user_id, day_key
           ORDER BY CASE source
                      WHEN 'manual'         THEN 1
                      WHEN 'healthkit'      THEN 2
                      WHEN 'health_connect' THEN 2
                      WHEN 'usda'           THEN 3
                      WHEN 'openfoodfacts'  THEN 3
                      ELSE 9
                    END,
                    -- within a source, prefer a dedicated food logger
                    CASE source_app
                      WHEN 'myfitnesspal' THEN 0
                      WHEN 'foodnoms'     THEN 1
                      ELSE 2
                    END
         ) AS rnk
  FROM food_daily_by_source s
)
SELECT * FROM ranked WHERE rnk = 1;
