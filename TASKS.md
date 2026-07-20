# TASKS.md — Ordered Work Queue

**Read `CLAUDE.md` first.** Work these **in order**. Mark `[x]` as you complete
them and **commit after each one**.

Legend: 🔒 = one-way door, think hard · 🧑 = needs the human · ⏭️ = skippable if blocked

---

## Phase 0 — Design decisions to lock first 🔒

Do these **before** writing implementation code. They're the highest-thinking,
lowest-token work and everything downstream depends on them.

### [x] T0.1 — Design the `food` canonical shape 🔒
Design and add to `schema/` a canonical table for nutrition entries.

Must handle: individual food entries **and** daily rollups; missing/partial
macros; multiple sources (HealthKit passthrough, manual, future food DB);
the same `source`/`raw_ref`/`user_id`/`day_key`/`tz_name` provenance pattern as
`recovery` and `workout`.

Think about: is a day's macro total a stored row or a computed view? (Recommend:
**computed view** — entries are the fact, totals are derived. But justify your call.)
Also: how do you represent "logged nothing yet today" vs "genuinely ate nothing"?
These must be distinguishable — the coach's advice depends on it.

**Done when:** DDL written, an ADR explains the entry-vs-rollup decision, and a
`food_daily` view returns per-day totals.

### [x] T0.2 — Design the `weight` / body-composition canonical shape 🔒
Simpler. Handles: scale weight, body fat %, lean mass; multiple sources;
multiple readings per day (which wins? — recommend: keep all rows, resolve at
read time, consistent with §2.3).

**Done when:** DDL written + a `weight_trend` view exposing a smoothed trend
(exponentially-weighted moving average is the standard approach for cut/bulk
work — raw daily weight is too noisy to steer on).

### [x] T0.3 — Write ADR-0001 documenting the source-row provenance pattern
Capture *why* sibling rows + read-time resolution beats merge-on-write, and why
objective measurements are stored separately from composite scores. This is the
keystone pattern; future-you needs the reasoning.

---

## Phase 1 — Repo foundation

### [x] T1.1 — Scaffold the project
Create the structure from `CLAUDE.md` §6. Set up `pyproject.toml`, dependency
management, `ruff` config, `pytest` config, `.gitignore` (**`.env` must be
ignored in the very first commit**), and a `README.md` (what it is, how to set
up, how to run).

**Done when:** `pytest` runs green (zero tests is fine), `ruff check` passes.

### [x] T1.2 — Config & secrets handling
A small config module loading from `.env` → typed settings object. Fail loudly
with a clear message if a required var is missing. Never log secret values.
Write `.env.example` with every var documented and **no real values**.

**Done when:** importing config with a missing required var raises a clear,
actionable error naming the variable.

### [x] T1.3 — Database bootstrap + migrations
Apply `schema/canonical_schema_v0.1.sql` plus the Phase 0 additions. Implement a
minimal, explicit migration mechanism (a numbered `schema/migrations/` directory
and a `schema_version` table is plenty — **do not add Alembic**).

**Done when:** `coach db init` creates the DB from scratch; running it twice is
safe (idempotent); `coach db status` reports the current version.

---

## Phase 2 — WHOOP vertical slice (the core of the night)

### [x] T2.1 — WHOOP OAuth 2.0 flow
Implement authorization-code flow. Tokens stored locally (in the DB or a
gitignored file), auto-refresh on expiry, clear error when re-auth is needed.

🧑 Requires real client credentials. If `.env` has them, verify end to end. If
not, build it fully, unit-test the token/refresh logic against mocks, and mark
"live verification pending."

**Done when:** `coach auth whoop` completes a full login and persists a usable token.

### [x] T2.2 — WHOOP API client
Typed client for: recovery, cycles/strain, sleep, workouts, body measurements.
Handle pagination, rate limits (~100 req/min — implement backoff), and transient
5xx retry. Log requests without leaking tokens.

