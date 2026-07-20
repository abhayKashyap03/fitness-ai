# ADR-0008 — Per-app source discriminator (`source_app`)

**Status:** Accepted (2026-07-19) · resolves **D3**

## Context

Apple Health export recon (T5.1) found that a single canonical `source`
(`healthkit`) fronts **multiple writer apps** for the *same* metric:

- **Weight:** OKOK smart scale **and** MyFitnessPal both write `BodyMass`.
- **Food:** MyFitnessPal (per-meal) **and** Foodnoms (per-item) both write dietary
  records.

§2.3 requires these to coexist as **sibling rows** and resolve at **read time** —
never merge-on-write. That means canonical rows need a per-app discriminator so a
scale weigh-in and an app-typed weight on the same day stay distinct (and so two
food apps don't SUM into a double-counted day).

The constraint: `raw_events.source` is a fixed `CHECK (...)` list. SQLite cannot
`ALTER` a CHECK, so admitting `healthkit:<app>` there would mean **rebuilding the
sacred `raw_events` table** (holds real WHOOP data) — forbidden unattended (§8.5).

## Decision

Keep `source = 'healthkit'` everywhere (already in every relevant CHECK). Add a
**non-destructive `source_app TEXT` column** to `food_entry` and
`weight_measurement` via `ALTER TABLE ADD COLUMN` — the same safe pattern as
migration 0004's `utc_offset`. `source_app` holds the writer slug
(`okok` | `myfitnesspal` | `foodnoms` | …), **NULL** for single-writer sources.

- **Raw stays sacred and untouched** — no `raw_events` rebuild. The writer app is
  preserved in the raw payload (`sourceName`) and folded into the deterministic
  `external_id`, so raw rows never collide across apps.
- **Resolver views are recreated** (0005) to split siblings by
  `(source, source_app)` and to let a real scale (`okok`) outrank an
  app-mirrored weight (`myfitnesspal`) within the `healthkit` source.

## Alternatives rejected

2. **Rebuild `food_entry`/`weight_measurement` with a `source LIKE 'healthkit:%'`
   CHECK** and use `source='healthkit:okok'`. More faithful to the recon's
   `healthkit:<app>` naming, but rebuilds tables + dependent views for no gain
   over an additive column, and drifts `source` semantics away from the fixed
   vocabulary the resolver keys on.
3. **Collapse all HealthKit into one `source`, don't distinguish apps.** Breaks
   the §2.3 sibling requirement the recon specifically surfaced (scale weight vs
   MFP weight; MFP food vs Foodnoms food) — rejected.

## Consequences

- Adding the column is reversible and mirrors an accepted pattern (0004).
- The `source`+`source_app` pair, not `source` alone, is now the provenance key
  for HealthKit-fronted data. Precedence lives in the recreated views
  (`weight_resolved_daily`, `food_daily`) — the one obvious place (ADR-0001).
- This does **not** solve the *raw* `raw_events.source` CHECK rigidity for a
  genuinely new top-level source (e.g. `myfitnesspal` CSV) — that is **D4**, and
  still needs a signed-off raw rebuild. `source_app` is orthogonal to it.
