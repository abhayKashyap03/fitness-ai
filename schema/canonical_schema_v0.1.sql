-- ============================================================
--  Canonical Schema — DRAFT v0.1
--  Unified AI Health & Fitness Coach
--  Scope: recovery + workout (the WHOOP vertical slice)
--  Store: SQLite. Everything derives from raw_events.
-- ============================================================
--
--  CONVENTIONS (apply to every canonical table)
--  * user_id on every row (always 1 today; multi-tenancy insurance).
--  * source  = which adapter produced the row. Provenance is first-class.
--  * raw_ref = FK back to the immutable raw payload it was derived from,
--              so canonical is fully REGENERABLE when logic improves.
--  * Times: UTC ISO-8601 text (SQLite has no native datetime) PLUS the
--           local tz name PLUS a day_key, so travel never corrupts days.
--  * SQLite has no ENUM -> TEXT + CHECK constraints.
--  * Canonical rows are DISPOSABLE. Raw is SACRED. Never edit raw.
-- ============================================================


-- ------------------------------------------------------------
-- 1. RAW STORE — append-only, immutable, keep forever
-- ------------------------------------------------------------
CREATE TABLE raw_events (
  id             TEXT PRIMARY KEY,          -- uuid
  user_id        INTEGER NOT NULL DEFAULT 1,
  source         TEXT NOT NULL CHECK (source IN
                   ('whoop_api','whoop_ble','healthkit','health_connect',
                    'withings','strava','oura','garmin','manual')),
  record_type    TEXT NOT NULL,             -- 'recovery','workout','sleep','cycle','hr_sample'...
  external_id    TEXT,                      -- source's own id (idempotent ingest)
  recorded_at    TEXT,                      -- UTC ISO-8601: when the data is *about*
  ingested_at    TEXT NOT NULL,             -- UTC ISO-8601: when we captured it
  payload        TEXT NOT NULL,             -- verbatim source JSON, untouched
  payload_hash   TEXT NOT NULL,             -- dedupe: hash(source, external_id, payload)
  schema_version INTEGER NOT NULL DEFAULT 1,
  UNIQUE (source, external_id, payload_hash)
);

-- Optional high-frequency companion for LOCAL recompute (RR intervals, HR stream).
-- Deferred until the BLE read is proven, but reserving the shape:
-- CREATE TABLE raw_signals (
--   id TEXT PRIMARY KEY, user_id INTEGER, source TEXT,
--   signal_type TEXT,            -- 'rr_interval','hr','accel'...
--   t_utc TEXT, value REAL, raw_ref TEXT REFERENCES raw_events(id)
-- );


