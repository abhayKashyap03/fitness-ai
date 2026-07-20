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
- **D3 — HealthKit sub-source namespacing (`source_app`)** → [ADR-0008](docs/adr/0008-healthkit-source-app.md) (option 1, per handoff; shipped in migration 0005)

---

## D4 — MyFitnessPal food ingestion path + the `raw_events.source` CHECK 🔒

**Context (2026-07-19c):** HealthKit nutrition is unusable — MFP stopped writing
to Apple Health after Oct 2025 (Premium paywalled the sync), leaving only 5
logged days in the whole export. The user's real Feb–June food history lives on
MFP and comes out via MFP's **Privacy Center "Download My Data"** full export
(CSV, free, legal data-portability — **not** the closed API, **not** scraping;
CLAUDE.md §12 updated). Zip expected ~2026-07-20. Plan: build
`src/coach/adapters/mfp/` reading that CSV.

**The constraint:** per §2.1 the CSV must land in `raw_events` verbatim, but
`raw_events.source` is a fixed `CHECK (source IN ('whoop_api','whoop_ble',
'healthkit','health_connect','withings','strava','oura','garmin','manual'))` —
**no `myfitnesspal`**. SQLite can't ALTER a CHECK; admitting a new source means
**rebuilding `raw_events`, which holds real WHOOP data** → §8.5 human sign-off.
(`food_entry.source` has the same gap but that table is **empty/disposable**, so
its CHECK can be rebuilt freely — the raw table is the only hard part.)

**Deeper issue:** this CHECK list contradicts §2.5 ("adding a source should be a
new adapter file, not a schema change"). Every future adapter hits this wall.

**Options:**
1. **(Recommended, minimal) One human-approved migration** rebuilds `raw_events`
   with `'myfitnesspal'` added to the CHECK, preserving every existing row via
   `INSERT INTO ... SELECT` and asserting row-count parity before/after. Raw data
   fully preserved; done once under review. Gets MFP flowing fastest.
2. **(Recommended, aligned — do as follow-up ADR) Replace the source CHECK with a
   `sources` reference table + FK.** Adding an adapter becomes an INSERT, not a
   DDL rebuild — satisfies §2.5. More work now; kills this whole class of blocker.
   Still a raw_events rebuild (needs sign-off), so fold it into the same reviewed
   migration as option 1 if you want it done right the first time.
3. Store MFP raw under an existing allowed value (`'manual'`) with the app in the
   payload. **Rejected** — `'manual'` means hand-entered; violates provenance
   (§2.3) and source-agnostic honesty.

**Recommendation:** option 1 to unblock, **or** option 1+2 combined in one
reviewed migration if you'd rather fix the root cause now. Either way this needs
your explicit sign-off (raw_events rebuild) — the next session will prep the
migration with row-count assertions and wait for approval, not run it unattended.
Independent of this, MFP **recon** and the pure `food_entry` normalizer can be
built first (no raw_events write).

