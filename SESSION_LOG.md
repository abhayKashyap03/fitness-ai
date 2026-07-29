# Session Log

> Rolling handoff (CLAUDE.md §6.3). Current state + latest sessions only. Older
> sessions → `docs/sessions/`; `git log` is the real history.

---

## Where the code stands (verified 2026-07-26)

- **419 tests green; ruff + mypy clean. Schema at v11** (migrations 0001–0011).
  PRs #12–#19 merged; #20 (web UI) and #21 (MFP training + watermark) open.
- Phases 0–7.5 done, plus the source-ownership fix (ADR-0015). WHOOP + MFP (food+weight) run **live**; the coach (`coach
  ask`) answers grounded questions live; it **steers** — a cut/bulk plan sets a
  daily calorie goal + timeline + adherence; and there is now a **local web
  dashboard** (`coach web`, ADR-0014). `coach eval grounding` and
  `eval hrv` both pass. Recovery + sleep + weight + food + plan show together on
  one `coach status` screen — the §1 thesis, working, in daily use.
- **Data types all have a canonical home:** recovery (0001), workout, food
  (0006), weight/body-comp (0005/0007), sleep (0009), plan (0010 — first
  user-authored table). `raw_events` is sacred and append-only; `normalize
  --rebuild` proven **byte-identical on the real DB**.
- **LLM layer is provider-agnostic** (§2.5 applied to vendors): Google Gemini
  (default, free tier), Anthropic, and xAI Grok — each a different wire shape,
  one module apiece, agent loop vendor-blind. Grounding verified 3/3 on Grok.
- CLI surface: `db init|status|backup|verify`, `auth whoop`, `ingest
  whoop|healthkit|mfp`, `normalize [--rebuild]`, `status`, `tdee`, `plan
  set|status`, `ask`, `eval grounding|hrv`, `doctor`, `sync`, `web`.
- **GateGuard disabled** via `.claude/settings.local.json` — **user-authorized
  2026-07-19; do NOT re-flag** the §8.2 tension. File stays untracked.
- **Open item (unchanged):** `~/.zshrc` plaintext API keys flagged, unrotated —
  user's call, do not act.

### What's next → see [TASKS.md](TASKS.md) ▶ NEXT UP
The big open item is the **BLE hardware spike** (Adapter B, ADR-0012) — the
subscription-survival play; needs the physical MG strap. The plan layer's daily
calorie goal fills in once TDEE crosses 10 logged-intake days (no code).

---

## Session 2026-07-26 (b) — Phase 7: the cut/bulk plan layer (PR #16, merged)

The first thing that *steers* rather than observes. D5 resolved as **option C**
([ADR-0013](docs/adr/0013-plan-target-model.md)): a signed target rate is the one
canonical driver; a goal-weight+deadline is convenience that reduces to a
§8.6-clamped rate. Deterministic all the way — the LLM narrates the goal, code
computes it (§2.2), clamped by the guardrails (§8.6).

- **Migration 0010 (schema v10)** — `plan` table, the first USER-AUTHORED table
  (not raw-derived; no raw_ref, absent from the rebuild/fingerprint). Append-only
  history, one `is_active` row per user. `store/plan.py`.
- **`compute/plan.py`** (pure) — daily calorie goal = adaptive TDEE + the rate's
  energy delta (KCAL_PER_KG), through the calorie-floor clamp; timeline
  projection; `Insufficient` without TDEE/trend. `guardrails.clamp_target_loss_rate`
  makes an unsafe deadline safe at set time (timeline stretches, honestly).
- **`get_plan_status`** — 7th coach tool, so `coach ask` reasons about adherence.
- **CLI** — `coach plan set (--rate | --goal-weight --by | --maintain) [--protein]`,
  `coach plan status`, plan line in `coach status`.
- **Mid-cut adoption** — `plan set --start-date/--start-weight` backdates the
  anchor; `plan_status` then reports progress + a deterministic adherence label
  (on_track/ahead/behind/wrong_way). A same-day plan shows no progress yet (§2.7).
- **Live-verified** (T7.5): set/status/adherence work on the real DB. The daily
  calorie goal reads `Insufficient` until TDEE has 10 logged-intake days
  (ADR-0005) — by design, fills in with a few more days.
