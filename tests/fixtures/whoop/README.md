# WHOOP fixtures — SYNTHETIC

**These payloads are hand-authored to match the documented WHOOP v2 API shape.
They are NOT recorded from a live account.** First contact with real API data is
therefore an *expected reconciliation* — if a field name or nesting differs,
update the adapter/normalizer and re-record real fixtures here.

Files:
- `recovery_page1.json` / `recovery_page2.json` — paginated recovery collection
  (page 1 has `next_token`; includes one `SCORED` and one `PENDING_SCORE` record).
- `workout_page1.json` — workouts, including one that crosses local midnight
  (tests day-boundary handling via `timezone_offset`).
- `body_measurement.json` — single body-measurement object (not paginated).
