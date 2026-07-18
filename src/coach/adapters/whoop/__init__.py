"""WHOOP adapter — the ONLY WHOOP-aware code (CLAUDE.md §2.5).

Adapter A (Cloud API, OAuth 2.0). Works while the membership is active; dies
with it. The recovery slot is generic — a future BLE adapter (Adapter B) slots
in beside this one without touching compute or coaching code.
"""

# WHOOP v2 API surface (https://developer.whoop.com).
API_BASE = "https://api.prod.whoop.com/developer"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"

# offline => we receive a refresh token so the strap keeps syncing unattended.
DEFAULT_SCOPES = (
    "offline",
    "read:recovery",
    "read:cycles",
    "read:sleep",
    "read:workout",
    "read:profile",
    "read:body_measurement",
)