- +26 plan tests. **328 tests green; ruff + mypy clean.**

---

## Session 2026-07-28 (b) — plan layer live on real data; floor-clamp projection bug

Ran the daily driver and verified the plan layer end-to-end for the first time.

- **T7.5 fully complete.** Adaptive TDEE crossed its 10-logged-intake-day gate
  (ADR-0005), so the plan now emits a REAL daily goal: TDEE **2040 kcal**, trend
  82.67 kg, goal 75 kg, adherence **ON_TRACK** (-0.86 kg over 8 days).
- **Per-type watermark confirmed in production:** sync reported "incremental
  since 2026-07-26" where it used to be stuck at 07-21.
- **Bug found by the real numbers (fixed here).** The §8.6 calorie floor bound
  for the first time (a -1%/wk target on a 2040 TDEE implies 1131 kcal, below
  the 1200 floor). ADR-0013 promises the timeline "stretches rather than
  promising an unsafe date" — but `plan_status` projected from the TARGET rate,
  so it promised exactly the date the floor forbids. Added
  `effective_rate_kg_per_week` (derived from the CLAMPED goal); timeline and
  adherence are now judged against what is actually achievable. Live: the
  projection honestly moved 9.3 → 10.0 weeks (Oct 1 → Oct 6).
  Also added `effective_daily_kcal_delta` so surfaces are self-consistent —
  the CLI showed "1200 kcal/day (TDEE -909)", which doesn't add up; now -840.
  Adherence is judged against the achievable rate too, so faithfully following
  a clamped plan no longer reads "behind" forever.
- HRV verdict still **NOISE** (autocorr +0.21 < 0.3). `coach doctor`: all clear.

**For the human:** the -1.00%/week target is at the §8.6 ceiling AND the calorie
floor is binding, so the target rate is not achievable within the safety limits.
Either the timeline stretches (as it now honestly shows) or the target eases.
That is a decision for the user, not the tool.

---

## Session 2026-07-27 — MFP training, sync watermark, source ownership (PR #21)

Two bugs the user reported, both root-caused against the REAL database before any
fix was written (systematic-debugging, not guessing).

- **MFP training was invisible.** MFP diaries carry `exercise_entry` items beside
  the food rows; the normalizer filtered to `diary_meal` only — right for not
  fabricating food calories, but nothing ever picked the exercise up. A walk
  logged in MFP reached `raw_events` and stopped. `parse_exercise` + migration
  0011 (drops the `workout.source` CHECK per ADR-0009). **Recovered 81 sessions
  back to 2026-01-06.** Not treated as workouts: `is_calorie_adjustment` rows
  (device burn adjustments) and entries with no usable duration.
- **Sync re-fetched the same days forever.** `auto_since` took the MIN across
  record types, so a QUIET type pinned every window open: no workouts since 07-23
  meant recovery/sleep/cycle re-fetched from 07-21 every run. `auto_since_by_type`
  resumes each type from its own watermark.
