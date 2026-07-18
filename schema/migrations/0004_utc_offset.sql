-- ============================================================
--  Migration 0004 — split time zone into utc_offset + IANA tz_name (D1)
--
--  CLAUDE.md §2.6 / §9 D1: store the UTC *offset* WHOOP provides in its own
--  `utc_offset` column (e.g. '-05:00'); keep `tz_name` STRICTLY IANA and NULL
--  when unknown. Never overload `tz_name` with an offset. `day_key` is derived
--  from instant + offset and stays exact regardless.
--
--  recovery_resolved uses `SELECT *`, so it picks up the new column with no
--  view rebuild. Existing rows get NULL utc_offset until re-normalized
--  (`coach normalize --rebuild`), which re-derives it from raw.
-- ============================================================

ALTER TABLE recovery ADD COLUMN utc_offset TEXT;  -- e.g. '-05:00'; NULL if source gave none
ALTER TABLE workout  ADD COLUMN utc_offset TEXT;
