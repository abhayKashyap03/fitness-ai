# Session Log

> Rolling handoff (CLAUDE.md §6.3). Current state + latest session only. Older
> sessions → `docs/sessions/`; `git log` is the real history.

---

## Where the code stands (verified)

- Phases 0–3 complete. WHOOP vertical slice works **live**. **Phase 5 (Apple
  Health, WEIGHT) complete**; **Phase 4 pre-work merged** (tool contract,
  guardrails, grounding harness). 145 tests green; ruff + mypy clean.
- Schema at **v5** (…0004 utc_offset, **0005 source_app** — D3/ADR-0008: adds
  `source_app` + `utc_offset` to weight/food, recreates resolver views; raw
  untouched).
- **PR #5 + #6 + #7 MERGED — all on `main`.** #5/#6 = Phase 5 WEIGHT (T5.0–T5.8 +
  D3). #7 = Phase 4 pre-work: T4.1 tool contract (`coach/tools.py`), §8.6
  guardrails (`compute/guardrails.py`), T4.2 grounding harness authored
  (`coach/grounding.py`; live-model eval gated on Anthropic SDK + API key).
- CLI: `coach db init|status`, `auth whoop`, `ingest whoop`,
  **`ingest healthkit --file`**, `normalize [--rebuild]`, `status --date`, `tdee`.
- **GateGuard disabled** via `.claude/settings.local.json` (`ECC_GATEGUARD=off`)
  — **user-authorized 2026-07-19; do NOT re-flag** the §8.2 tension. File stays
  untracked (machine-local).

---

## Session 2026-07-19 (e) — Phase 4 pre-work while MFP CSV pending