**Done when:** each endpoint has a method, tested against fixtures.

### [x] T2.3 — Raw ingestion
`coach ingest whoop --since <date>` writes **verbatim** payloads to `raw_events`.
Idempotent: re-running the same window creates no duplicates (that's what
`payload_hash` is for). Record `ingested_at`, `recorded_at`, `external_id`.

**Done when:** running the same ingest twice leaves the row count unchanged, and
a test proves it.

### [x] T2.4 — Normalizers: raw → canonical
Pure functions `raw_event -> canonical row(s)` for `recovery` and `workout`.
No I/O inside them — that's what makes backfill trivially safe.

Map WHOOP's sport IDs to our canonical `sport_type` enum in the **adapter**,
keeping the original in `source_sport_raw`.

**Done when:** `coach normalize` populates both tables from raw; re-running
produces identical results (idempotent); unit tests cover each mapping including
missing/null fields.

### [x] T2.5 — Backfill & regeneration
`coach normalize --rebuild` drops and fully re-derives canonical tables from raw.
This is the payoff of §2.1 — prove it works now, while the data is small.

**Done when:** rebuild produces byte-identical canonical output on unchanged raw.

### [x] T2.6 — Workout dedup / `session_group_id` 🔒
Implement grouping so the same real workout from multiple sources is counted
once. Suggested approach: group by `(user_id, sport_type)` where start times fall
within a tolerance window (~5 min) and durations overlap substantially — but
this is a judgment call, so document your rule in an ADR and make the tolerance
configurable.

**Done when:** tests cover: same workout from 2 sources → 1 group; genuinely
distinct back-to-back workouts → 2 groups; overlapping-but-different sports.

### [x] T2.7 — The resolver
Implement `recovery_resolved` (and an equivalent for weight/food if the shapes
warrant it). Precedence must live in **one obvious, documented place** —
post-membership adapter swap is a one-line change and it should be trivially
findable.

**Done when:** a test proves that flipping precedence changes which source wins,
with no data mutation.

---

## Phase 3 — Compute layer (deterministic math only)

### [x] T3.1 — Daily rollups
Per `day_key`: calories in (from food), calories out (active + basal estimate),
macro totals and adherence vs targets, recovery, weight, workout summary.
**Handle missing data explicitly** — "not logged" ≠ zero. This distinction is
load-bearing for the coach.

**Done when:** `coach status --date YYYY-MM-DD` prints a complete daily picture
with explicit nulls for missing data. This is the "single circle" view.

### [x] T3.2 — Trend functions
Smoothed weight trend (EWMA), HRV baseline + deviation, rolling averages for
recovery and strain. All pure, all unit-tested, all with explicit minimum-data
requirements (**return "insufficient data," never a misleading number**).

**Done when:** tested against hand-computed fixtures.

