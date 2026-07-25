# Session Log

> Rolling handoff (CLAUDE.md §6.3). Current state + latest session only. Older
> sessions → `docs/sessions/`; `git log` is the real history.

---

## Where the code stands (verified)

- Phases 0–5 complete + Phase 4 coach (`ask`) merged. WHOOP slice works **live**.
  MFP food+weight adapter built, diary shape **live-reconciled**; awaiting live
  end-to-end run. **230 tests green; ruff + mypy clean.**
- Schema at **v7** (0006 myfitnesspal food — drops `raw_events.source` CHECK
  entirely §2.5, widens `food_entry.source`; **0007 myfitnesspal weight** —
  widens `weight_measurement.source`, ranks MFP below manual). D4/ADR-0009.
- **`raw_events` rebuild landed WITH sign-off (§8.5)** — row-preserving; proven
  with FK-linked data (row counts preserved, integrity ok, 0 FK violations).
  Migration runner now hosts SQLite's 12-step: FK off during a migration, whole-DB
  `foreign_key_check` before COMMIT (stronger than per-statement enforcement).
- CLI: `coach db init|status|backup|verify`, `auth whoop`, `ingest whoop`,
  `ingest healthkit --file`, **`ingest mfp [--since --until]`**, `normalize
  [--rebuild]`, `status`, `tdee`, `ask`, `eval grounding`, `doctor`, `sync`.
- **GateGuard disabled** via `.claude/settings.local.json` (`ECC_GATEGUARD=off`)
  — **user-authorized 2026-07-19; do NOT re-flag** the §8.2 tension. File stays
  untracked (machine-local).
- **Open item (unchanged):** `~/.zshrc` plaintext API keys flagged, unrotated —
  user's call, do not act.

---

## Session 2026-07-24 (b) — MFP live reconciliation + weight + WHOOP fixes (branch `feat/mfp-adapter`)

Follow-up to the same-day MFP adapter build, driven by the user's live testing.

- **MFP diary param fixed:** `date` → **`entry_date`** (user caught it live — an
  unknown param makes MFP silently return TODAY). `/v2/diary`.
- **MFP diary shape LIVE-RECONCILED** against a real payload: entries are
  per-MEAL aggregates (`type:'diary_meal'`, meal in `diary_meal`, summed
  `nutritional_contents`), NOT per-item. The `items` list also carries non-food
  `exercise_entry`/`steps_aggregate` — normalizer now **filters to diary_meal**
  (folding those in would fabricate calories). Fixture replaced with a
  structurally-real one (synthetic macro values).
