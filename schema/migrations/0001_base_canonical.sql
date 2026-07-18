-- ============================================================
--  Migration 0001 — base canonical schema (WHOOP vertical slice)
--  Executable mirror of schema/canonical_schema_v0.1.sql.
--  See that file for the fully-annotated design rationale.
--
--  Migrations are the EXECUTABLE source of truth. The migration
--  runner (src/coach/store) applies files in this directory in
--  ascending numeric order and records them in schema_version.
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

-- ------------------------------------------------------------
-- 2. RECOVERY — one row per (user, day, source, score_method)
-- ------------------------------------------------------------
CREATE TABLE recovery (
  id             TEXT PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  day_key        TEXT NOT NULL,             -- local physiological-day date 'YYYY-MM-DD'
  source         TEXT NOT NULL CHECK (source IN
                   ('whoop_api','whoop_ble','oura','garmin','manual')),

  measured_at    TEXT,                      -- UTC ISO-8601 (usually last night's sleep end)
  tz_name        TEXT,                      -- IANA, e.g. 'America/New_York' (travel-proof)

  -- OBJECTIVE measurements — comparable ACROSS sources
  hrv_rmssd_ms   REAL,
  resting_hr_bpm REAL,
  spo2_pct       REAL,
  skin_temp_c    REAL,
  resp_rate_bpm  REAL,

  -- DERIVED composite score — NOT comparable across sources
  score          REAL,
  score_scale    TEXT,                      -- 'whoop_0_100','computed_0_100'
  score_method   TEXT,                      -- 'whoop_proprietary','rmssd_baseline_v1'...
  is_official    INTEGER NOT NULL DEFAULT 0,

  raw_ref        TEXT REFERENCES raw_events(id),
  derived_at     TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,

  UNIQUE (user_id, day_key, source, score_method)
);

-- ------------------------------------------------------------
-- 3. WORKOUT — one row per (source, detected session)
-- ------------------------------------------------------------
CREATE TABLE workout (
  id               TEXT PRIMARY KEY,
  user_id          INTEGER NOT NULL DEFAULT 1,
  source           TEXT NOT NULL CHECK (source IN
                     ('whoop_api','whoop_ble','healthkit','health_connect',
                      'strava','garmin','manual')),
  external_id      TEXT,

  sport_type       TEXT NOT NULL,           -- canonical enum; adapters map into this
  source_sport_raw TEXT,                    -- what the source called it (debug/audit)

  start_at         TEXT NOT NULL,           -- UTC ISO-8601
  end_at           TEXT NOT NULL,           -- UTC ISO-8601
  tz_name          TEXT,
  day_key          TEXT NOT NULL,           -- local date the session belongs to
  duration_s       INTEGER,

  kcal_active      REAL,
  kcal_total       REAL,
  avg_hr_bpm       REAL,
  max_hr_bpm       REAL,
  strain           REAL,                    -- WHOOP 0-21 (source-specific, nullable)
  distance_m       REAL,
  hr_zones_json    TEXT,

  session_group_id TEXT,                    -- same real workout across sources
  dedupe_hash      TEXT,                    -- hash(user, start-time bucket, sport_type)

  raw_ref          TEXT REFERENCES raw_events(id),
  derived_at       TEXT NOT NULL,
  schema_version   INTEGER NOT NULL DEFAULT 1
);

-- ------------------------------------------------------------
-- 4. RESOLVER — precedence is a RULE, not schema.
--    To swap adapters at membership end, reorder the CASE.
--    (Single, obvious, documented place — see ADR-0001.)
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
