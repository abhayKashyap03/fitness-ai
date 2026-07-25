-- ============================================================
--  Migration 0007 — MyFitnessPal as a weight `source`
--
--  CONTEXT: we now ingest the user's MFP weight measurements directly
--  (/measurements?type=weight&entry_date=...). Honest provenance (§2.3) wants
--  source='myfitnesspal', but the 0003 CHECK predates it. As in 0006, SQLite
--  can't ALTER a CHECK, so we ROW-PRESERVING rebuild weight_measurement (a
--  canonical, regenerable table; nothing references it by FK). raw_events is
--  UNTOUCHED. The two weight views depend on it and are recreated below.
--
--  MFP weight is USER-TYPED into the MFP app (not a scale reading), so it ranks
--  just below 'manual' and always loses to a real scale (withings/renpho, or an
--  okok scale fronted by healthkit) on any day both are present (§2.3).
-- ============================================================

-- 1) Widened table — 0003 columns + 0005's source_app/utc_offset, CHECK now
--    includes 'myfitnesspal'. Column order matches the live table.
CREATE TABLE weight_measurement_new (
  id             TEXT PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  day_key        TEXT NOT NULL,
  source         TEXT NOT NULL CHECK (source IN
                   ('withings','myfitnesspal','healthkit','health_connect',
                    'renpho','manual','other')),
  measured_at    TEXT,
  tz_name        TEXT,
  weight_kg      REAL,
  body_fat_pct   REAL,
  lean_mass_kg   REAL,
  raw_ref        TEXT REFERENCES raw_events(id),
  derived_at     TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  source_app     TEXT,
  utc_offset     TEXT
);

INSERT INTO weight_measurement_new
  (id, user_id, day_key, source, measured_at, tz_name, weight_kg, body_fat_pct,
   lean_mass_kg, raw_ref, derived_at, schema_version, source_app, utc_offset)
SELECT
   id, user_id, day_key, source, measured_at, tz_name, weight_kg, body_fat_pct,
   lean_mass_kg, raw_ref, derived_at, schema_version, source_app, utc_offset
FROM weight_measurement;

-- 2) Drop dependent views BEFORE the table (they are re-parsed during the
--    RENAME; a view pointing at the just-dropped table would raise). Recreated
--    in step 4.
DROP VIEW IF EXISTS weight_trend;
DROP VIEW IF EXISTS weight_resolved_daily;

-- 3) Swap in the widened table + recreate its index.
DROP TABLE weight_measurement;
ALTER TABLE weight_measurement_new RENAME TO weight_measurement;
CREATE INDEX idx_weight_day ON weight_measurement (user_id, day_key);

-- 4) Recreate the views. Same shape as 0005; only the source precedence gains
--    'myfitnesspal' (rank 5: below manual, above unknown). Within a source, a
--    real scale still outranks an app-mirrored weight (source_app tiebreak).
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
                      WHEN 'myfitnesspal'   THEN 5  -- user-typed in MFP; loses to any scale
                      ELSE 9
                    END,
                    CASE
                      WHEN source_app = 'myfitnesspal' THEN 1
                      WHEN source_app = 'manual'       THEN 1
                      ELSE 0
                    END,
                    (measured_at IS NULL),
                    measured_at ASC
         ) AS rnk
  FROM weight_measurement w
  WHERE weight_kg IS NOT NULL
)
SELECT * FROM ranked WHERE rnk = 1;

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
