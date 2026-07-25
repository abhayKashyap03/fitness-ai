"""MyFitnessPal adapter — the ONLY MFP-aware code (CLAUDE.md §2.5).

Nutrition slot, Adapter A (MFP internal v2 JSON API). This talks to MFP's
undocumented, private v2 endpoints — the same ones their web client uses —
authenticated with the user's own browser session cookie. That path is a
deliberate, signed-off override of the original §12 "do not scrape MFP" rule,
for the user's OWN account and OWN data (see docs/adr/0009-myfitnesspal-direct-api.md
and docs/adr/0010-override-mfp-scraping-ban.md).

Consequences we accept, documented so they surprise nobody:
  * It can break without notice — MFP has changed these endpoints before
    (the old /food/diary/{user}/add now 404s). Treat it like the BLE risk (§10.9).
  * First LIVE contact is a reconciliation, not a failure (§10.2): the exact
    diary-read path and payload field names below are our best reconstruction
    from the client's write shape and must be confirmed against a real response.
    They are isolated to THIS module + the pure normalizer's fixture.
"""

# MFP internal API surface (undocumented; observed from the web client).
API_BASE = "https://api.myfitnesspal.com"

# Cookie -> short-lived bearer token exchange. python-myfitnesspal hits this
# same path; it returns {access_token, token_type, expires_in, user_id}.
AUTH_TOKEN_PATH = "/user/auth_token"

# Diary read. RECONCILE ON FIRST LIVE CONTACT (§10.2): this is our best
# reconstruction of the web client's per-day read. If a live call 404s/400s,
# the fix is a one-line change here + the fixture — nothing downstream moves.
DIARY_PATH = "/v2/diary"

# Headers MFP's web client sends; mfp-client-id identifies the JS app.
CLIENT_ID = "mfp-main-js"
