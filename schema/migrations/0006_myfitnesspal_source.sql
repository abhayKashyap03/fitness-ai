-- ============================================================
--  Migration 0006 — MyFitnessPal as a first-class food `source` (D4)
--
--  CONTEXT (docs/adr/0009-myfitnesspal-direct-api.md): we now ingest MFP food
--  DIRECTLY from MFP's v2 JSON API (not mirrored through HealthKit), so honest
--  provenance (§2.3/§2.5) demands source='myfitnesspal' — the adapter that made
--  the row. The 0002 CHECK predates that source and would reject the insert.
--
--  SQLite cannot ALTER a CHECK constraint in place, so we do the standard
--  12-step table rebuild: create the widened table, COPY every row, drop the
--  old, rename. This is ROW-PRESERVING — no data is lost — so §6.1's
--  "don't DROP a real-data table without sign-off" tripwire is not crossed
--  (and food_entry is canonical/regenerable from raw either way, §2.1).
--  raw_events is UNTOUCHED (§8.5).
--
--  Nothing references food_entry (it is a referencing leaf: it points AT
--  raw_events, nothing points at it), so the drop breaks no foreign key. The
--  two food views depend on it and are recreated below.
-- ============================================================

-- raw_events is a PARENT: recovery/workout/weight/food_entry all carry a
-- raw_ref FK to it. The migration runner applies this file with FK enforcement
-- OFF and then runs `PRAGMA foreign_key_check` on the whole DB before COMMIT
-- (see store/db.py::migrate), so dropping + rebuilding the parent is safe: by
-- commit the table is back under the same name with every row + id intact, all
-- raw_ref FKs resolve, and any dangling reference would roll the migration back.

-- ============================================================
--  Part A — drop the source CHECK on raw_events (signed off).
--
--  APPROVED: the whitelist CHECK fought §2.5 ("a new source should be a new
--  adapter, not a schema change"). Removing it entirely means MFP — and every
--  future source (BLE, Strava, ...) — needs no raw_events migration again.
--  This is a ROW-PRESERVING rebuild: every raw row is copied verbatim before
--  the old table is dropped (§2.1 raw is sacred; §8.5 rebuild done WITH
--  sign-off + a backup-first workflow). Take `coach db backup` first.
-- ============================================================

-- Same columns as 0001's raw_events, MINUS the source CHECK. UNIQUE + PK
-- come with the table; idx_raw_lookup is recreated below.
CREATE TABLE raw_events_new (
  id             TEXT PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  source         TEXT NOT NULL,             -- CHECK removed: any adapter's own name
  record_type    TEXT NOT NULL,
  external_id    TEXT,
  recorded_at    TEXT,
  ingested_at    TEXT NOT NULL,
  payload        TEXT NOT NULL,
  payload_hash   TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  UNIQUE (source, external_id, payload_hash)
);

INSERT INTO raw_events_new
  (id, user_id, source, record_type, external_id, recorded_at, ingested_at,
   payload, payload_hash, schema_version)
SELECT
   id, user_id, source, record_type, external_id, recorded_at, ingested_at,
   payload, payload_hash, schema_version
FROM raw_events;

DROP TABLE raw_events;
ALTER TABLE raw_events_new RENAME TO raw_events;
CREATE INDEX idx_raw_lookup ON raw_events (source, record_type, recorded_at);

-- ============================================================
--  Part B — MyFitnessPal as a first-class canonical food source.
-- ============================================================

-- 1) Widened table — identical to 0002 + 0005's added columns, CHECK now
--    includes 'myfitnesspal'. Column order matches the live table
--    (0002 body, then 0005's source_app, utc_offset) so the copy lines up.
CREATE TABLE food_entry_new (
  id             TEXT PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  day_key        TEXT NOT NULL,
  source         TEXT NOT NULL CHECK (source IN
                   ('manual','myfitnesspal','healthkit','health_connect',
                    'usda','openfoodfacts','other')),
  entry_type     TEXT NOT NULL DEFAULT 'item'
                   CHECK (entry_type IN ('item','daily_total','fast')),
  consumed_at    TEXT,
  tz_name        TEXT,
  description    TEXT,
  quantity       REAL,
  unit           TEXT,
  kcal           REAL,
  protein_g      REAL,
  carbs_g        REAL,
  fat_g          REAL,
  fiber_g        REAL,
  alcohol_g      REAL,
  raw_ref        TEXT REFERENCES raw_events(id),
  derived_at     TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  source_app     TEXT,
  utc_offset     TEXT
);

-- 2) Row-preserving copy (explicit columns; no SELECT * so a future column
--    add on either side fails loudly instead of silently misaligning).
INSERT INTO food_entry_new
  (id, user_id, day_key, source, entry_type, consumed_at, tz_name,
   description, quantity, unit, kcal, protein_g, carbs_g, fat_g, fiber_g,
   alcohol_g, raw_ref, derived_at, schema_version, source_app, utc_offset)
SELECT
   id, user_id, day_key, source, entry_type, consumed_at, tz_name,
   description, quantity, unit, kcal, protein_g, carbs_g, fat_g, fiber_g,
   alcohol_g, raw_ref, derived_at, schema_version, source_app, utc_offset
FROM food_entry;

-- 3) Drop the dependent views BEFORE the old table. SQLite re-parses every
--    view during the RENAME below; if food_daily_by_source still pointed at the
--    just-dropped food_entry it would raise "no such table". Views are
--    disposable (no data) — recreated in step 6.
DROP VIEW IF EXISTS food_daily;
DROP VIEW IF EXISTS food_daily_by_source;

-- 4) Swap in the widened table.
DROP TABLE food_entry;
ALTER TABLE food_entry_new RENAME TO food_entry;

-- 5) Recreate indexes (dropped with the old table).
CREATE INDEX idx_food_day        ON food_entry (user_id, day_key);
CREATE INDEX idx_food_day_source ON food_entry (user_id, day_key, source);

-- 6) Recreate the views. Same shape as 0005; only food_daily's precedence
--    changes: a DIRECT MyFitnessPal log is the user's own intent at full
--    fidelity, so it outranks 'manual' and any HealthKit-mirrored copy of the
--    same day (source_app='myfitnesspal' under 'healthkit'). Direct beats
--    mirror (§2.3).
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
                      WHEN 'myfitnesspal'   THEN 1  -- direct MFP: own intent, full fidelity
                      WHEN 'manual'         THEN 2
                      WHEN 'healthkit'      THEN 3
                      WHEN 'health_connect' THEN 3
                      WHEN 'usda'           THEN 4
                      WHEN 'openfoodfacts'  THEN 4
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
