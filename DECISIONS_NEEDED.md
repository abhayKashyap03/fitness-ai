# Decisions Needed

> Claude Code appends here when it hits a **one-way door** it shouldn't decide
> alone. Each entry: what's blocked, the options, its recommendation, why it
> matters. Answer these first thing — they gate real work.
>
> When a decision is made it **graduates to an ADR** in `docs/adr/` and is
> **removed** from this queue (CLAUDE.md §6.3).

_(empty — nothing blocked)_

Recently resolved:
- **D1 — WHOOP timezone (offset vs IANA)** → [ADR-0006](docs/adr/0006-timezone-offset-vs-iana.md)
- **D2 — Calories-burned precedence** → [ADR-0007](docs/adr/0007-calorie-burned-precedence.md)

---

## D3 — How to namespace HealthKit sub-sources (MyFitnessPal / Foodnoms / OKOK scale) 🔒

**Context (from T5.1 recon):** the export has multiple apps writing the same
metric — two food loggers (MyFitnessPal per-meal, Foodnoms per-item) and
multiple weight writers (OKOK scale + MyFitnessPal). Per §2.3 these must stay
**sibling rows** and resolve at read time, so we need to tell them apart by
`source`.

**The constraint:** `raw_events.source` is a fixed `CHECK (source IN (...))`
list that includes bare `healthkit` but not `healthkit:myfitnesspal`. SQLite
**cannot ALTER a CHECK** — widening it means rebuilding `raw_events`, which
**holds real WHOOP data**. §8.5 / §5 forbid dropping a table with real data
unattended. So I did **not** touch `raw_events`.

**What I did (safe, no migration):** T5.3 will store HealthKit rows with
`raw_events.source = 'healthkit'` (already allowed), preserve the app name in the
payload (`sourceName`), and make `external_id` a deterministic hash of
`(type, sourceName, startDate, value)` so MFP vs Foodnoms vs OKOK never collide.
Raw stays sacred and untouched.

**The open question — canonical sibling distinction (blocks clean T5.4):**
`food_entry.source` / `weight_measurement.source` also have fixed CHECK lists
(both include bare `healthkit`). To keep OKOK-scale weight and MFP weight as
distinct siblings we need a per-app discriminator. Options:

1. **(Recommended) Add a non-destructive `source_app` column** to `food_entry`
   and `weight_measurement` via `ALTER TABLE ADD COLUMN` (the same safe pattern
   as migration 0004's `utc_offset`). Keep `source='healthkit'`, set
   `source_app='myfitnesspal'|'foodnoms'|'okok'`. Resolver views add `source_app`
   to their precedence ordering. No table rebuild, no CHECK problem, raw
   untouched. Clean and reversible.
2. Rebuild `food_entry`/`weight_measurement` (both currently **empty**,
   disposable canonical) with a broadened `source LIKE 'healthkit:%'` CHECK, and
   use `source='healthkit:okok'` etc. More faithful to the `healthkit:<app>`
   naming in TASKS_PHASE5, but rebuilds tables + dependent views.
3. Collapse all HealthKit into `source='healthkit'` and accept that MFP-weight
   and scale-weight are **not** distinguished. **Rejected** — breaks the
   multi-source sibling requirement (§2.3) the recon specifically found.

**Recommendation: option 1.** It's a two-way door, non-destructive, and mirrors
an existing accepted pattern (0004). I stopped here rather than pick a schema
shape that's awkward to reverse. Pick one and T5.4 proceeds.
