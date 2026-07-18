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

---

## D2 — Calories-burned source precedence (T3.4) 🔒

**Blocked / deferred:** When WHOOP, Apple Watch, and gym equipment all report
calories for overlapping activity, which wins? These disagree 30%+ and this is a
one-way-door policy that shapes the TDEE "calories out" comparison.

**Not urgent yet:** today WHOOP is the *only* calorie source, so there is no live
conflict — `daily_status` already counts each workout once per `session_group_id`
using a documented source rank (`whoop_api > whoop_ble > other`). Nothing is
being double-counted or silently dropped now.

**Recommendation (for when a 2nd source lands):**
1. **(Recommended)** Do NOT let wearable "calories out" drive anything important.
   The adaptive TDEE (ADR-0005) already ignores wearable kcal by design — weight
   trend + intake is the trustworthy signal. Treat all wearable calorie numbers
   as *advisory only*, and when sources conflict, display the range rather than
   pick a winner.
2. If a single number is required for a per-workout view, prefer the
   chest/strap-based estimate (WHOOP) over wrist optical (Apple Watch) over
   machine estimates, in that order — but label it clearly as approximate.

**Why it matters:** picking a precedence and baking it into compute is hard to
unwind. Since it doesn't affect correctness today and genuinely depends on your
judgment about which device you trust, I did not guess — flagging per T3.4's own
instruction.
