-- ============================================================
--  Migration 0011 — drop the `workout.source` CHECK
--
--  CONTEXT: MyFitnessPal diary payloads carry `exercise_entry` items (a real
--  logged session: sport, duration, energy, distance) alongside the food rows.
--  Until now the normalizer dropped them, so a walk logged in MFP existed in
--  raw_events but never reached `workout` — invisible to `coach status`, the
--  dashboard and the coach. Fixing that needs source='myfitnesspal', which the
--  0002 CHECK predates.
--
--  We DROP the CHECK entirely rather than extend it, following ADR-0009: under
--  §2.5 adding a new source should be a new adapter file, not a schema change.
--  Every future source (BLE, Strava, an in-app logger) would otherwise need its
--  own migration for no safety benefit — the adapter boundary is what actually
--  validates `source`.
--
--  SQLite can't ALTER a CHECK, so this is a ROW-PRESERVING rebuild of `workout`
--  (a canonical, regenerable table; nothing references it by FK). raw_events is
--  UNTOUCHED (§2.1/§8.5). The migration runner hosts the 12-step protocol:
--  foreign_keys off for the duration, whole-DB foreign_key_check before COMMIT.
-- ============================================================

CREATE TABLE workout_new (
  id               TEXT PRIMARY KEY,
  user_id          INTEGER NOT NULL DEFAULT 1,
  source           TEXT NOT NULL,           -- validated at the adapter boundary (§2.5)
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
  schema_version   INTEGER NOT NULL DEFAULT 1,
  utc_offset       TEXT
);

INSERT INTO workout_new
  (id, user_id, source, external_id, sport_type, source_sport_raw, start_at,
   end_at, tz_name, day_key, duration_s, kcal_active, kcal_total, avg_hr_bpm,
   max_hr_bpm, strain, distance_m, hr_zones_json, session_group_id,
   dedupe_hash, raw_ref, derived_at, schema_version, utc_offset)
SELECT
   id, user_id, source, external_id, sport_type, source_sport_raw, start_at,
   end_at, tz_name, day_key, duration_s, kcal_active, kcal_total, avg_hr_bpm,
   max_hr_bpm, strain, distance_m, hr_zones_json, session_group_id,
   dedupe_hash, raw_ref, derived_at, schema_version, utc_offset
FROM workout;

DROP TABLE workout;

ALTER TABLE workout_new RENAME TO workout;

CREATE INDEX idx_workout_day   ON workout (user_id, day_key);
CREATE INDEX idx_workout_group ON workout (session_group_id);
