# ROADMAP

Version-controlled mirror of the **Project Task Board** in the Notion hub
(the live, clickable source of truth). This file exists so the plan survives
outside Notion and shows up in `git`. If the two disagree, the Notion board wins
for near-term status; this file is refreshed alongside major doc updates.

**Convention (decided 2026-07-26):** near-term work is granular (task cards);
far-future work is **epics**, deliberately coarse and refined when closer
("decay with distance"). Time is expressed as **phases, not dates** — solo,
evenings-and-weekends pace. Guardrails and principles live in
[CLAUDE.md](../CLAUDE.md); decisions in [docs/adr/](adr/).

**Board columns** (Trello-style, adapted from Dane Wesolko's design board —
dropping On-Hold / In Development / Archived as redundant at n=1):
**Inbox** → **Backlog/Upcoming** (far-future / not-soon) → **Next Up** (actionable
queue) → **In Progress** → **Blocked** (dependency-gated) → **Review** → **Done**.

## Product data model (shipped product, decided 2026-07-26)

The eventual product is a **native iOS (SwiftUI) app + multi-tenant backend**.

- **Food logging → in-app** (own logging on USDA FoodData Central + Open Food
  Facts). The MyFitnessPal private API is **personal-validation-only and does
  NOT ship** (ADR-0010 forbids generalizing it).
- **Exercise logging → in-app** (own).
- **Weight/body-comp → Apple Health (HealthKit)**, automatic. No manual/CSV
  anywhere in the product.
- **Heart rate → WHOOP**: cloud API gives resting + workout avg/max HR;
  continuous/live HR comes via WHOOP **BLE** (Adapter B). Apple Health HR is a
  **fallback** only (needs an Apple Watch).

---

## Done — the CLI codebase (phases 0–7)

| Phase | Summary |
|---|---|
| **P0 Design** | Canonical shapes (recovery/workout/food/weight); ADRs 0001–0005; resolver + EWMA views. |
| **P1 Foundation** | Scaffold, typed config, numbered migration runner. |
| **P2 WHOOP** | OAuth, v2 client (pagination+backoff), idempotent raw ingest, pure normalizers, byte-identical `--rebuild`, `session_group_id` dedup, `recovery_resolved`. Live-verified. |
| **P3 Compute** | Daily rollup, trends (explicit Insufficient), adaptive TDEE (ADR-0005), calorie precedence (ADR-0007). |
| **P4 Coach** | Tool contract, §8.6 guardrails, grounding, `coach ask`; provider-agnostic LLM (Gemini/Anthropic/Grok). |
| **P5 Weight** | Apple Health export → `weight_measurement`; `source_app` siblings (ADR-0008, migration 0005); 298 resolved days. |
| **P6 Food (MFP)** | MFP direct v2 API (ADR-0009/0010); food + weight; migrations 0006/0007. **Personal-only.** |
| **P6.5 Sleep/HRV** | Sleep table (0009); gap-aware EWMA (ADR-0011); HRV validation + verdict → **NOISE**; calibration stats; sync service. |
| **P7 Plan** | Cut/bulk plan (ADR-0013, migration 0010); `get_plan_status`; `coach plan set\|status`; mid-cut backdating + adherence. Live-verified. |
| **P7.5 Web UI** | Local dashboard (ADR-0014 — lifts the §11/§6.4 bans for a local single-user UI). FastAPI + Jinja; `/api/*` pass-through of the tool layer (the contract P13 will consume); dashboard + plan + coach chat; `coach web`, localhost-bound. Live-verified. |

---

## P8 — Subscription survival (BLE Adapter B)

The post-membership path. **ADR-0012 ACCEPTED 2026-08-03 — the hardware gate passed.**

- **[Done · P0]** **BLE spike PASSED 2026-08-03** on the owner's own MG strap. ADR-0012 is
  ACCEPTED and risk #1 is retired. `fd4b0001` present (chars `0002-0005`, `0007` — **no
  `0006`**, unlike whoop-vault's r52 Maverick), plus standard SIG **Heart Rate**
  `180d`/`2a37`, Battery and Device Info. Scrubbed GATT enumeration kept as a baseline
  fixture, because firmware churn is the standing threat and the only way to notice it is
  to have written down what working looked like.
- **[Done]** **Live HR ingest** (`coach ble record`) → `raw_events` (`source='whoop_ble'`).
  Standard SIG `2a37` carries **RR intervals**, so textbook HRV needs no reverse
  engineering at all — a durable path WHOOP cannot break without breaking every generic
  HR app. One raw event per session, original frame hex retained (§2.1).
