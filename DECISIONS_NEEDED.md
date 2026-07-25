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
- **D4 — MyFitnessPal ingestion path + `raw_events.source` CHECK** → [ADR-0009](docs/adr/0009-myfitnesspal-direct-api.md) + [ADR-0010](docs/adr/0010-override-mfp-scraping-ban.md). Resolved differently than the queued options: user signed off to **override §12** and ingest **directly from MFP's v2 API** (daily; cookie auth), and to **drop the `raw_events.source` CHECK entirely** (§2.5) rather than extend it. Shipped in migration 0006.
