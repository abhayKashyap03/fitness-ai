# Decisions Needed

> Claude Code appends here when it hits a **one-way door** it shouldn't decide
> alone. Each entry: what's blocked, the options, its recommendation, why it
> matters. Answer these first thing — they gate real work.
>
> When a decision is made it **graduates to an ADR** in `docs/adr/` and is
> **removed** from this queue (CLAUDE.md §6.3).

## D6 — May the coach author its own memory? 🔒 (not blocking; conservative half shipped)

**Shipped conservatively (ADR-0016):** `coach_note` accepts `user` and `system`
authors only. Code writes a note when a plan is set; the human writes the rest.
The model **reads** memory and never writes it.

**The open question:** should the model be allowed to append its own notes
("advised a deload because recovery was low 4 days running")?

- **(A) Keep it closed (current).** No new fabrication surface. Cost: the coach
  can't record its own reasoning, so continuity only covers decisions that pass
  through code or you.
- **(B) Allow model-authored notes, marked `model`.** Richer continuity. Risk: a
  wrong number written today is read back as established fact next week and
  nothing recomputes it — the zero-fabrication guarantee (§1.2/§2.2) would then
  rest on the model never having erred once.
- **(C) Model proposes, human confirms.** Notes land in a pending state and only
  become readable once approved. Safe, but adds a review queue to a tool whose
  whole point is low friction (risk #8).

**Recommendation: stay on (A) until the grounding eval routinely passes at
scale.** Reversing toward (B) later is additive — a new author value plus a
filter. Reversing *out* of (B) would mean auditing every note ever written.

**Why it matters:** memory is the one place a model error becomes permanent
rather than per-answer.

---

_(nothing else blocked)_

Recently resolved:
- **D5 — Cut/bulk target model** → [ADR-0013](docs/adr/0013-plan-target-model.md). Option C (user, 2026-07-26): accept goal-weight+deadline OR %/week, store the (§8.6-clamped) rate as the one canonical driver; deadline is convenience only. Implemented in migration 0010 + `compute/plan.py`.
- **D1 — WHOOP timezone (offset vs IANA)** → [ADR-0006](docs/adr/0006-timezone-offset-vs-iana.md)
- **D2 — Calories-burned precedence** → [ADR-0007](docs/adr/0007-calorie-burned-precedence.md)
- **D3 — HealthKit sub-source namespacing (`source_app`)** → [ADR-0008](docs/adr/0008-healthkit-source-app.md) (option 1, per handoff; shipped in migration 0005)
- **D4 — MyFitnessPal ingestion path + `raw_events.source` CHECK** → [ADR-0009](docs/adr/0009-myfitnesspal-direct-api.md) + [ADR-0010](docs/adr/0010-override-mfp-scraping-ban.md). Resolved differently than the queued options: user signed off to **override §12** and ingest **directly from MFP's v2 API** (daily; cookie auth), and to **drop the `raw_events.source` CHECK entirely** (§2.5) rather than extend it. Shipped in migration 0006.
