-- ============================================================
--  Migration 0003 — weight / body-composition canonical shape (T0.2)
--
--  DESIGN:
--   * Keep ALL readings (multiple per day are common: post-wake,
--     post-workout). We NEVER discard rows; resolution is at READ
--     time (§2.3), consistent with recovery.
--   * weight_resolved_daily picks ONE reading per (user, day):
--       - source precedence first (a real scale beats a manual guess),
--       - then the EARLIEST reading of the day within that source
--         (proxy for the standard morning-fasted weigh-in).
--   * weight_trend exposes an EWMA-smoothed trend. Raw daily scale
--     weight is too noisy to steer a cut/bulk on; the trend is the
--     signal. Default alpha = 0.10 (Hacker's Diet convention, ~19-day
--     smoothing). The compute layer (T3.2) parameterizes alpha; this
--     view is the convenience default.
-- ============================================================

CREATE TABLE weight_measurement (
  id             TEXT PRIMARY KEY,
  user_id        INTEGER NOT NULL DEFAULT 1,
  day_key        TEXT NOT NULL,             -- local physiological-day 'YYYY-MM-DD'
  source         TEXT NOT NULL CHECK (source IN
                   ('withings','healthkit','health_connect',
                    'renpho','manual','other')),

  measured_at    TEXT,                      -- UTC ISO-8601 (nullable)
  tz_name        TEXT,                      -- IANA (travel-proof)

  weight_kg      REAL,                      -- nullable (a reading may be BF%-only)
  body_fat_pct   REAL,
  lean_mass_kg   REAL,

  raw_ref        TEXT REFERENCES raw_events(id),
  derived_at     TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_weight_day ON weight_measurement (user_id, day_key);

-- ------------------------------------------------------------
-- weight_resolved_daily — ONE reading per (user, day).
--   Precedence lives here (one obvious place). Ties broken by the
--   earliest measured_at (morning-fasted proxy). NULL measured_at
--   sorts last so a timestamped reading always wins a same-source tie.
-- ------------------------------------------------------------
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
                    (measured_at IS NULL),   -- 0 (has ts) before 1 (null)
                    measured_at ASC          -- earliest reading of the day
         ) AS rnk
  FROM weight_measurement w
  WHERE weight_kg IS NOT NULL
)
SELECT * FROM ranked WHERE rnk = 1;

-- ------------------------------------------------------------
-- weight_trend — EWMA-smoothed weight over the resolved daily series.
--   Recursive CTE: trend_t = a*weight_t + (1-a)*trend_{t-1}, a=0.10.
--   NOTE: smooths over the SEQUENCE of weigh-ins, not calendar-filled
--   days; gaps are not interpolated (documented limitation for v0).
-- ------------------------------------------------------------
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
