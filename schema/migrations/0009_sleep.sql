-- ============================================================
--  Migration 0009 — canonical `sleep` table
--
--  Sleep was ingested raw from day one (§2.1) but only respiratory_rate ever
--  reached canonical (joined onto recovery). This promotes the full sleep
--  record: stages, efficiency, need — the last §1 data type without a
--  canonical home. Same provenance pattern as recovery (§5):
--
--   * OBJECTIVE durations/counts (stage minutes, cycle/disturbance counts,
--     respiratory rate) are physical quantities, comparable across sources —
--     calibration currency for the future BLE adapter (ADR-0012).
--   * COMPOSITE percentages (performance/consistency/efficiency) are
--     WHOOP-proprietary — NOT comparable across sources — so rows carry
--     score_method + is_official exactly like recovery.score.
--
--  One row PER SLEEP SESSION (naps are separate rows, flagged is_nap);
--  day_key = the local day the sleep ENDED (the physiological day it serves —
--  the night of the 14th/15th belongs to the 15th). Unscored sleeps are
--  skipped by the normalizer, not stored as zeros (§2.7).
-- ============================================================

CREATE TABLE sleep (
  id             TEXT PRIMARY KEY,          -- slp:<user>:<source>:<external_id>
  user_id        INTEGER NOT NULL DEFAULT 1,
  day_key        TEXT NOT NULL,             -- local day the sleep ENDED
  source         TEXT NOT NULL,             -- adapter name ('whoop_api', 'whoop_ble', ...)
  external_id    TEXT NOT NULL,             -- source's own sleep id
  is_nap         INTEGER NOT NULL DEFAULT 0,

  start_at       TEXT NOT NULL,             -- UTC ISO-8601
  end_at         TEXT NOT NULL,             -- UTC ISO-8601
  tz_name        TEXT,                      -- strictly IANA; NULL when unknown (§2.6)
  utc_offset     TEXT,                      -- e.g. '-04:00'

  -- objective stage summary (minutes; NULL = source didn't report, never 0)
  in_bed_min     REAL,
  awake_min      REAL,
  light_min      REAL,
  sws_min        REAL,                      -- slow-wave (deep)
  rem_min        REAL,
  no_data_min    REAL,
  sleep_cycle_count   INTEGER,
  disturbance_count   INTEGER,
  respiratory_rate    REAL,

  -- sleep-need decomposition (minutes)
  need_baseline_min     REAL,
  need_from_debt_min    REAL,
  need_from_strain_min  REAL,
  need_from_nap_min     REAL,

  -- composite scores — proprietary weighting, NOT cross-source comparable
  performance_pct   REAL,
  consistency_pct   REAL,
  efficiency_pct    REAL,
  score_method      TEXT,                   -- 'whoop_proprietary' | 'textbook' | ...
  is_official       INTEGER NOT NULL DEFAULT 0,

  raw_ref        TEXT REFERENCES raw_events(id),
  derived_at     TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_sleep_day ON sleep (user_id, day_key);

-- resolver: ONE authoritative NIGHT sleep per (user, day) at read time (§2.3).
-- Naps are excluded here (they're additive, not alternative); official source
-- outranks recomputed, then longest in-bed wins ties.
CREATE VIEW sleep_resolved AS
WITH ranked AS (
  SELECT s.*,
         ROW_NUMBER() OVER (
           PARTITION BY user_id, day_key
           ORDER BY is_official DESC,
                    CASE source WHEN 'whoop_api' THEN 1 WHEN 'whoop_ble' THEN 2 ELSE 9 END,
                    in_bed_min DESC
         ) AS rnk
  FROM sleep s
  WHERE is_nap = 0
)
SELECT * FROM ranked WHERE rnk = 1;
