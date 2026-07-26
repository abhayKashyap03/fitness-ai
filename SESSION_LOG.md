# Session Log

> Rolling handoff (CLAUDE.md §6.3). Current state + latest sessions only. Older
> sessions → `docs/sessions/`; `git log` is the real history.

---

## Where the code stands (verified 2026-07-26)

- **328 tests green; ruff + mypy clean. Schema at v10** (migrations 0001–0010).
  On `main` after PRs #12–#16 merged; working tree clean.
- Phases 0–7 done. WHOOP + MFP (food+weight) run **live**; the coach (`coach
  ask`) answers grounded questions live; and it now **steers** — a cut/bulk plan
  sets a daily calorie goal + timeline + adherence. `coach eval grounding` and
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
  set|status`, `ask`, `eval grounding|hrv`, `doctor`, `sync`.
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