- **[Next Up]** Historical drain → `raw_events` (append-only, time-sliced). Richer than
  live HR; the ~14-day on-strap buffer makes the time-sliced calibration lossless.
  **Must not assume `fd4b0006`** — this MG does not expose it. Untested on
  macOS/CoreBluetooth (whoop-vault is Linux/BlueZ only). _Depends: nothing — unblocked._
- **[Done]** **Recompute objective metrics from raw** — `hrv_rmssd_ms` (textbook RMSSD)
  and resting HR from standard SIG Heart Rate `2a37`, written as `whoop_ble` sibling rows
  with `score_method='textbook'`/`is_official=0`. No composite score is emitted:
  WHOOP's weighting is proprietary and a lookalike number in the same column would be a
  fabrication. SpO2/skin-temp still need the `fd4b` drain.
- **[Done]** `coach eval calibration` wired — and it no longer waits on `whoop_ble`.
  A weight assembler was added so the machinery runs on today's sibling sources
  (scale-via-Apple-Health vs MFP), exercising the calibration path before Adapter B
  exists. Recovery mode honestly reports insufficient (0 shared days) until the spike
  lands. _Note: the live weight pair agrees to 28 g MAE, which means they are not
  independent measurements — a machinery smoke test, not instrument validation._
- **[Blocked]** Precedence flip at membership end (one-line resolver reorder). _Depends: calibration acceptance._

## P9 — The differentiator

