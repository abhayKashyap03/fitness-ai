# TASKS — Phase 5: Apple Health ingestion (nutrition + body composition)

> Append this into `TASKS.md`. Work in order. Commit after each task.
> Read `CLAUDE.md` first — all principles apply, especially §2.1 (raw is
> sacred), §2.7 (absence is absence), §6.2 (fixtures), §8.4 (secrets).

**Context:** MyFitnessPal cannot be integrated directly — its API is closed to
new developers (CLAUDE.md §12). Instead, MFP writes nutrition into Apple Health,
and the smart scale writes body composition there too. An Apple Health export
(`apple_health_export/`, repo root) is therefore our nutrition + weight source.

WHOOP does **not** provide a weight time series (its body-measurement endpoint
returns current profile values only), so Apple Health is the *only* source for
weight and body fat.

---

## T5.0 — Protect the export file 🔒 **DO THIS FIRST**

`apple_health_export/` contains the user's complete personal health history. Before any
other work, before any commit:

- Ensure `.gitignore` covers `apple_health_export/`
- Run `git status` and confirm the export does not appear as untracked
- If it is already tracked or committed, **STOP and flag it prominently** in
  `SESSION_LOG.md` — do not attempt history rewriting (CLAUDE.md §6.1)

**Done when:** `git check-ignore apple_health_export/` succeeds.

---

## T5.1 — Reconnaissance: understand the export before writing a parser

**No adapter code until this is complete.** The whole point of the WHOOP 404
lesson: observe the real structure rather than assume it.

Explore `apple_health_export/` and produce `docs/healthkit-export-notes.md` containing:

- Overall file size, total record count, date range covered
- **Every distinct `type` value present**, with counts — the full inventory, not
  just the ones we want
- **Every distinct `sourceName`**, with counts (expect MyFitnessPal, the scale
  app, Apple Watch, iPhone, possibly WHOOP)
- For each record type we care about, a **structurally representative sample**
  with attributes documented: `type`, `sourceName`, `sourceVersion`, `unit`,
  `creationDate`, `startDate`, `endDate`, `value`, plus any nested
  `<MetadataEntry>` keys
- Nutrition types to inventory specifically: `HKQuantityTypeIdentifierDietary*`
  (energy consumed, protein, carbohydrates, fat total, fiber, sugar, sodium, and
  whatever else is present)
- Body types: `HKQuantityTypeIdentifierBodyMass`, `BodyFatPercentage`,
  `BodyMassIndex`, `LeanBodyMass`
- **Granularity findings — important:** does MyFitnessPal write one record per
  food item, per meal, or per day? Are multiple records present per day for the
  same nutrient? Do records carry meaningful names/metadata or only values?
- **Timezone findings:** do records carry a `HKTimeZone` metadata entry or any
  IANA zone name? If so, this is the source that can backfill `tz_name` per
  ADR-0006. Report exactly what is available — do not infer.
- Any anomalies: duplicate records, overlapping sources for the same metric,
  gaps, obviously wrong values

⚠️ **§6.3 applies:** the notes file must contain **aggregate statistics and
structural description only** — no personal values, no dates tied to identifiable
events, no full record dumps. Describe the shape, not the contents. Sample values
should be obviously anonymized or rounded.

**Done when:** the notes file lets a reader understand the export's structure
without opening the export.

---

## T5.2 — Streaming XML parser

The export is large — **do not load it into memory.** Use
`xml.etree.ElementTree.iterparse` (or equivalent) with element clearing.

- `adapters/healthkit/parser.py` — streams records, yields only the types we
  care about (nutrition + body composition)
- Explicitly **ignore** workouts, heart rate, steps, sleep for this session
  (WHOOP already covers those; see "Out of scope" below)
- Handle malformed/partial records gracefully — log and skip, never crash the
  whole import
- Preserve `sourceName` — it becomes our `source` value

**Done when:** parsing the full export completes in reasonable time with flat
memory usage, and record counts match the T5.1 inventory.

---

## T5.3 — Raw ingestion

Per §2.1, records go into `raw_events` **verbatim** before any normalization.

