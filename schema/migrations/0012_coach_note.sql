-- ============================================================
--  Migration 0012 — `coach_note` (coaching memory, ADR-0016)
--
--  The coach had data but no record of its own DECISIONS, so guidance drifted
--  between sessions. This is that record.
--
--  Like `plan` (0010) this is USER/CODE-authored primary data, not derived from
--  raw_events: no raw_ref, and it takes no part in the normalize rebuild or its
--  fingerprint. Append-only — a superseded note is followed by a new one, never
--  edited, so what was believed at the time stays recoverable.
--
--  `author` is deliberately constrained to 'user' | 'system'. There is no
--  'model' value, and adding one is a decision, not an implementation detail
--  (ADR-0016): a model-authored memory would let a fabricated number become
--  persistent truth that nothing ever recomputes, defeating §2.2.
-- ============================================================

CREATE TABLE coach_note (
  id             TEXT PRIMARY KEY,          -- note:<user>:<created_at>
  user_id        INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL,             -- UTC ISO-8601 (when it was recorded)
  day_key        TEXT NOT NULL,             -- local day the note is ABOUT (§2.6)

  author         TEXT NOT NULL CHECK (author IN ('user', 'system')),
  kind           TEXT NOT NULL,             -- 'plan' | 'advice' | 'observation' | 'note'
  text           TEXT NOT NULL,             -- the decision/observation, in words

  schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_coach_note_day ON coach_note (user_id, day_key);
