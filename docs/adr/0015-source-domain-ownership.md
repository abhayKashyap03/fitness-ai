# ADR-0015 — Each domain has one authoritative source

## Status
Accepted 2026-07-27 (user's call). Refines §2.3 read-time resolution and updates
the §12 source table.

## Context
Sources overlap. WHOOP auto-detects "workouts" and reports calories for them;
MyFitnessPal records exercise the user logs by hand. Both write `workout` rows,
so something has to decide which one a day's training actually *is*.

Discovering this was not theoretical: 23 days in the real database carried the
same session from both sources, and compute counted each one twice — including
its calories — because MFP's start times are placeholders (every logged walk at
08:30, months apart) that no time-window dedup can ever match to the strap's
measured instant. That is §5's "expected bug class" arriving exactly as
predicted.

## Decision
**Each domain has one authoritative source, resolved at read time (§2.3).**

| Domain | Authoritative | Why |
|---|---|---|
| Training / exercise | **MyFitnessPal** | The user logs sessions there deliberately. It is the record of what was trained. |
| Food / nutrition | **MyFitnessPal** | Already the daily driver (ADR-0009). |
| Recovery, HRV, sleep, skin temp, resting HR | **WHOOP** | What the strap is worn for; MFP has no equivalent. |
| Weight / body composition | **Apple Health** (scale), then MFP | A real scale outranks a typed-in number (ADR-0008). |

**WHOOP is a recovery instrument, not a fitness tracker, in this system.** Its
auto-detected workouts and their calorie estimates are a by-product of wearing
it; they are not the training log. So `_SOURCE_RANK` ranks `myfitnesspal` above
`whoop_api` for `workout`, and a session seen by both reports the user's own
numbers.

**One exception, by design:** `strain` is WHOOP-proprietary and has no MFP
equivalent. It is aggregated **separately, once per session group**, so flipping
the ranking never silently drops a health metric. This is the general rule —
precedence is per *domain*, and a metric only one source can produce is taken
from that source regardless of who wins the row.

Nothing is deleted. WHOOP workout rows remain as siblings (§2.3) — they carry
strain, they feed the HRV-validation harness's next-day-strain probe, and if
this decision is ever reversed it is a one-line ranking change with no migration.

## Consequences
- Training numbers now reflect what the user actually logged, not what a strap
  inferred. Calories-burned stop being double-counted on overlapping days.
- Cross-source grouping cannot rely on timestamps where one side hand-logs; the
  normalizer folds placeholder-time rows into the same-day/same-sport group.
  **Trade-off:** two genuinely distinct same-sport sessions in one day, one
  strap-detected and one hand-logged, collapse into one. Under-counting a session
  is the safer error — double-counting inflates burn and flatters the cut.
- Adaptive TDEE is unaffected either way: it runs on weight trend + intake and
  deliberately never uses workout calories (ADR-0005/0007). The blast radius of
  this decision is display and coach narration, not the number steering the cut.
- If an in-app exercise logger ships (P12), it becomes the authoritative training
  source and MFP moves down the same ranking — again one line.