- **Cross-source double-counting, found while verifying.** 23 days had the same
  session in both sources. MFP start times are placeholders (every walk at 08:30,
  months apart) so time-window dedup can never match them — both rows survived and
  compute counted the workout AND its calories twice (§5's expected bug class).
  `_absorb_placeholder_time_sources` folds hand-logged rows into the strap group
  for the same day+sport. Trade-off documented in code: two genuinely distinct
  same-sport sessions collapse into one; under-counting is the safer error.
- **ADR-0015 — source-domain ownership (user's rule).** MyFitnessPal is
  authoritative for training AND food; WHOOP is the recovery instrument
  (HRV/sleep/skin-temp) and its auto-detected workouts are a by-product, not the
  training log. `_SOURCE_RANK` puts MFP first. **`strain` is the documented
  exception** — WHOOP-only, aggregated separately once per group, so flipping the
  ranking can never silently drop a health metric. CLAUDE.md §12 + README both
  said the opposite and were corrected.
- **Plan insufficient marker** now propagates the real reason ("need 10, have 9")
  instead of flattening to "need 1, have 0".
- **Grounding eval made two-sided** (3 → 10 scenarios). It only tested absence, so
  a model that always said "I don't have that" scored a perfect run. Added
  `must_state_numbers` + `omitted_numbers()`; an always-refuse model now provably
  fails the present-data scenarios.

- **HRV probe with real power.** The strain probe is capped at n=24 by WHOOP
  coverage (strain is WHOOP-only). Training DURATION comes from every source and
  81 MFP sessions just appeared, so `dev_vs_next_train_min` tests the same
  hypothesis with more data — counted once per session group. Live r=+0.072
  (n=28); **verdict still NOISE**. The extra power did not rescue the
  differentiator, which is exactly the honest answer risk #6 asks for.
- **Session detail.** The rollup said "N session(s)" and nothing else, so what
  was actually trained stayed invisible. `compute.training_sessions()` + a
  `get_training_sessions` tool (8th) + a list under `coach status` + the
  dashboard training card + `/api/training`. One compute function, three surfaces.

Live-verified: today's walk shows (203 kcal / 2700s); 2026-07-23 reports MFP's
745 kcal / 8160s with WHOOP's strain 15.55 intact, listing 'Strength training'
97min/398kcal and 'Walking, 2.0 mph' 39min/347kcal; groups 152 → 123.

---

## Session 2026-07-26 (c) — the first UI: local web dashboard (PR #20)

§3's gate ("prove the spine before any UI") was met, so the UI got built. This
**lifts two explicit CLAUDE.md bans** — §11 (UI, web servers) and §6.4 (web
frameworks) — scoped to a *local single-user* UI and recorded in
[ADR-0014](docs/adr/0014-local-web-ui.md); both sections amended to point there.
Auth/billing/multi-tenancy stay out of scope.

- **`src/coach/web/`** — FastAPI + Jinja templates. A **presentation boundary
  exactly like the CLI**: every number comes from `coach/tools.py`; no arithmetic
  or domain logic in a route or template (§2.2). That rule is the thing to guard
  — putting logic in the UI because it's faster than adding a tested compute
  function is the obvious failure mode.
- **Absence renders as absence** (§2.7) — "NOT LOGGED" never becomes `0`;
  insufficient data shows its need/have marker.
- **`/api/*` is a pass-through of the tool layer** — the same contract the future
  iOS app (P13) consumes, now exercised against real data. A test asserts
  byte-equality with the tool handler so it can't drift.
- **Pages** — dashboard (single circle + plan + TDEE + weight sparkline), plan
  view/set, coach chat. `coach web` binds **127.0.0.1** by default; any other
  `--host` warns (no auth on personal health data).
- Deps behind the optional **`[web]` extra**; core CLI still 3 dependencies.
- **344 tests green** (+16 web), ruff + mypy clean. **Live-verified**: all routes
  200 on the real DB, real recovery/sleep/weight/food rendering, TDEE honestly
  reporting `need 10, have 8`.
- Known nits, deliberately not fixed here: `plan_status`'s insufficient marker
  reads `need 1, have 0` where the TDEE card says `need 10, have 8` (honest but
  less informative — propagate the underlying marker in compute later);
  `ruff format` fails repo-wide on 30 pre-existing files, untouched to avoid
  unrelated churn.

---

## Session 2026-07-26 — Grok provider, HRV verdict, doc/code sweep

Three pieces of work; all on their own branches → PRs (user reviews/merges).

- **Grok provider (PR #13, merged).** Added xAI Grok as a third LLM provider on
  its own branch (user has Grok API credits for eval). It's the OpenAI
  chat-completions wire shape — genuinely different from Gemini and Anthropic —
  and it dropped in with **zero agent-loop changes**: one module
  (`coach/llm/grok.py`) + one registry line. That's the §2.5 vendor abstraction
  proving itself. Verified against xAI's live docs first (don't guess APIs).
  Grounding 3/3 on real credits.

- **HRV deterministic verdict (PR #14, merged).** The user caught that `coach
  eval hrv` printed a static "reading:" legend regardless of the numbers — the 5
  stat lines were real, but the tool never *concluded*. Now `hrv_verdict()`
  (`compute/hrv_validation.py`) makes a signal/noise/insufficient call from the
  real stats vs fixed thresholds (autocorr ≥ 0.30 AND a next-day |r| ≥ 0.20;
  absolute value, so a strong negative still counts; insufficient sub-stats are
  ignored, never zeroed). **Live result on the real DB: NOISE** — 72 HRV days,
  lag-1 autocorr +0.19, best next-day r 0.14. An honest null on risk #6's core
  bet. It shapes Phase 7: lean on weight-trend + intake, treat recovery lighter.

- **Codebase placeholder sweep (this session).** User asked to find any other
  hardcoded/placeholder/"reserved-for-later" output like the old `eval hrv`.
  Swept CLI + compute + coach + tools. **Finding: `eval hrv` was the only one**
  (already fixed). Everything else computes from real data. Two pieces of
  genuinely-gated future code exist but emit **no** fake output — they're just
  not surfaced yet:
  * `compute/calibration.py` (`calibration_report`/`compare_series`) — real math,
    no live caller; needs `whoop_ble` sibling rows that don't exist until the BLE
    hardware spike lands. (Could run today on healthkit-vs-mfp weight, but no
    command wires it — a surfacing gap, not a placeholder.)
  * `compute/guardrails.py` (`clamp_calorie_target`/`calorie_floor_alert`) —
    built for Phase 7's target layer; fires only when a target is proposed.

- **Docs refreshed:** TASKS.md gained a top-of-file ▶ NEXT UP block (so work
  resumes cleanly after any usage-limit cutoff), Phase 7 sketch, and the
  session-4/5 records; README added Grok; DECISIONS_NEEDED added D5 (cut/bulk
  target model); this log rewritten; pre-session-4 detail archived to
  `docs/sessions/2026-07-19-to-25.md`.

---

## Session 2026-07-25 (b) — sleep slice + live verification + review fixes (PR #12, merged)

Live credentials were available, so several long-"unverified" items are now
verified against the real install and real APIs.

- **Model-has-no-clock fix** (`e81a417`) — the first live `coach ask` had Gemini
  guessing a training-era date and querying empty windows. `ask(today=...)` now
  injects the real date (COACH_HOME_TZ); `dispatch(today=...)` fills omitted date
  args server-side (§2.2 extends to the calendar). An explicit model date still wins.
- **Gemini free-tier retry** (`105e4bd`) — honor the JSON `retryDelay` hint in the
  error body (Gemini never sends a `Retry-After` header); unblocked `eval grounding`.
- **Sleep canonical slice** (`886a8ce`, migration 0009, schema v9) — the last §1
  data type without a home. Objective stage durations are cross-source calibration
  currency; composite percentages carry `score_method`/`is_official` (§5). One row
  per session; naps are sibling rows; `day_key` = the day the sleep ended. Field
  names verified against live payloads + developer.whoop.com. **103 real sessions
  canonicalized; rebuild byte-identical on the real DB.**
- **Calibration stats** (`f1a8922`, `compute/calibration.py`) — bias/MAE/correlation
  over shared days; wired to whoop_api vs whoop_ble when Adapter B lands.
- **Credential namespacing** (`28660f2`) — external review caught a real bug:
  global `.credentials/*.json` meant a second user's WHOOP auth would overwrite
  the first's refresh token. Now `.credentials/u<user_id>/`; legacy file MOVED on
  first access (never copied/deleted). Live-verified incl. an OAuth auto-refresh.
- **Sync service seam** (`f68634b`) — `services/sync.py::run_sync()` returns a
  `SyncResult`; the CLI only formats it. Degradation contract tested with zero
  credentials: one dead source never costs you the others.
- **Grounding scorer fix** (`8ecaa52`) — it was failing correct answers 3/3
  (flagging the tool's own window/insufficient counts and prose dates as
  "fabricated"). Now strips prose dates and derives the allowed-number set from
  what the model was actually given (replays each successful tool call). An
  invented measurement is still caught; faithfully restating a tool's counts is not.

### Still not verified
- `coach eval grounding` full 3/3 pass — free-tier 20 req/min quota throttles a
  back-to-back run. Re-run on fresh quota, or point it at Grok/Anthropic.
- BLE hardware spike (needs the strap; ADR-0012's acceptance gate).
