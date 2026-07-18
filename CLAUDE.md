# CLAUDE.md — Unified AI Health & Fitness Coach

> Read this fully before writing code. It encodes decisions already made after
> extensive design discussion. **Do not re-litigate them.** If you think one is
> wrong, say so explicitly and wait — don't quietly deviate.

---

## 1. What we're building

A personal AI coach that reads **all** of one user's health data — WHOOP
recovery/HRV/sleep/strain, nutrition (calories + macros), body weight &
composition, and strength/workout logs — into one grounded, unified store, then
guides structured **cuts and bulks**.

Built as a scratch-my-own-itch tool for a single user (n=1), architected so it
*could* become a public product later without a rewrite.

**Two things make it different from every existing product:**
1. It sees recovery + food + weight + training **together** (no incumbent does).
2. It **never hallucinates the user's numbers** (the #1 complaint about WHOOP
   Coach and Levels' AI).

---

## 2. Non-negotiable architectural principles

These are **one-way doors**. Violating them is the single worst thing you can do
in this repo.

### 2.1 Raw is sacred; canonical is disposable
- Every byte fetched from any source is written **verbatim, append-only** to
  `raw_events`. Never edit, never delete, never "clean on the way in."
- Every canonical row carries `raw_ref` back to the raw payload it came from.
- Canonical tables must be **fully regenerable** from raw. When normalization
  logic improves, we re-derive and backfill history. Design every normalizer
  as a pure function `raw -> canonical` so this stays true.

### 2.2 Code computes; the LLM narrates
- **The LLM must never do arithmetic.** Not sums, not averages, not trends, not
  TDEE, not percentages. Ever.
- All numbers come from deterministic Python in the compute layer, exposed to
  the model as tool calls returning structured data.
- The model's job: decide which tools to call, then explain the results.
- If data is missing, the model says "I don't have that data." It must never
  interpolate, estimate, or invent. This is a **correctness requirement**, not
  a style preference.

### 2.3 Provenance is first-class
- Every canonical row has a `source` column identifying the adapter that made it.
- Multiple sources for the same real-world fact coexist as **sibling rows**.
  We never merge them destructively at write time.
- Which source "wins" is resolved at **read** time via views/precedence rules,
  never by deleting or overwriting rows.

### 2.4 `user_id` on every row
Always `1` today. It is multi-tenancy insurance and costs nothing now. Do not
omit it "because it's a single-user app."

### 2.5 Source-agnostic canonical schema
Adapters translate their source's shape **into our schema at the edge**. A
vendor's field names and JSON structure must never leak past the adapter
boundary into compute or coaching code. Adding a new source should be a new
adapter file, not a schema change.

### 2.6 Travel-proof time
The user travels solo across time zones constantly. Every timestamp is stored as:
- a **UTC ISO-8601 instant**, plus
- the **IANA timezone name** it occurred in (e.g. `America/New_York`), plus
- a **`day_key`** (`YYYY-MM-DD`) for the local/physiological day it belongs to.

Never derive a day boundary from a naive local timestamp. Day-boundary bugs are
a known, expected failure class here — write tests for them.

---

## 3. Locked technical decisions

| Decision | Value | Notes |
|---|---|---|
| Language | **Python 3.14** | |
| Store | **SQLite** (single file) | Fine at n=1; two-way door |
| Shape of v0 | **Local CLI**, no server, no UI | Prove the spine before any UI |
| Hosting | **None** — runs locally | |
| Auth | **None** — single user | `user_id` column reserved anyway |
| Package mgmt | `uv` if available, else `pip` + venv | |
| Testing | `pytest` | |
| Formatting | `ruff` (lint + format) | |
| Typing | Type hints throughout; `mypy`-clean where practical | |

**Build order: one thin vertical slice at a time.** Ingest → raw → canonical →
compute → one real query, working end to end on WHOOP alone, before widening to
any other source. One vertical slice beats four half-built horizontal ones.

---

## 4. The WHOOP situation (critical context)

The user owns a **WHOOP 5.0 MG** with ~9 months of membership left.

