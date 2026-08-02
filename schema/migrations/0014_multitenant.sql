-- ============================================================
--  Migration 0014 — multi-tenant foundation (ADR-0018)
--
--  `user_id` has been on every row since migration 0001 (§2.4, "multi-tenancy
--  insurance"). This is where that column is finally cashed in: the users it
--  refers to become real rows, they get credentials, and their source secrets
--  get somewhere safe to live once the database is no longer on a laptop.
--
--  Nothing here changes an existing row or an existing query. Every canonical
--  table already filters by user_id, so activation is additive.
--
--  SECURITY SHAPE (the part worth reading twice):
--
--   * Passwords are NEVER stored. `password_hash` holds a scrypt digest and
--     `password_salt` its per-user salt (stdlib `hashlib.scrypt`, no dependency).
--   * Session tokens are NEVER stored. `user_session.id` is the SHA-256 of the
--     token; the token itself exists only in the user's cookie. A stolen
--     database dump therefore cannot be replayed as a live session.
--   * Source credentials (WHOOP refresh token, MFP cookie) are stored ENCRYPTED
--     with a key held in the host environment and never in this file, so a dump,
--     a backup, or a restored snapshot yields ciphertext alone.
-- ============================================================

-- ---- users -------------------------------------------------------------
--  id is deliberately the SAME integer the canonical tables already carry, so
--  existing data belongs to user 1 without a backfill.
CREATE TABLE app_user (
  id             INTEGER PRIMARY KEY,       -- matches canonical rows' user_id
  -- NULL until the account is claimed. UNIQUE tolerates multiple NULLs in
  -- SQLite, which is what lets user 1 exist before we know their email —
  -- absence stays absence (§2.7) rather than a fabricated placeholder address.
  email          TEXT UNIQUE,
  password_hash  TEXT,                      -- scrypt digest, hex; NULL until set
  password_salt  TEXT,                      -- per-user salt, hex; NULL until set
  status         TEXT NOT NULL DEFAULT 'invited'
                 CHECK (status IN ('invited', 'active', 'disabled')),
  role           TEXT NOT NULL DEFAULT 'member'
                 CHECK (role IN ('owner', 'member')),
  created_at     TEXT NOT NULL,             -- UTC ISO-8601
  activated_at   TEXT,                      -- when the password was first set
  schema_version INTEGER NOT NULL DEFAULT 1
);

-- The existing local owner. Everything in this database already belongs to
-- user_id = 1; this simply gives that id a row to point at. No email and no
-- password: the account cannot be logged into until it is explicitly claimed,
-- which is the honest state rather than an invented credential.
INSERT INTO app_user (id, email, password_hash, password_salt, status, role, created_at)
VALUES (1, NULL, NULL, NULL, 'active', 'owner', '2026-08-02T00:00:00+00:00');

-- ---- invitations -------------------------------------------------------
--  Invite-only by decision (ADR-0018): there is no public signup path, so an
--  account cannot exist without one of these being issued first.
CREATE TABLE user_invite (
  -- SHA-256 of the invite token. The token itself is shown once, to the
  -- inviter, and never persisted — same rule as sessions.
  token_hash     TEXT PRIMARY KEY,
  email          TEXT NOT NULL,
  invited_by     INTEGER NOT NULL REFERENCES app_user(id),
  created_at     TEXT NOT NULL,
  expires_at     TEXT NOT NULL,
  accepted_at    TEXT,                      -- NULL until redeemed
  accepted_user  INTEGER REFERENCES app_user(id),
  schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_user_invite_email ON user_invite (email);

-- ---- sessions ----------------------------------------------------------
CREATE TABLE user_session (
  -- SHA-256 of the session token, never the token (see the header).
  id             TEXT PRIMARY KEY,
  user_id        INTEGER NOT NULL REFERENCES app_user(id),
  created_at     TEXT NOT NULL,
  expires_at     TEXT NOT NULL,
  last_seen_at   TEXT,
  revoked_at     TEXT,                      -- explicit logout / forced revoke
  user_agent     TEXT,                      -- coarse provenance for the user's own review
  schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_user_session_user ON user_session (user_id, expires_at);

-- ---- per-user source credentials ---------------------------------------
--  The ONE table in this schema that is deliberately not append-only.
--
--  Everything else — plan (0010), coach_note (0012), llm_call (0013) — keeps
--  history on principle (§2.1). A rotated credential is the exception: a
--  superseded refresh token has no analytical value whatsoever, and retaining
--  every historical ciphertext only widens the blast radius of a future key
--  compromise. Secrets are updated in place, on purpose.
CREATE TABLE user_secret (
  user_id        INTEGER NOT NULL REFERENCES app_user(id),
  name           TEXT NOT NULL,             -- 'whoop_refresh_token' | 'mfp_session_cookie' | ...
  ciphertext     BLOB NOT NULL,             -- AEAD output; plaintext never touches this DB
  nonce          BLOB NOT NULL,             -- per-write, never reused with the same key
  key_id         TEXT NOT NULL,             -- which host key encrypted this, for rotation
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (user_id, name)
);