-- ------------------------------------------------------------
-- 2. RECOVERY — one row per (user, day, source)
--    Dual-adapter answer: official + self-computed are SIBLING ROWS,
--    same shape, distinguished by `source` + `score_method`.
-- ------------------------------------------------------------
CREATE TABLE recovery (
  id             TEXT PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  day_key        TEXT NOT NULL,             -- local physiological-day date 'YYYY-MM-DD'
  source         TEXT NOT NULL CHECK (source IN
                   ('whoop_api','whoop_ble','oura','garmin','manual')),

  measured_at    TEXT,                      -- UTC ISO-8601 (usually last night's sleep end)
  tz_name        TEXT,                      -- IANA, e.g. 'America/New_York' (travel-proof)

  -- OBJECTIVE measurements — comparable ACROSS sources -----------------
  hrv_rmssd_ms   REAL,                      -- the honest cross-source currency
  resting_hr_bpm REAL,
  spo2_pct       REAL,
  skin_temp_c    REAL,
  resp_rate_bpm  REAL,

  -- DERIVED composite score — NOT comparable across sources -----------
  score          REAL,                      -- 0-100 (or source scale)
  score_scale    TEXT,                      -- 'whoop_0_100','computed_0_100'
  score_method   TEXT,                      -- 'whoop_proprietary','rmssd_baseline_v1'...
  is_official    INTEGER NOT NULL DEFAULT 0,-- 1 = vendor score, 0 = self-computed

  -- lineage -----------------------------------------------------------
  raw_ref        TEXT REFERENCES raw_events(id),
  derived_at     TEXT NOT NULL,             -- when this canonical row was computed
  schema_version INTEGER NOT NULL DEFAULT 1,

  UNIQUE (user_id, day_key, source, score_method)
);


-- ------------------------------------------------------------
-- 3. WORKOUT — one row per (source, detected session)
--    session_group_id links duplicates of the SAME real workout
--    arriving from multiple sources, so compute counts it ONCE.
-- ------------------------------------------------------------
CREATE TABLE workout (
  id               TEXT PRIMARY KEY,
  user_id          INTEGER NOT NULL DEFAULT 1,
  source           TEXT NOT NULL CHECK (source IN
                     ('whoop_api','whoop_ble','healthkit','health_connect',
                      'strava','garmin','manual')),
  external_id      TEXT,

  sport_type       TEXT NOT NULL,           -- MY canonical enum: 'strength','run','cycle',
                                            -- 'walk','hiit','swim','other'... adapters map into this
  source_sport_raw TEXT,                    -- what the source called it (debug/audit)

  start_at         TEXT NOT NULL,           -- UTC ISO-8601
  end_at           TEXT NOT NULL,           -- UTC ISO-8601
  tz_name          TEXT,
  day_key          TEXT NOT NULL,           -- local date the session belongs to
  duration_s       INTEGER,

  -- metrics (all nullable; sources vary) ------------------------------
  kcal_active      REAL,
  kcal_total       REAL,
  avg_hr_bpm       REAL,
  max_hr_bpm       REAL,
  strain           REAL,                    -- WHOOP 0-21 (source-specific, nullable)
  distance_m       REAL,
  hr_zones_json    TEXT,                    -- time-in-zone blob

  -- dedupe / grouping -------------------------------------------------
  session_group_id TEXT,                    -- same real workout across sources
  dedupe_hash      TEXT,                    -- hash(user, start-time bucket, sport_type)

  raw_ref          TEXT REFERENCES raw_events(id),
  derived_at       TEXT NOT NULL,
  schema_version   INTEGER NOT NULL DEFAULT 1
);

-- Strength detail (sets/reps/load) is a DIFFERENT shape — reserve a child
-- table for when manual / Hevy logging lands:
-- CREATE TABLE workout_set (
--   id TEXT PRIMARY KEY, workout_id TEXT REFERENCES workout(id),
--   exercise TEXT, set_no INTEGER, reps INTEGER, load_kg REAL, rpe REAL
-- );


-- ------------------------------------------------------------
-- 4. RESOLVER — the payoff. Precedence is a RULE, not schema.
--    Picks ONE authoritative recovery per day. To swap adapters
--    at membership end, reorder the CASE. That's the whole migration.
-- ------------------------------------------------------------
CREATE VIEW recovery_resolved AS
WITH ranked AS (
  SELECT r.*,
         ROW_NUMBER() OVER (
           PARTITION BY user_id, day_key
           ORDER BY CASE source
                      WHEN 'whoop_api' THEN 1   -- TODAY: official wins
                      WHEN 'whoop_ble' THEN 2   -- (flip these two after membership ends)
                      WHEN 'oura'      THEN 3
                      ELSE 9
                    END
         ) AS rnk
  FROM recovery r
)
SELECT * FROM ranked WHERE rnk = 1;

-- Helpful indexes
CREATE INDEX idx_recovery_day  ON recovery (user_id, day_key);
CREATE INDEX idx_workout_day   ON workout  (user_id, day_key);
CREATE INDEX idx_workout_group ON workout  (session_group_id);
CREATE INDEX idx_raw_lookup    ON raw_events (source, record_type, recorded_at);

-- ============================================================
--  NOTE: this file is the ANNOTATED design reference for the base
--  slice. The EXECUTABLE source of truth is schema/migrations/*.sql,
--  applied in order by the migration runner (src/coach/store).
--
--  Phase-0 additions (food, weight) are NOT duplicated here; see:
--    * schema/migrations/0002_food.sql   — food_entry + food_daily
--        (design: docs/adr/0002-food-entry-vs-rollup.md)
--    * schema/migrations/0003_weight.sql — weight_measurement +
--        weight_resolved_daily + weight_trend (EWMA)
--    * schema/migrations/0004_utc_offset.sql — adds utc_offset to recovery +
--        workout; tz_name is strictly IANA/NULL (docs/adr/0006-timezone-offset-vs-iana.md)
--  Provenance/resolution rationale: docs/adr/0001-source-row-provenance.md
-- ============================================================