> **Pre-registered gate, 2026-08-02 ([ADR-0019](adr/0019-hosting-the-owners-instance.md) §8):**
> recovery-informed *computation* is pursued, and judged at **n = 150 HRV days**
> against the existing `hrv_verdict()` thresholds (autocorr ≥ 0.30 **and**
> next-day |r| ≥ 0.20). Meets the bar → build. Misses → cut it, on that date,
> without relitigating. The threshold is fixed **before** the data arrives on
> purpose — that is what makes a null result honest (risk #6).
> **Narration is not gated** and ships now: "recovery is low, train lighter"
> needs no statistical claim (§8.6 already treats recovery as a signal). Only
> numbers *driven* by HRV wait.

- **[Next Up · P1]** Recovery-informed training-load auto-scale on low-recovery
  days — **narration only** until the n=150 gate resolves.
- **[Blocked · P1]** Recovery→macro adjustment — **guarded**: HRV is NOISE today; must beat weight+intake before shipping (risk #6). _Depends: the n=150 pre-registered gate._
- **[Next Up · P1]** Plan TDEE-backed daily goal live. _Depends: 10+ logged-intake days (data accrual, no code)._
- **[Done]** Protein target in the plan — `protein_status()` scales g/kg off the
  trend weight and reports target vs logged. It had been *stored and never read*
  since Phase 7 (0010's "future use" comment). No default g/kg is supplied: a
  recommended intake is a coaching opinion, not a measurement. Reported alongside
  plan status so it survives an insufficient TDEE.
- **[Backlog]** Re-run HRV validation as data grows — now a **countdown to the
  n=150 decision date**, not an open-ended revisit.
  _Re-run 2026-07-30: still NOISE (autocorr +0.199, best next-day r 0.133, 72 HRV
  days — unchanged, no new recovery data in the window)._

## P10 — Personal hardening

- **[Done]** Coaching memory & consistency — ADR-0016, migration 0012; model reads, never writes.
- **[In Progress]** Model routing + prompt-cache / COGS audit (§8.7). **Measurement
  landed** (migration 0013 `llm_call`, `compute/cost.py`, `coach cost`): per-call
  tokens + cache-hit rate + per-command split, with rates stamped at call time and
  unpriced never reading as free. First real numbers: ~3.6k tokens per `ask`,
  **47.1% cache hit**. Routing itself stays deferred until the measurement says
  which command is actually expensive (§11).
- **[Backlog]** MFP CSV backfill (Phase 6B — secondary; API path covers daily).
- **[Done]** Zero-fabrication eval at **50 scenarios covering all 9 tools**
  (two-sided; substrate verified offline). `eval grounding --only/--limit` keeps
  a 50-scenario run affordable. _Live model pass remains a human step._

---

## P11 — Backend foundation (the product pivot) · epics

Lifts §11's no-server / no-multi-tenancy for real.

> **Scope correction, 2026-08-02 ([ADR-0019](adr/0019-hosting-the-owners-instance.md)):**
> the hosted instance is **the owner's own**. The 5–30 user invite-only beta
> ADR-0018 described is **deferred, not cancelled** — the member machinery is
> kept as hosting infrastructure, and the medical-disclaimer prerequisite stays
> armed as a tripwire rather than a scheduled task. **The host becomes the only
> instance:** the laptop stops holding live data and becomes the backup archive.

- **[In Progress · EPIC · P1]** Multi-tenant backend (ADR-0018). **Foundation landed**
  (migration 0014): invite-only users, scrypt passwords, hashed session tokens,
  per-user secrets encrypted at rest. **Decided: SQLite on a volume, NOT hosted
  Postgres** — it keeps all 13 prior migrations and every view working unchanged,
  and is a two-way door. Auth is now **wired into the web app** (PR B): per-request user, closed by
  default, and a non-loopback bind with no claimed account refuses to start.
- **[Next Up · P1]** Hosting, in the order ADR-0019 forces: **VPS → domain → TLS
  → headless OAuth → migrate → cron sync → rehearse restore.** Domain + TLS are
  *prerequisites*, not polish: WHOOP is authorised on the host via a public
  `https://<domain>/callback`, so there is nothing to authorise until HTTPS is real.
- **[Next Up]** Headless WHOOP login. `run_login` opens a browser and binds a
  local `HTTPServer`; it cannot run on a VPS. _Depends: domain + TLS._
- **[Next Up]** Nightly `coach db backup` on host cron, pulled off-host to the
  laptop, **with a rehearsed restore**. Nothing in the repo schedules anything today.
- **[Backlog]** Move WHOOP/MFP credentials out of `.credentials/u<id>/*.json`
  into the encrypted `user_secret` store built in ADR-0018. _Depends: foundation._
- **[Backlog]** Move compute + coach server-side (pure Python lifts cleanly). _Depends: foundation._
- **[Backlog]** Ingest/sync as a service + `user_id` activation. _Depends: foundation._
- **[Backlog]** Observability + per-user LLM COGS caps. _Depends: foundation._

## P12 — Own food logging (product food source) · epics

- **[In Progress · EPIC · P1]** In-app food logging — replaces MFP for the product (§12-intended path).
- **[Done]** **Open Food Facts** integration (adapter-bounded, free, no key). `coach food
  search|log` + a Food page in the dashboard. Unknown macros stay NULL through parsing,
  scaling and storage — defaulting them to 0 would under-report intake in exactly the
  direction self-reporting is already biased (risk #7). Live-verified against the real API.
- **[Next Up]** **USDA FoodData Central** — a clean seam beside the OFF adapter; needs a
  free API key. OFF covers barcoded packaged food; USDA is what covers raw ingredients.
- **[Done]** Food search + barcode lookup (low friction = survival, risk #8).
- **[Backlog]** In-app exercise logging.
- **[Backlog]** One-time migrate personal MFP history → own store. _Depends: own logging._

## P13 — iOS app (SwiftUI) · epics

- **[Backlog · EPIC · P1]** App scaffold + auth. _Depends: backend foundation._
  _(The `/api/*` contract it consumes already exists and is exercised by the P7.5 web UI.)_
- **[Backlog]** HealthKit integration (weight/body-comp; HR fallback). _Depends: scaffold._
- **[Backlog]** On-device WHOOP BLE read (CoreBluetooth; continuous HR). _Depends: BLE spike + scaffold._
- **[Backlog]** "Single circle" daily dashboard. _Depends: scaffold._
- **[Backlog]** Chat coach UI (grounded, shows computed data used). _Depends: scaffold + coach server-side._
- **[Backlog]** Food + exercise logging UI. _Depends: own food logging._
- **[Backlog]** Cut/bulk plan UI + adherence. _Depends: scaffold._
- **[Backlog]** Logging-friction reducers (quick-add, notifications, widgets). _Depends: logging UI._

## P14 — Public launch · epics/milestones

- **[Backlog]** Recovery-source abstraction (Oura/Garmin/Apple Watch fill the recovery slot).
- **[Backlog · P1]** Safety / medical disclaimers for public (§8.6 floors in code; legal review). _Depends: foundation._
- **[Backlog]** Billing / subscriptions — only if monetizing; model LLM COGS first. _Depends: foundation._
- **[Backlog]** TestFlight beta → App Store release. _Depends: app feature-complete._
- **[Backlog]** Onboarding flow (connect WHOOP, grant HealthKit, set a plan, first log).

---

## Ongoing (cross-cutting)

- **[P1]** Validation — does recovery improve guidance vs weight+intake alone? (Currently NOISE; cut the differentiator honestly if it never beats the baseline.)
- **[Blocked]** Validation — recomputed metrics track official scores (BLE calibration acceptance). _Depends: whoop_ble rows._
- **[P1]** Safety — maintain §8.6 hard floors; surface harmful patterns; extend for public.
- Docs — keep SESSION_LOG / TASKS / ADRs / the board current (§6.3).
- Maintenance — token expiry / API churn / food-DB staleness watch (risk #9).