**The problem:** WHOOP is subscription-tethered. When membership lapses, the
strap effectively goes dark — the official app stops syncing it and the official
API (which requires an active membership) dies with it. The user does not want
to pay $199–$359/yr forever to keep hardware they own.

**The strategy:** `recovery` is a **generic slot** filled by interchangeable adapters:

- **Adapter A — WHOOP Cloud API (OAuth 2.0).** Works now. Gives WHOOP's
  *proprietary* recovery/strain/sleep scores. **Dies with the membership.**
- **Adapter B — Local Bluetooth (BLE).** Reads raw sensor data directly off the
  strap; no account, no cloud, no subscription. Survives membership end.
  Metrics are **recomputed from raw** with textbook formulas, so they are
  *different, approximate* numbers — not WHOOP's score.

Community open-source projects for Adapter B (all unofficial, young, fragile —
WHOOP can break them with firmware): **NOOP** (`github.com/ryanbr/noop`),
**OpenWhoop**, **OpenStrap**. ⚠️ The 5.0 MG is the newest hardware and least
community-tested — **local-read viability is UNPROVEN** and is the project's
single biggest technical risk.

**The calibration play (why this shapes the code now):** run Adapter B in
parallel with Adapter A *during* the paid window, and use WHOOP's official
scores as ground truth to tune our own computed metrics. Both write sibling
rows; the resolver picks the authoritative one. When membership ends, we flip
precedence — no migration, no schema change, no seam in the objective series.

**Implication for you:** never assume `whoop_api` is the only recovery source.
Never hardcode WHOOP-specific fields outside its adapter. Build Adapter A now;
leave Adapter B a clean, obvious seam to slot into.

---

## 5. Data model orientation

Full DDL lives in `schema/canonical_schema_v0.1.sql`. Key ideas:

- **`raw_events`** — append-only, immutable, deduped on
  `(source, external_id, payload_hash)`.
- **`recovery`** — one row per `(user, day_key, source, score_method)`.
  Critically, it separates:
  - **Objective measurements** (`hrv_rmssd_ms`, `resting_hr_bpm`, `spo2_pct`,
    `skin_temp_c`, `resp_rate_bpm`) — physical quantities, **comparable across
    sources**. This is our honest calibration currency.
  - **Derived composite score** (`score`, `score_scale`, `score_method`,
    `is_official`) — **not comparable across sources**, because WHOOP's
    proprietary weighting ≠ our textbook formula.
- **`workout`** — one row per source-detected session. `session_group_id` links
  rows representing the **same real workout** arriving from multiple sources, so
  compute counts it **once**. Cross-source double-counting is a known,
  expected bug class (one run appearing via Watch + Strava + WHOOP = 3× calories
  if handled naively).
- **`recovery_resolved`** — a view applying source precedence. Swapping adapters
  post-membership is a one-line reorder of its `CASE`.

`food` and `weight` shapes are **not yet designed** — see TASKS.md.

---

## 6. Repo conventions

```
.
├── CLAUDE.md              # this file
├── TASKS.md               # ordered work queue
├── DECISIONS_NEEDED.md    # created by you when blocked (see §7)
├── README.md
├── .env.example           # committed
├── .env                   # NEVER commit
├── docs/adr/              # architecture decision records
├── schema/                # .sql DDL + migrations
├── src/coach/
│   ├── adapters/          # one module per source; the ONLY vendor-aware code
│   ├── store/             # raw + canonical persistence
│   ├── normalize/         # pure raw -> canonical functions
│   ├── compute/           # all deterministic math
│   ├── coach/             # LLM tool-calling layer (later)
│   └── cli/               # entry points
└── tests/
    └── fixtures/          # recorded/sample API payloads
```

**Commits:** small, frequent, conventional-commit style (`feat:`, `fix:`,
`refactor:`, `test:`, `docs:`, `chore:`). Commit after each completed task in
TASKS.md — if a session halts mid-run we lose the tail, not the night.

**Secrets:** `.env` only, `.gitignore`d from the first commit. Never hardcode a
credential, never print one to logs, never commit one. If you find one committed,
stop and flag it.

