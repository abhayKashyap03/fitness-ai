# ADR-0009 — MyFitnessPal direct v2-API adapter + drop the `raw_events.source` CHECK

**Status:** Accepted (2026-07-24) · resolves **D4** · pairs with
[ADR-0010](0010-override-mfp-scraping-ban.md) (the policy sign-off)

## Context

With §12 overridden (ADR-0010), we ingest MFP food directly. D4 asked *how*, and
flagged the blocking schema constraint: `raw_events.source` is a fixed
`CHECK (source IN (...))` with no `myfitnesspal`, and SQLite cannot `ALTER` a
CHECK — admitting a new source means **rebuilding the sacred `raw_events`
table** (§8.5 sign-off).

Recon (reading both community libraries' source) established the wire shape:

- **Auth chain:** browser **session cookie** (durable, ~weeks) →
  `GET /user/auth_token?refresh=true` returns a short-lived **bearer token** +
  `user_id` → v2 calls send `Authorization: Bearer …`, `mfp-client-id:
  mfp-main-js`, `mfp-user-id: …`.
- **Read via JSON, not HTML.** The web client's diary read is a v2 JSON endpoint;
  using it avoids `python-myfitnesspal`'s HTML scrape and its `lxml` dependency
  (which pins Python 3.10–3.12).

## Decision

**1. Adapter (`src/coach/adapters/myfitnesspal/`), stdlib + httpx, no new deps.**
Mirrors the WHOOP adapter exactly:
- `auth.py` — exchanges the pasted `MFP_SESSION_COOKIE` for a bearer token,
  caches it to a gitignored 0600 file, refreshes from the cookie on expiry.
  Cookie/token are never logged (§8.4).
- `client.py` — injectable-transport httpx client, bounded retry with
  `Retry-After`, headers only (never leaks token/cookie in logs or errors).
- `ingest.py` — one raw event per diary **day** (`record_type='diary'`,
  `external_id='mfp:diary:<day>'`), append-only + idempotent; an edited day
  writes a new sibling raw row that the normalizer resolves by newest ingest.
- `normalize/myfitnesspal.py` — pure `raw -> list[FoodEntryRow]`. MFP's diary
  date **is** the local physiological `day_key` (no offset math). Absence is
  absence (§2.7): a macro MFP omits is `NULL`; an empty diary yields **no rows**
  (not-logged), never a synthesized fast.

**2. Drop the `raw_events.source` CHECK entirely** (migration 0006, signed off).
Not "add `myfitnesspal` to the list" — **remove the whitelist**. The CHECK
fought §2.5 ("a new source should be a new adapter, not a schema change"); every
future adapter (BLE, Strava, …) would otherwise repeat this rebuild + sign-off.
Removing it once ends the class of blocker. `food_entry.source` keeps a CHECK
(canonical is disposable, cheaply rebuilt) but gains `'myfitnesspal'`.

**3. Migration runner hosts SQLite's 12-step safely.** Rebuilding `raw_events`
(a table other tables reference by `raw_ref` FK) requires FK enforcement off
during the swap — a no-op *inside* a transaction, so the runner now toggles
`foreign_keys=OFF` around each migration and runs `PRAGMA foreign_key_check` on
the **whole database** before COMMIT, rolling back on any dangling reference.
This is *stronger* than per-statement enforcement (it checks every row, not just
touched ones) and is the documented-correct migration host.

## Alternatives rejected

- **Add only `'myfitnesspal'` to the CHECK.** Keeps the whitelist; every future
  source repeats the rebuild + sign-off. Rejected in favor of removing it.
- **`sources` reference table + FK** (D4 option 2). Achieves the same §2.5 goal
  with more machinery; dropping the CHECK is simpler and sufficient at n=1.
- **HTML scrape / `lxml`** — see ADR-0010.

## Consequences

- Row-preserving rebuild proven with FK-linked data present: row counts
  preserved, `integrity_check` ok, zero FK violations, `raw_ref` joins resolve,
  `idx_raw_lookup` recreated, FK re-enabled after. `--rebuild` remains
  byte-identical (food folded into `canonical_fingerprint`).
- **First LIVE contact is a reconciliation, not a failure (§10.2).** The diary
  read path and the `items`/`nutritional_contents` field names are reconstructed
  from the write payload; a live 400/404 or field mismatch is a one-line fix in
  the adapter + fixture, nothing downstream.
- `food_daily` precedence now ranks a **direct** MFP log above a HealthKit
  **mirror** of the same day (`source_app='myfitnesspal'`) and above `manual`.
- The MFP normalizer clears + rebuilds only its own source slice each run
  (a diary day is an editable collection, unlike 1:1 weight/recovery rows), so
  removed items never orphan.