Branch `phase4/coach-layer` (stacked on merged #6). Food-independent Phase-4 work.

- **T4.1** `coach/tools.py` — 5 model-callable tools over Phase-3 compute;
  structured data + provenance + explicit null/insufficient; no prose/math (§2.2).
- **§8.6** `compute/guardrails.py` — code-enforced hard limits (weight-loss-rate
  alert off EWMA trend; 1200 kcal floor). Surfaced as `get_safety_flags`.
- **T4.2** `coach/grounding.py` — faithfulness SYSTEM_PROMPT + fabrication-risk
  scenarios + absence/fabrication helpers. Substrate honesty tested deterministically;
  **live-model eval gated** (Anthropic SDK §6.4 + tokens §8.7 — `run_live_grounding`
  raises, never in pytest).
- 145 tests green; ruff + mypy clean. GateGuard stays off (user-authorized).

## Session 2026-07-19 (d) — Phase 5 WEIGHT built (D3 + T5.3–T5.8)

Branch `phase5/healthkit-weight` (off merged main). One feature commit + docs.

### Done
- **D3 resolved** → ADR-0008 (option 1, per handoff). Migration **0005**:
  non-destructive `source_app` + `utc_offset` columns on `weight_measurement` +
  `food_entry`; resolver views recreated so an OKOK **scale** weigh-in outranks an
  MFP-**mirrored** weight as siblings (§2.3), and two food apps under one source
  stay siblings instead of SUM-double-counting. **raw_events untouched** (§8.5).
- **T5.3** `adapters/healthkit/ingest.py` — body-only raw ingest,
  `source='healthkit'`, deterministic `external_id`, idempotent. Dietary
  **deliberately skipped** (food = MFP CSV; keeps stale 5-day HK food from
  competing with real MFP food later).
- **T5.4/T5.5** `normalize/healthkit.py` — pure body parser: lb→kg, BodyFat %,
  LeanMass; BMI/missing→None (§2.7); `tz_name` NULL (no HKTimeZone on body rows);
  `day_key`/`utc_offset` from startDate offset. **One canonical row per HK record
  (1:1 raw_ref)** — chose this over merging metrics, to keep provenance honest.
- **T5.6** CLI `ingest healthkit`; weight wired into `normalize` (+`--rebuild`
  clears/re-derives `weight_measurement`; fingerprint covers it → byte-identical
  rebuild proven).

### Verified against the REAL export (scratch DB, user DB untouched)
- 1410 body records ingested in ~7s (memory-flat). Normalize → 1084
  `weight_measurement` rows (431 with weight_kg). **298 resolved days,
  2023-07-06 → 2026-07-18.** app split okok 978 / mfp 103 / cronometer 2 /
  health 1 — matches T5.1 recon exactly.
- `coach status --date 2026-07-18` → `weight [healthkit]: 83.19kg (trend
  82.60kg)`. `coach tdee` → correctly "insufficient intake" (food = Phase 6).

## Session 2026-07-19 (c) — nutrition-source diagnosis; Phase 5 replan

Investigated why adaptive TDEE has no intake to calibrate on. **Read-only** — two
scratchpad scripts against the real export; no repo code changed, no commits.

### Finding — HealthKit is NOT a viable food source
- Export is **current** (latest record 2026-07-19; weight/HR/steps live through
  Jul 18–19). Not stale.
- **Dietary data dies 2026-02-12.** Across all 34 `Dietary*` types, only **5 distinct
  logged days ever** exist in the export: MFP 2025-10-24/25/26, Foodnoms 2026-02-11/12.
  Longest consecutive run = **3 days**. No 2-week calibration window.
- Cause: **MyFitnessPal stopped writing to Apple Health after 2025-10-26** (no MFP
  record of any type after that date). Known MFP behavior — Apple Health sync got
  **paywalled behind MFP Premium** ~2024–25; the toggle shows "connected" but silently
  stops. User confirms they logged consistently Feb–June *in MFP* — that history lives
  on MFP's servers, never reached HealthKit.

### Replan (decided with user)
- **Food source = a new MFP CSV adapter**, not HealthKit passthrough. HealthKit stays
  the **weight/body-comp** source (rich, live).
- MFP free-tier Reports export = last 7 days only (empty). Correct path = MFP
  **Privacy Center → "Download My Data"** (full account history, free, CCPA/GDPR).
  **User is submitting that request; the zip arrives ~2026-07-20 afternoon.**
- No scraping (CLAUDE.md §12 / ToS). The user's own data-portability export is the
  sanctioned path — §12 updated to say so.

### New blocker surfaced
- MFP-CSV raw ingest needs `raw_events.source='myfitnesspal'`, which is **not** in the
  fixed CHECK list → widening it rebuilds the sacred `raw_events` (real WHOOP data) →
  §8.5 human sign-off. **Flagged as D4.** (Note: this CHECK-rigidity contradicts §2.5
  "adding a source should be a new adapter file, not a schema change" — a `sources`
  lookup table is the aligned long-term fix; see D4.)

---

## Next session — do in this order

**A. Weight/body-comp — ✅ DONE + MERGED (#5/#6). Phase 4 pre-work ✅ MERGED (#7).**
   Remaining human step: first real ingest into the actual DB — `coach db init &&
   coach ingest healthkit --file apple_health_export/export.xml && coach normalize`
   (verified only against a scratch DB so far). `db init` first — applies 0005.

**B. Food / MFP (starts when the CSV lands — expected ~2026-07-20 PM):**
5. **Recon the MFP CSV first** — headers, date format, meal grouping, units, date
   range, distinct logged days. Do NOT assume columns (the WHOOP-404 lesson). Confirm
   the Feb–June month is present + gap-free.
6. Answer **D4** (raw_events source for MFP). Build `src/coach/adapters/mfp/` — raw
   ingest + pure `food_entry` normalizer (day_key from CSV local date, no zero-fill §2.7).

**C. Open items (need the human):**
- `~/.zshrc` plaintext API keys — flagged, still unrotated.
- Live grounding eval (T4.2) — needs Anthropic SDK (§6.4 sign-off) + API key +
  token spend (§8.7). `run_live_grounding` raises until wired.
- (GateGuard §8.2 — RESOLVED: stays off, user-authorized. No longer an open item.)

### Verified vs unverified
- Verified: weight pipeline end-to-end on the REAL export (1410 records, 298
  resolved days, trend in `status`); Phase 4 tools/guardrails/grounding
  deterministic; 145 tests / ruff / mypy green; idempotent + byte-identical rebuild.
- Unverified: not yet ingested into the user's **actual** DB (scratch DB only) —
  human step. MFP privacy-export contents (not yet received). D4 still open. Live
  model faithfulness (grounding) not yet run.
