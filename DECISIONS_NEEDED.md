# Decisions Needed

> Claude Code appends here when it hits a **one-way door** it shouldn't decide
> alone. Each entry: what's blocked, the options, its recommendation, why it
> matters. Answer these first thing — they gate real work.

## D1 — WHOOP gives a UTC offset, not an IANA timezone name

**Blocked / at-risk:** CLAUDE.md §2.6 requires every timestamp to carry the
**IANA timezone name** (e.g. `America/New_York`). The WHOOP v2 API does **not**
provide one — records carry only a UTC `timezone_offset` string like `-05:00`.
There is no reliable offset→IANA mapping (many zones share `-05:00`; DST makes
it ambiguous).

**What I did (not a one-way door, so I proceeded):**
- `day_key` (the load-bearing value) is derived **correctly** from
  `start`/`created_at` + the WHOOP `timezone_offset` — travel-proof and exact.
- `tz_name` for WHOOP rows stores the **offset string** (`-05:00`) with the true
  offset preserved, rather than a fabricated IANA name. No guessing.

**Options for you to choose:**
1. **(Recommended)** Accept offset-only for WHOOP; treat `tz_name` as
   "zone-or-offset". IANA is only truly available from phone-sourced data
   (HealthKit) later, which we can cross-reference. Costs nothing, invents nothing.
2. Infer IANA from the offset + your known travel calendar. Accurate but needs a
   manual location log you'd have to maintain.
3. Always store your configured `COACH_HOME_TZ`. **Rejected** — wrong whenever you
   travel, which is precisely the case §2.6 exists to handle.

**Why it matters:** day boundaries are already correct (via offset), so this does
NOT threaten correctness today. It only affects future features that need the
named zone (e.g. "what local time do you usually train"). Flagging per §2.6's
status as a core principle.
