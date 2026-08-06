-- ============================================================
--  Migration 0015 — recorded acknowledgement of the medical disclaimer
--
--  §8.6 and ADR-0018 both make medical disclaimers a hard prerequisite rather
--  than a backlog item: the calorie floor and maximum-loss-rate guardrails were
--  written by one consenting adult for himself, and the moment anyone else's
--  data is in this database they are protecting a stranger.
--
--  WHY A TABLE AND NOT A FOOTER
--  A footer nobody reads is the same failure the auth work already rejected
--  once ("a runtime warning nobody reads is not good enough"). Storing the
--  acknowledgement makes it answerable rather than assumed: for any user we can
--  say what they were shown and when they agreed to it.
--
--  WHY IT IS VERSIONED
--  An acknowledgement is of a SPECIFIC text. If the notice is later revised —
--  a new limitation found, a new source added — an old row is not consent to
--  the new wording. Bumping `disclaimer.DISCLAIMER_VERSION` re-prompts everyone
--  without deleting the history of what they previously agreed to.
--
--  APPEND-ONLY, like `plan` and `coach_note` (§2.1). A withdrawn or superseded
--  acknowledgement stays visible; the CURRENT state is the newest row for a
--  (user, version) pair. This is user-authored primary data: no raw_ref, and it
--  is absent from the rebuild fingerprint, exactly like the other user-authored
--  tables.
-- ============================================================

CREATE TABLE user_disclaimer_ack (
  id              INTEGER PRIMARY KEY,
  user_id         INTEGER NOT NULL REFERENCES app_user(id),
  -- The version of disclaimer.FULL that was actually displayed. Never defaulted
  -- in SQL: the value must come from the code that rendered the text, so the
  -- two cannot drift apart silently.
  version         INTEGER NOT NULL,
  acknowledged_at TEXT    NOT NULL,   -- UTC ISO-8601 instant (§2.6)
  -- Recorded for the same reason user_session records it: if an acknowledgement
  -- is ever disputed, "which browser said yes" is the only evidence there is.
  -- NULL when unknown, never a placeholder string (§2.7).
  user_agent      TEXT
);

CREATE INDEX idx_disclaimer_ack_user
  ON user_disclaimer_ack(user_id, version, acknowledged_at DESC);
