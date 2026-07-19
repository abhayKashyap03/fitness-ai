# Apple Health export — structural recon (T5.1)

> **Aggregate statistics and structure only** (CLAUDE.md §6.3). No personal
> values, no dates tied to events, no record dumps. Regenerate with
> `scratchpad/hk_recon.py` (streaming, memory-flat) against the real export.
> The export itself is gitignored and never committed.

## Overview

- `apple_health_export/` ≈ **1.1 GB**; the payload is `export.xml` (plus
  `export_cda.xml`, a clinical-format duplicate we ignore, and
  `workout-routes/*.gpx`).
- **~1,691,859 `<Record>` elements**, date range **2018 → 2026**.
- **Must stream** (`xml.etree.ElementTree.iterparse` + clear). Element counts:
  `Record` 1.69M, `MetadataEntry` 1.30M, `InstantaneousBeatsPerMinute` 122k,
  `Workout` 560, `ActivitySummary` 1027, plus small singletons.
- iterparse pitfall confirmed: **do not `.clear()` a `MetadataEntry` on its own
  end event** — the parent `Record` must read its children first. Clear only
  `Record`/`Workout`/top-level containers. (This bug silently blanked all
  metadata on the first recon pass.)

## Record shape

`<Record type=… sourceName=… sourceVersion=… unit=… creationDate=… startDate=…
endDate=… value=…>` with zero or more `<MetadataEntry key=… value=…/>` children.
Dates look like `YYYY-MM-DD HH:MM:SS ±HHMM`.

## Sources (by record count)

| sourceName | ~count | relevance |
|---|---|---|
| Abhay's Apple Watch | 1.43M | activity/HR — **out of scope** (WHOOP covers) |
| Abhay's iPhone | 231k | steps/audio/etc — out of scope |
| WHOOP | 31k | already ingested via the WHOOP API — **skip here** |
| OKOK·International Version | 1.6k | **smart scale** — body composition ✅ |
| Foodnoms | 408 | food logger — **nutrition** ✅ |
| MyFitnessPal | 238 | food logger + weight — **nutrition + weight** ✅ |
| Cronometer / Health / Strava / Clock / iPhone(2) | <60 each | trace |

## Nutrition (our target) — **SPARSE**

The headline finding: **almost no nutrition reaches the export.**
`DietaryEnergyConsumed` totals **33 records across ~5 logged days** (MyFitnessPal
9, Foodnoms 24). The coach's nutrition arm will be data-starved until logging
consistently reaches Apple Health — flag for the human.

- **Energy unit `Cal`** (Apple "Cal" = **kilocalorie**). Macros in **`g`**;
  micros in `mg`/`mcg`; water in `mL`.
- **Macros are reliable when present:** of the energy-days, **100%** also carry
  protein, carbs, and fat. So an energy record implies its macros exist.
- **Granularity differs by source:**
  - **MyFitnessPal ≈ 4 records/day per nutrient → per-MEAL.** Carries `meal` /
    `Meal` metadata (meal name) + `HKFoodType` (food/label) + `HKExternalUUID`.
  - **Foodnoms ≈ 18–22 records/day per nutrient → per-ITEM.** Richer micros;
    `HKFoodType` + `HKExternalUUID`, no meal key.
- Dietary metadata keys (present on all 408 dietary records): `HKFoodType`,
  `HKExternalUUID`, `HKWasUserEntered`, `HKTimeZone`, `HKMetadataKeySyncIdentifier`,
  `HKMetadataKeySyncVersion`; MFP-only: `meal`/`Meal`.
- **Two food loggers coexist** (MFP + Foodnoms). Both can write the same day →
  must resolve at read time (§2.3), consistent with the existing `food_daily`
  precedence view.

## Body composition (our other target) — **RICH**

| type | ~count | unit | sources |
|---|---|---|---|
| BodyMass | 431 | **`lb`** | OKOK 326, MyFitnessPal 103, Health 1, Cronometer 1 |
| BodyFatPercentage | 327 | `%` | OKOK 326, Cronometer 1 |
| BodyMassIndex | 326 | `count` | OKOK |
| LeanBodyMass | 326 | **`lb`** | OKOK |
| Height | 327 | (n/a here) | OKOK |

- **Weight is in POUNDS** (`lb`) — the normalizer must convert lb→kg (canonical
  `weight_kg`). BodyFat is a **percent** (0–100), not a fraction.
- **Multiple sources write BodyMass** (OKOK scale + MyFitnessPal + …) →
  **sibling rows, resolve at read** (§2.3). Exactly the case T5.4 anticipates.
- OKOK writes **multiple weigh-ins per day** (median 1, up to 5). The existing
  `weight_resolved_daily` view (earliest-of-day + source precedence) handles this.
- Body records carry **almost no metadata** (only sync keys) — crucially **no
  `HKTimeZone`** and **no `HKExternalUUID`**.

## Timezone (ADR-0006 / T5.5) — **real IANA present, but only on some records**

- **`HKTimeZone` metadata carries true IANA zone names** — 3 distinct in the
  export: a home US-Eastern zone plus two international travel zones (validated:
  all contain `/`, i.e. `Region/City`). This is the IANA source ADR-0006
  anticipated HealthKit could provide.
- **But it is on dietary (and some activity) records only — NOT on body records.**
  So HealthKit can backfill `tz_name` for **food** rows, not weight rows.
- ⚠️ **The `startDate` UTC offset is unreliable in this export** — every
  nutrition+body record's offset serializes to a **single value** (the export
  device's current offset), even for records whose `HKTimeZone` says Tokyo/Kolkata.
  **Therefore `day_key` for dietary rows must be derived from the instant +
  `HKTimeZone`, not from the `startDate` offset** (which would misplace travel
  days near midnight). For body rows (no zone), fall back to the offset / home tz.
- Cross-source backfill (use HealthKit's IANA to fill WHOOP rows on the same
  `day_key`) is a judgment call → **DECISIONS_NEEDED**, not implemented (T5.5).

## Anomalies / gotchas

- Nutrition is nearly empty (5 days) — biggest surprise; not a bug, a data gap.
- Weight in `lb`; BodyFat in `%` (0–100) — unit handling is load-bearing.
- Multi-source BodyMass and multi-source dietary → sibling rows both.
- `startDate` offset is normalized/unreliable; `HKTimeZone` is the trustworthy
  zone signal and is absent on body records.
- `HKExternalUUID` exists for dietary (a stable id) but **not** for body — so a
  single external_id strategy can't rely on it; derive deterministically from
  `(type, sourceName, startDate, value)` and use `HKExternalUUID` only if we
  choose to (documented in T5.3).

## Implications for the build

- **T5.2 parser:** stream; yield only `Dietary*` + Body types; skip everything
  Apple-Watch/iPhone/WHOOP-sourced. Read `MetadataEntry` before clearing.
- **T5.3 raw:** `source` = `healthkit:myfitnesspal`, `healthkit:foodnoms`,
  `healthkit:okok`, etc. (needs a migration to widen the `raw_events` source
  CHECK). `external_id` deterministic from `(type, sourceName, startDate, value)`.
- **T5.4 normalizers:** lb→kg, BodyFat %→ store as %; dietary `day_key` from
  `HKTimeZone`; keep entries (don't pre-aggregate); no zero-fill (§2.7).
- **T5.5:** populate `tz_name` (IANA) for dietary rows from `HKTimeZone`; leave
  body rows' `tz_name` NULL; cross-source WHOOP backfill → DECISIONS_NEEDED.
