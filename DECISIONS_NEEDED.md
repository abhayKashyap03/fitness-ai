# Decisions Needed

> Claude Code appends here when it hits a **one-way door** it shouldn't decide
> alone. Each entry: what's blocked, the options, its recommendation, why it
> matters. Answer these first thing — they gate real work.
>
> When a decision is made it **graduates to an ADR** in `docs/adr/` and is
> **removed** from this queue (CLAUDE.md §6.3).

## D5 — Cut/bulk target model 🔒 (gates Phase 7, the plan layer)

**Blocked:** the plan layer (Phase 7) can't start until the target *shape* is
fixed — it's a one-way door because it defines the `plan` schema and every
downstream number (daily calorie goal, projected timeline, on/off-track delta).

**Options:**
- **(A) Goal-weight + deadline** — user declares "83 → 78 kg by Oct 1"; code
  derives the required weekly rate and calorie deficit. Intuitive to set;
  can imply an unsafe rate (then §8.6 clamps and the timeline stretches — the
  clamp must win, and the coach says so).
- **(B) %/week rate** — user declares "lose 0.5%/week"; timeline is emergent.
  Safer by construction (rate is the direct lever, bounded by the §8.6 ceiling);
  less intuitive, no fixed end date.
- **(C) Both, rate is canonical** — accept either input, store the rate; a
  deadline is just a convenience that computes an initial rate, immediately
  clamped. One stored quantity, either entry style.

**Recommendation: (C).** Rate is the physiologically safe, guardrail-aligned
lever; a deadline is a nice front-end affordance that reduces to a (clamped)
rate. Avoids fake precision from a deadline that the safety floor can't honor.

**Why it matters:** picking A now and switching to rate later is a schema +
recompute migration. Deciding once is cheap; reversing is not.

---

_(no other open decisions)_

Recently resolved:
- **D1 — WHOOP timezone (offset vs IANA)** → [ADR-0006](docs/adr/0006-timezone-offset-vs-iana.md)
- **D2 — Calories-burned precedence** → [ADR-0007](docs/adr/0007-calorie-burned-precedence.md)
- **D3 — HealthKit sub-source namespacing (`source_app`)** → [ADR-0008](docs/adr/0008-healthkit-source-app.md) (option 1, per handoff; shipped in migration 0005)
- **D4 — MyFitnessPal ingestion path + `raw_events.source` CHECK** → [ADR-0009](docs/adr/0009-myfitnesspal-direct-api.md) + [ADR-0010](docs/adr/0010-override-mfp-scraping-ban.md). Resolved differently than the queued options: user signed off to **override §12** and ingest **directly from MFP's v2 API** (daily; cookie auth), and to **drop the `raw_events.source` CHECK entirely** (§2.5) rather than extend it. Shipped in migration 0006.