- `source` values derived from `sourceName`, normalized to our enum style
  (e.g. `healthkit:myfitnesspal`, `healthkit:<scale-app>`) — extend the
  `raw_events` source CHECK constraint via a new migration if needed
- `record_type` from the HealthKit type identifier
- `external_id` — HealthKit records have no stable ID; derive a deterministic
  one from `(type, sourceName, startDate, value)` and document the choice
- **Idempotency is critical here.** Exports overlap heavily — the user will
  re-export monthly and re-import the same months. Re-importing must not
  duplicate.

**Done when:** importing the same export twice leaves `raw_events` count
unchanged, proven by test. Importing two genuinely overlapping exports produces
the union, not duplicates.

---

## T5.4 — Normalizers → `food_entry` and `weight_measurement`

Pure functions, no I/O (§2.1). Schemas already exist (migrations 0002/0003).

- Map dietary records → `food_entry`; body records → `weight_measurement`
- Unit conversion handled explicitly and tested (kcal/kJ, kg/lb, % vs fraction)
- **§2.7 is load-bearing here:** a day with no dietary records is *not logged*,
  not zero. Do not synthesize zero rows. Do not forward-fill weight.
- Multiple records per day per nutrient: sum them into the day's total via the
  existing `food_daily` view — do **not** pre-aggregate at write time (entries
  are the fact, totals are the view; ADR-0002)
- If both MyFitnessPal and the scale app write `BodyMass`, both rows are kept as
  siblings (§2.3) and resolved at read time

**Done when:** `coach normalize` populates both tables; `--rebuild` is
byte-identical; unit tests cover each mapping including missing/partial macros.

---

## T5.5 — Timezone backfill (conditional on T5.1 findings)

If and only if T5.1 found real IANA zone data in the export:

- Populate `tz_name` for HealthKit-sourced rows
- Consider whether it can backfill `tz_name` for WHOOP rows on the same
  `day_key` — **flag this as a proposal in `DECISIONS_NEEDED.md` rather than
  implementing it**, since cross-source inference is a judgment call

If no IANA data exists, record that finding and leave `tz_name` NULL per
ADR-0006. **Do not infer a zone from an offset.**

---

## T5.6 — CLI

```
coach ingest healthkit --file <path>     # accepts .xml or .zip
```

- Sensible progress output for a large file
- Clear error if the file is missing or malformed
- Reports inserted/skipped counts per record type, matching the WHOOP ingest
  output style

**Done when:** the command runs end to end on the real export.

---

## T5.7 — Verification against real data

- `coach status --date <a-day-with-food>` shows real calories, macros, and weight
- `coach status --date <a-day-without-food>` still reports NOT LOGGED correctly
- `coach tdee` runs and produces an estimate (it now has weight + intake)
- Spot-check a few days against what the user sees in MyFitnessPal — record the
  comparison in `SESSION_LOG.md` so the human can verify

**Done when:** the daily rollup reflects reality and TDEE has real inputs.

---

## T5.8 — Fixtures

- Create **small, scrubbed, synthetic-but-structurally-real** fixtures from the
  observed export format (§6.2)
- Scrub all personal values; a few dozen records is plenty
- **Never commit any slice of the real export**
- Include edge cases found in T5.1: multiple records per day, missing macros,
  overlapping sources for the same metric, unit variations

**Done when:** the test suite covers the HealthKit path without touching the
real export.

---

## Out of scope for this session

- HealthKit workouts, heart rate, steps, sleep — WHOOP covers these. Ingesting
  workouts from a second source would exercise `session_group_id` dedup for the
  first time, which is worth doing **later, deliberately**, not as a side effect.
- Automated export tooling (auto-export apps) — manual export is fine for now.
- Phase 4 coach layer — still design-gated.
- Anything in CLAUDE.md §11.

---

## End-of-session checklist

Per CLAUDE.md §7: commit everything, update checkboxes, keep `SESSION_LOG.md`
current *continuously*, ensure `DECISIONS_NEEDED.md` reflects open questions,
confirm `pytest` + `ruff` + `mypy` green on the final commit.

**And verify one more time that `apple_health_export/` was never staged or committed.**