**Tests:** every normalizer and every compute function gets unit tests. Adapters
are tested against **recorded fixtures** in `tests/fixtures/`, never live calls.

**ADRs:** when you make a non-obvious technical call, write a short ADR in
`docs/adr/`. Format: Context / Decision / Consequences. Keep it to a page.

**DevOps:** *Always* work on feature branch and create PRs. I will merge them 
myself after reviewing. *Never* directly work on and push to the `main` branch.

---

## 7. Working autonomously — protocol

You will often be running unattended. Follow this exactly:

1. **Work TASKS.md in order.** Mark tasks complete as you go.
2. **Never guess on a one-way door.** If a decision would be expensive to
   reverse and isn't specified here, **do not pick one silently.** Append the
   question to `DECISIONS_NEEDED.md` (what's blocked, the options, your
   recommendation, why it matters) and move to the next *independent* task.
3. **Two-way doors: just decide.** Naming, file layout, helper structure, minor
   library choices — pick something sensible, note it, keep moving. Don't burn
   the window on reversible details.
4. **Never invent credentials or fake data to "get past" a blocker.** If a task
   needs live creds you don't have, mark it blocked and move on.
5. **Fixtures over live calls.** If real API responses aren't available, write
   the adapter against the documented schema, generate representative fixtures,
   and **clearly label them as synthetic** so first contact with real data is
   an expected reconciliation, not a surprise.
6. **Leave a `SESSION_LOG.md` entry** at the end of each work session: what got
   done, what's blocked, what the human should verify first, and what you'd do
   next.

---

## 8. Things that will bite (known risk list)

Ordered by likelihood of causing real pain:

1. **WHOOP 5.0 MG local-read viability** — unproven; hardware-gated; can't be
   resolved without the physical strap.
2. **Data normalization** — sources disagree on the same fact (WHOOP vs Apple
   Watch vs machine calories can differ 30%+); cross-source double-counting;
   day-boundary/timezone chaos; async partial data (recovery lands in the
   morning, food dribbles in all day, weight sometimes never).
3. **Metric discontinuity** — official vs recomputed scores are different
   numbers. Mitigated by storing objective measurements separately and
   computing our own metric from raw across the whole timeline.
4. **LLM faithfulness** — see §2.2. Non-negotiable.
5. **Recovery-informed macro validity** — MacroFactor deliberately excludes HRV
   because it's noisy. Our differentiator may not actually beat weight+intake
   alone. **Must be validated, not assumed.**
6. **Nutrition garbage-in** — self-reported intake runs 20–40% off.
7. **User adherence** — the coach starves if the user stops logging. Reducing
   logging friction beats adding AI features.
8. **Maintenance rot** — 5 integrations = 5 things that break. Firmware pushes,
   token expiry, API versioning, stale food DBs.

---

## 9. Explicitly out of scope for now

Do **not** build these. Building them is the side-project graveyard:

- Auth systems, user registration, login flows
- Billing, subscriptions, payments
- Multi-tenancy machinery (the `user_id` column is enough)
- Web servers, REST APIs, GraphQL
- Docker, Kubernetes, CI/CD pipelines, cloud infra
- Any UI — web, mobile, or desktop
- Nutrition food-database integration (deferred; see TASKS.md)

Clean seams: yes. Premature machinery: no.

---

## 10. API reality

| Source | Status |
|---|---|
| WHOOP Cloud API | ✅ Free OAuth 2.0 (v2). ~100 req/min. Requires active membership. Recovery *formula* is proprietary — we get the score + inputs, not the weighting. |
| WHOOP local BLE | ⚠️ Unofficial, open-source, 5.0 MG unproven |
| MyFitnessPal | ❌ **Closed to new developers. Do not design around it. Scraping violates ToS.** |
| Apple HealthKit / Google Health Connect | ✅ But **not readable from a laptop** — data lives on-device. Bridge via Health export (XML zip) or an auto-export app writing to a folder. |
| USDA FoodData Central / Open Food Facts | ✅ Free, open — the intended nutrition DB path |
| Smart scale | ⚠️ Brand-dependent; Withings has an API, most others route through the health platforms |