- **MFP weight added** (user's call: get weight from MFP, not WHOOP):
  `GET /v2/measurements?type=weight&entry_date=…` → `{item:{value,unit,date,
  updated_at}}`. `client.get_weight`, ingest pulls diary+weight per day
  (`record_type` `diary`+`measurement`), `parse_measurement` (lb/kg/stone→kg;
  day_key exact; measured_at NULL — MFP gives a day not an instant). **Migration
  0007** widens `weight_measurement.source` + ranks `myfitnesspal` BELOW manual
  (user-typed; a real scale always wins). Proven on FK-linked data.
- **WHOOP ingest bug-fixes** (user's edits were broken by a wrong "collection"
  assumption): `client.get_body_measurement` reverted to return the single dict
  (`list()` was keeping the KEYS, dropping the numbers); removed the crashing +
  duplicate `plan` line. Body measurement is a single dateless object — stays
  raw-only, NOT normalized (dateless static value would corrupt the EWMA trend).
- Removed the user's debug `print` in `ingest.py` during the weight restructure.

**Verified:** 230 green, ruff + mypy clean; migrations 0006+0007 proven on
FK-linked data; schema now **v7**. **Next:** live `coach ingest mfp` end-to-end
(diary + weight) once usage resets; watch the first real weight payload (the
`item` vs `items` shape — normalizer tolerates both).

---

## Session 2026-07-24 (a) — MyFitnessPal direct adapter (branch `feat/mfp-adapter`)

User wanted off the daily manual-export treadmill (Apple Health / MFP CSV) and
asked about `python-myfitnesspal` / `myfitnesspal-mcp-python`. Recon: both hit
MFP's **private** API (HTML scrape and/or internal v2 JSON) with the browser
session — the path §12 banned. Laid out the trade-off; **user signed off to
override §12** for their own account (ADR-0010) and to **drop the
`raw_events.source` CHECK entirely** rather than extend it (ADR-0009).

Built (stdlib + httpx, **zero new deps**; mirrors WHOOP adapter):
- `adapters/myfitnesspal/`: `auth.py` (cookie → bearer, cached 0600, refresh),
  `client.py` (injectable transport, bounded retry, no token/cookie leak),
  `ingest.py` (one raw event per diary day, idempotent, edit → sibling).
- `normalize/myfitnesspal.py`: pure `raw → FoodEntryRow[]`; MFP date = local
  `day_key`; absence stays NULL; empty diary = no rows (not a fast). Runner clears
  + rebuilds the MFP slice each run so edited-away items don't orphan.
- `store/canonical.py`: `upsert_food` + `food_id`; food folded into the rebuild
  fingerprint. Migration **0006** (raw_events CHECK drop + food_entry widen +
  view precedence). Runner hardened for parent-table rebuilds (see above).
- Config `MFP_SESSION_COOKIE` + `require_mfp()`; `.env.example`; CLI `ingest mfp`.
- Tests: `test_mfp_normalize`, `test_mfp_adapter`, `test_mfp_pipeline`, + 2 runner
  guard tests (dangling-FK rollback; parent rebuild preserves children).

**Verified:** 223 green, ruff + mypy clean; migration proven on FK-linked data in
a scratchpad; CLI smoke (db init → v6, ingest mfp clean-errors without a cookie,
doctor lists myfitnesspal). **NOT verified:** live MFP call — the v2 diary READ
path + field names are reconstructed and **reconcile on first live contact**
(§10.2), isolated to the adapter + fixture.

**Next (human):** log in at myfitnesspal.com, paste the Cookie header into
`.env` as `MFP_SESSION_COOKIE`, run `coach ingest mfp --since <date>` →
`coach normalize` → `coach status`. Expect a first-contact field reconciliation.

---

## Session 2026-07-20 (b) — revamp session 2: `coach ask` (branch `revamp/coach-ask`)

Phase 4 completed: the coach actually coaches. **Zero new dependencies** —
stdlib urllib Messages client (§6.4; no cloud SDK), injectable transport so
tests never touch the network (§6.2), bounded retries honoring retry-after,
prompt caching on SYSTEM_PROMPT (§8.7), secrets never logged (§8.4).

- `coach/llm.py` client + `coach/agent.py` bounded loop (MAX_ROUNDS=8) over the
  deterministic tool contract; refusal/pause_turn/max_tokens/unknown-tool/round-
  exhaustion all explicit.
- `run_live_grounding` implemented (was gated stub) — per-scenario fresh
  in-memory DB, scored via admits_absence + fabricated_numbers (date-tolerant).
- CLI: `coach ask "…" [--show-tools]`, `coach eval grounding` (exit 1 on any
  fabrication). `COACH_MODEL` default claude-opus-4-8, overridable (§8.7).
- Intentional test change (§6.2): `test_live_runner_is_gated` (asserted
  NotImplementedError) replaced — the runner is now the feature; offline
  coverage lives in `test_coach_agent.py` (fake transport).
- 176 tests green; ruff + mypy clean.
### Provider-agnostic follow-up (same branch, user request: no paid credits)
- `llm.py` → `coach/llm/` package mirroring `adapters/`: canonical conversation
  shape in `base.py`, one module per vendor (`google.py`, `anthropic.py`),
  `PROVIDERS` registry + `build_provider()`. Agent loop never sees a vendor
  field name (§2.5) — switching providers is a config value.
- **Default provider = Google Gemini** (`gemini-2.5-flash`, real free tier).
  Anthropic kept as an option. `COACH_LLM_PROVIDER`, `GOOGLE_API_KEY`,
  `COACH_MODEL` (empty ⇒ provider default).
- Gemini translation notes: role `model` not `assistant`; functionResponse
  matched by **name** (no call id — canonical ToolResult carries both);
  functionDeclarations need an OpenAPI subset (types upper-cased, `minimum`
  stripped or 400); SAFETY/RECITATION ⇒ canonical refusal; implicit caching.
- Keys in headers only, never URL params (§8.4) — asserted by test.
- Agent-loop tests **parametrized across both providers** — identical behavior
  is the proof of the abstraction. 200 tests green; ruff + mypy clean.

- **Human steps:** get a free key at aistudio.google.com/apikey → `GOOGLE_API_KEY`
  in `.env`; run `coach doctor`, then `coach ask "how am I doing?" --show-tools`
  and `coach eval grounding`. Session 3 (BLE ADR, gap-aware EWMA, HRV validation
  harness, README) still queued.

## Session 2026-07-20 (a) — revamp session 1: correctness + friction (branch `revamp/core-upgrades`)

User granted full creative latitude (reviews PR before merge). Three-session plan;
this is session 1 of 3. Sessions 2–3 queued: (2) `coach ask` agent loop via
stdlib-only Anthropic REST client (no SDK dep — §6.4 stays pure) + live grounding;
(3) BLE recon ADR, gap-aware EWMA (+ADR), HRV-validation harness, sleep table, README.

### Done (session 1)
- **Bug fix:** HealthKit mass conversion was unconditionally lb — a kg-unit record
  would double-convert. Now unit-aware (lb/kg/g/oz/st; unknown unit -> row skipped,
  §2.7); BodyFat 0–1-fraction guard.
- **Migration atomicity:** executescript's implicit COMMIT left partial DDL on
  mid-file failure. Now statement-split + one explicit transaction per migration.
- **resp_rate enrichment:** recovery rows carry resp_rate_bpm joined from raw WHOOP
  sleep by sleep_id (parse_recovery stays pure; synthetic sleep fixture, labeled).
- **CLI:** `coach sync` (incremental auto-since WHOOP + HK if present + normalize),
  `coach doctor`, `coach db backup|verify`, `--json` on status/tdee (emits the
  coach tool-layer dicts — one contract), `--date`/`--end` default to today in
  COACH_HOME_TZ. `ingest whoop --since` now optional (incremental).
- **Multi-agent review over the diff** (3 lenses + adversarial verify): 13 raw
  findings, 4 confirmed empirically + 5 self-verified. All fixed: doctor/verify
  no longer crash on an unmigrated DB (doctor now read-only), write-path
  commands auto-migrate, splitter handles statements sharing a line + rejects
  in-file BEGIN/COMMIT, atomic .part backup, per-type auto_since watermark ('Z'
  form), deterministic resp_rate sibling pick, weight_skipped surfaced.
- 161 tests green; ruff + mypy clean.
- **User's real DB confirmed live:** doctor shows whoop_api 354 + healthkit 1410
  raw rows (user ran the ingest themselves).

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
