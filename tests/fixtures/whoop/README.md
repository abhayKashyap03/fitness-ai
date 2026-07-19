# WHOOP fixtures — REAL (scrubbed)

**These are real WHOOP v2 API payloads**, recorded from a live account and
scrubbed per CLAUDE.md §6.2: `user_id` is replaced with `1`. No emails, tokens,
or device serials appear in these record types (the profile endpoint, which
carries an email, is deliberately not fixtured). Record UUIDs and `cycle_id`s are
per-record identifiers, not account-identifying, and are kept for realism.

Regenerate with `scratchpad/export_fixtures.py` after a fresh ingest.

Files:
- `recovery_page1.json` / `recovery_page2.json` — paginated recovery collection
  (page 1 has `next_token`). **One exception to "real":** the `PENDING_SCORE`
  record in page 1 is **synthetic** — no unscored recovery existed in the
  recorded window, but the unscored path must be tested. It is the only
  fabricated record here.
- `cycle_page1.json` — real cycles, including `+09:00` (Japan) and `-10:00`
  (Hawaii) offsets, used to supply recovery's timezone offset.
- `workout_page1.json` — real workouts: a `walking` (sport_id 63) and a
  `swimming` (sport_id 33) session. Confirms `sport_name`-based mapping and the
  real `zone_durations` (plural) score field.
- `workout_timezone_jump.json` — **regression fixture.** A real Hawaii walk that
  starts `2026-06-01T06:11Z` at `-10:00` → local **2026-05-31**. Locks in the
  day-boundary rule: a workout's `day_key` follows its local offset, not UTC.
- `body_measurement.json` — real body measurement (no identifiers).

First contact with these real payloads corrected three assumptions the earlier
synthetic fixtures had encoded: `sport_id 33 = swimming`, the score field is
`zone_durations` (not `zone_duration`), and workouts carry a `v1_id`.
