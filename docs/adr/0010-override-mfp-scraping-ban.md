# ADR-0010 — Override the §12 "do not scrape MFP" ban for the user's own account

**Status:** Accepted (2026-07-24) · user sign-off in-session · supersedes part of
CLAUDE.md §12 · pairs with [ADR-0009](0009-myfitnesspal-direct-api.md)

## Context

CLAUDE.md §12 states MyFitnessPal's API is "closed; scraping violates ToS — do
not," and designates the **Privacy Center "Download My Data"** CSV export as the
only sanctioned nutrition path. That path works but is **occasional**: each
export is a manual request that takes ~a day to arrive. The user logs food in
MFP **daily** and wants it to flow into the coach without a manual export (or an
Apple Health re-export) every day — the CSV cadence can't serve that.

The user evaluated two community libraries (`python-myfitnesspal`,
`myfitnesspal-mcp-python`) and asked to connect directly. Both reach MFP's
**private** API: `python-myfitnesspal` scrapes HTML; the MCP writes via MFP's
internal v2 JSON API using the browser session token. Either way this is the
private-API/scraping path §12 forbids.

## Decision

**Override §12 for the user's own account and own data only.** Ingest MFP food
directly from its internal v2 JSON API, authenticated with the user's own
browser session cookie (see ADR-0009 for the technical shape). This is a
deliberate, explicit, in-session sign-off — not a silent deviation.

The override is **scoped and conditional**:

- **Single user, own data.** This is an n=1 personal tool reading the user's own
  logged food from their own account. It is not a product feature, not
  multi-user, and ships no scraper anyone else runs.
- **Read-only.** We ingest the diary; we never write to MFP.
- **No credential automation.** No `browser_cookie3`, no headless login, no
  password handling. The user pastes their own session cookie into `.env`.

## Risks accepted (documented so they surprise nobody)

1. **ToS.** Using the private API is contrary to MFP's terms. The user accepts
   that risk for their own account; the account and data are theirs.
2. **Fragility / maintenance rot (§10.9).** MFP can change these undocumented
   endpoints without notice and has before (the old `/food/diary/{user}/add`
   now 404s). Treated like the BLE risk: isolated to one adapter, first live
   contact is a reconciliation (§10.2), breakage is expected and non-fatal to
   the rest of the spine.
3. **Auth churn.** The session cookie expires (~weeks); ingest then 401s and the
   user re-pastes it. No silent lockout — the error says exactly what to do.

## Alternatives rejected

- **Keep §12, CSV only.** Doesn't meet the daily-friction requirement the user
  raised; rejected by the user after the trade-off was laid out.
- **`browser_cookie3` auto-steal of the cookie.** Adds a heavy dependency (§6.4)
  and reads the browser's cookie store — more invasive for no real gain over a
  pasted cookie. Rejected.
- **HTML scraping via `python-myfitnesspal`.** Pulls in `lxml` (pins Python
  3.10–3.12, conflicts with the 3.11+ floor / 3.14 dev interpreter) for a
  brittle HTML path when the JSON v2 API needs neither. Rejected (ADR-0009).

## Consequences

- CLAUDE.md §12's MFP row and §9 are updated to point here; §12's general
  "don't scrape third-party data / don't automate logins" posture stands for
  everything else.
- The nutrition slot now has two adapters: this direct v2-API adapter (Adapter
  A, fragile, daily) and the still-valid CSV privacy export (a manual, sanctioned
  backfill). They write sibling raw rows; nothing about the CSV path is removed.
