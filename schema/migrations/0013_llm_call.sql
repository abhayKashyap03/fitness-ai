-- ============================================================
--  Migration 0013 — `llm_call` (token + cost accounting, §8.7, ADR-0017)
--
--  The Anthropic/xAI/Google API is the project's only meaningful running cost,
--  and §8.7 asks for cost control. `coach ask` printed a token count to stderr
--  and then threw it away, so there was no way to answer "what did this month
--  cost", "which command is expensive", or "is prompt caching actually working".
--  §11 forbids optimizing before a measured problem exists — this is the
--  measurement.
--
--  Like `plan` (0010) and `coach_note` (0012) this is CODE-authored primary
--  data, not derived from raw_events: no raw_ref, and it takes no part in the
--  normalize rebuild or its fingerprint (which enumerate the raw-derived tables
--  explicitly, so this table is excluded by construction). Append-only.
--
--  UNIT RATES ARE STORED ON THE ROW, deliberately.
--  Prices change. If cost were recomputed later from a current price table, the
--  cost of a call made in July would silently change in September, and any
--  historical total would be fiction. Recording the rate in effect at call time
--  is the same provenance rule the rest of the schema follows (§2.3): keep what
--  was actually used, resolve nothing destructively.
--
--  RATES ARE NULLABLE, and NULL means "unpriced" — not free (§2.7).
--  The code does not ship a built-in price table: inventing per-token prices
--  would be fabricating exactly the kind of number this project refuses to
--  fabricate, and a wrong rate would flow straight into a "you spent $X" claim.
--  Rates come from configuration; when absent, the call is recorded with its
--  real token counts and reported as UNPRICED. Tokens are always the truth;
--  currency is an optional, user-supplied overlay.
-- ============================================================

CREATE TABLE llm_call (
  id             TEXT PRIMARY KEY,          -- llm:<user>:<created_at>:<seq>
  user_id        INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL,             -- UTC ISO-8601 (when the call ran)
  day_key        TEXT NOT NULL,             -- local day it belongs to (§2.6)

  provider       TEXT NOT NULL,             -- 'google' | 'anthropic' | 'grok' | ...
  model          TEXT NOT NULL,             -- resolved model id, as called
  command        TEXT NOT NULL,             -- 'ask' | 'eval_grounding' | 'web_ask' | ...
  rounds         INTEGER NOT NULL DEFAULT 0,-- agent-loop rounds this spend covers
  ok             INTEGER NOT NULL DEFAULT 1,-- 0 when the call errored (still billable)

  -- token counts, always present (0 is a true zero here: the provider reported it)
  input_tokens          INTEGER NOT NULL DEFAULT 0,
  output_tokens         INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens   INTEGER NOT NULL DEFAULT 0,
  cache_write_tokens    INTEGER NOT NULL DEFAULT 0,

  -- USD per 1M tokens, in effect at call time; NULL = unpriced, NOT free
  price_input_per_mtok        REAL,
  price_output_per_mtok       REAL,
  price_cached_input_per_mtok REAL,
  price_cache_write_per_mtok  REAL,

  schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_llm_call_day ON llm_call (user_id, day_key);
CREATE INDEX idx_llm_call_command ON llm_call (user_id, command);