### [x] T3.3 — Adaptive TDEE 🔒
MacroFactor-style: estimate expenditure from **weight trend + logged intake**
over a rolling window. Deliberately **does not** use wearable calorie estimates
as the primary signal (they're unreliable) — but keep them available for
comparison. Must degrade gracefully with sparse logging.

**Done when:** given a synthetic dataset with a known true TDEE, the estimate
converges within a documented tolerance. Write an ADR on the method chosen.

### [x] T3.4 — Calorie source precedence ⏭️🔒 (RESOLVED — see docs/adr/0007-calorie-burned-precedence.md)
Decide and document which source wins for calories-burned when WHOOP, Apple
Watch, and gym equipment disagree. If it isn't obvious, this is a legitimate
`DECISIONS_NEEDED.md` entry — flag it and move on rather than guessing.

---

## Phase 4 — Coach layer (only if the window allows)

⚠️ These are **design-heavy** and benefit from human input. Do **not** rush them
to fill time. If Phase 3 finishes and no human is available, prefer strengthening
tests and docs over half-building this.

### [x] T4.1 — Tool-calling contract
`src/coach/coach/tools.py` — 5 tools (`get_daily_status`, `get_weight_trend`,
`get_recovery_history`, `get_tdee_estimate`, `get_safety_flags`), each a thin
adapter over tested Phase-3 compute + resolver views. Structured data only, with
provenance (`source`) + explicit null/insufficient; no prose, no math (§2.2).
`anthropic_tool_defs()` + `dispatch()`. Deterministic, no model call.

### [x] §8.6 — Deterministic safety guardrails
`src/coach/compute/guardrails.py` — hard limits in code, not prompt:
weight-loss-rate alert off the EWMA trend (warn >1%/wk, critical >1.5%/wk),
1200 kcal floor clamp. Surfaced via the `get_safety_flags` tool.

### [x] T4.2 — Grounding harness (built; live run = human step)
`src/coach/coach/grounding.py` — `SYSTEM_PROMPT` faithfulness contract +
fabrication-risk `SCENARIOS` + `admits_absence`/`fabricated_numbers` helpers.
Substrate guarantee tested (no tokens, §6.2); `run_live_grounding` fully
implemented (fresh in-memory DB per scenario, real tool contract, injectable
transport — harness itself offline-tested). **Human step remaining:** put
`ANTHROPIC_API_KEY` in `.env`, run `coach eval grounding` (burns tokens §8.7);
target = zero fabrications.

### [x] T4.3 — `coach ask` (the actual coach)
`coach/llm.py` (stdlib-only urllib Messages client — no SDK, §6.4; injectable
transport, bounded retries, prompt caching §8.7, no secrets logged §8.4) +
`coach/agent.py` (bounded loop MAX_ROUNDS=8; refusal/pause_turn/max_tokens/
unknown-tool handled explicitly). CLI: `coach ask "…" [--show-tools]`,
`coach eval grounding`. `COACH_MODEL` default claude-opus-4-8 (overridable).
**Live verification pending** (needs ANTHROPIC_API_KEY).

---

## Blocked — requires hardware or human 🧑

Do **not** attempt these unattended. Listed so you don't waste the window trying.

- **WHOOP 5.0 MG local BLE read** — needs the physical strap and a paired
  Bluetooth radio. *Preparatory work you may do:* research NOOP / OpenWhoop /
  OpenStrap, document 5.0 MG support status, and write `docs/adr/ble-approach.md`
  with a recommendation. **Do not add BLE dependencies or write speculative
  pairing code.**
- **HealthKit / Health Connect ingestion** — data lives on the phone. Blocked
  until the human sets up an export path.
- **Live WHOOP API verification** — needs real credentials in `.env`.
- **Coaching methodology** (cut aggressiveness, training philosophy) — the
  human's body, the human's call.

---

## End-of-session checklist

Before you stop (or when the window is nearly exhausted):

1. Commit all work. Never leave the repo dirty.
2. Update this file's checkboxes.
3. Write/update `SESSION_LOG.md`: what got done, what's blocked, what the human
   should verify **first**, what you'd do next.
4. Ensure `DECISIONS_NEEDED.md` reflects every open question, each with your
   recommendation.
5. Confirm `pytest` and `ruff check` pass on the final commit.

---

## Phase 5 — Apple Health ingestion (WEIGHT / body-comp only)

Full spec in `TASKS_PHASE5.md`. **Scope revised 2026-07-19c:** Apple Health is
our **weight/body-comp** source only. It is NOT a usable nutrition source — MFP
stopped writing food to Apple Health after Oct 2025 (Premium paywall), leaving
only 5 dietary days in the whole export. **Food moved to Phase 6 (MFP CSV).**
Work in order; recon before code.

- [x] **T5.0 — Protect the export file** 🔒 — `apple_health_export/` gitignored
  (`.gitignore:56`, rule committed), never tracked/staged/committed. Export now
  present (1.1 GB).
- [x] **T5.1 — Reconnaissance** — `docs/healthkit-export-notes.md` written.
  Key findings: nutrition is SPARSE (~5 logged days; MFP per-meal, Foodnoms
  per-item); macros present 100% of energy-days; weight RICH (OKOK scale + MFP,
  unit **lb**); multi-source BodyMass → siblings; **real IANA via `HKTimeZone`
  on dietary rows only** (startDate offset unreliable) — day_key from HKTimeZone.
- [x] **T5.2 — Streaming XML parser** — `iterparse`, flat memory, nutrition +
  body only; skip workouts/HR/steps/sleep; read MetadataEntry before clearing.
  (Merged in PR #5.)
- [x] **T5.3 — Raw ingestion** — verbatim to `raw_events`; `source='healthkit'`
  (already in CHECK — no raw rebuild needed); deterministic `external_id` from
  `(type,sourceName,startDate,value)`; idempotent. **Body-only** (dietary skipped;
  food = MFP CSV). `adapters/healthkit/ingest.py`. Verified: 1410 body records
  from the real 1.1 GB export in ~7s.
- [x] **T5.4 — Normalizer (WEIGHT)** — `weight_measurement`: lb→kg, BodyFat %,
  LeanMass; §2.7 (no zero-fill); OKOK vs MFP-weight kept as siblings (§2.3) via
  `source_app` (D3/ADR-0008). One canonical row per HK record (1:1 raw_ref).
- [x] **T5.5 — Timezone** — body rows have no `HKTimeZone` → `tz_name` NULL,
  `utc_offset` from startDate; `day_key` exact regardless (§2.6).
- [x] **T5.6 — CLI** — `coach ingest healthkit --file <path>` (.xml/.zip);
  wired into `coach normalize` (+ `--rebuild`).
- [x] **T5.7 — Verify against real data** — `coach status --date 2026-07-18`
  shows `weight [healthkit]: 83.19kg (trend 82.60kg)`; 298 resolved days
  (2023-07-06 → 2026-07-18). `coach tdee` correctly reports insufficient intake
  (food lands Phase 6).
- [x] **T5.8 — Fixtures** — synthetic body records covered by the existing
  `tests/fixtures/healthkit/export_sample.xml` (labeled synthetic; OKOK+MFP
  BodyMass, BodyFat, lb units), exercised by `test_healthkit_weight.py`.

---

## Phase 6 — MyFitnessPal nutrition (direct CSV adapter)

**Why (2026-07-19c):** the real food history (consistent Feb–June logging) lives
on MFP, not in Apple Health. Source = MFP **Privacy Center → "Download My Data"**
full export (free, CCPA/GDPR; NOT the closed API, NOT scraping — §12). Zip
expected ~2026-07-20. Work in order; **recon before code** (WHOOP-404 lesson).

- [ ] **T6.0 — Receive + protect the export** — user drops CSV(s) in a gitignored
  local dir (`data/mfp/`). Confirm never staged/committed (same rigor as T5.0).
- [ ] **T6.1 — Recon** 🔒 — print structure ONLY (aggregate, §6.3): headers, date
  format, meal grouping, macro columns, units, date range, distinct logged days.
  Confirm the Feb–June month is present and gap-free. Assume nothing.
- [ ] **T6.2 — Raw ingestion** — verbatim rows to `raw_events`. **Gated on D4**
  (`raw_events.source` has no `myfitnesspal`; needs a sign-off migration).
- [ ] **T6.3 — Normalizer** — pure `raw → food_entry`; day_key from CSV local
  date; §2.7 no zero-fill; per-entry provenance. (Buildable + testable before D4.)
- [ ] **T6.4 — CLI** — `coach ingest mfp --file <path>`.
- [ ] **T6.5 — Verify** — `coach status`/`tdee` over the logged month; adaptive
  TDEE should finally have an intake window to calibrate on.
- [ ] **T6.6 — Fixtures** — small scrubbed synthetic from the OBSERVED CSV format.
