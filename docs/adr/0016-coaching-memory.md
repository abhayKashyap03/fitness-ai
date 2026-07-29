# ADR-0016 — Coaching memory is code- and human-authored, never model-authored

## Status
Accepted 2026-07-28. Implements the P10 "coaching memory & consistency" item.

## Context
The coach answers every question from scratch. It has the *data* (compute layer)
but no record of its own *decisions*, so guidance drifts: it can suggest a deload
today, forget it tomorrow, and contradict itself the day after. Continuity is the
difference between a query tool and a coach.

The obvious implementation — let the model write notes to itself and read them
back — is exactly the wrong one for this codebase. §2.2 exists because the model
must never be the source of a number. A model-authored memory launders that rule:
the model writes "TDEE was 2100" today, reads it as established fact next week,
and a fabrication has become persistent truth that nothing recomputes. The
zero-fabrication guarantee (the project's #1 differentiator, §1.2) would then
depend on the model never having been wrong once.

## Decision
**Memory holds decisions and observations, authored by CODE or the HUMAN. The
model reads it and never writes it.**

1. **`coach_note`** — append-only. Never edited, never deleted; a superseded note
   is followed by a new one, so the record of what was believed *at the time*
   stays intact (same reasoning as `plan`'s append-only history, ADR-0013).
2. **Authors are `user` or `system`.** `system` notes are written by
   deterministic code at real decision points — currently when a plan is set
   (direction, rate, goal). `user` notes come from `coach note add`. There is no
   third author.
3. **Notes must not carry numbers the compute layer owns.** A note records *that
   a decision was made and why* ("set a cut at -1.0%/week to reach 75 kg"), not a
   measurement. Anything measurable is re-derived from compute at read time, so a
   stale note can never be served as a current number.
4. **The model reads via a tool** (`get_coach_notes`) like any other data, so its
   answers stay grounded in what it was actually given.

## Consequences
- Continuity without a new fabrication surface. The worst case is a note that is
  *out of date*, which reads as history ("on 2026-07-20 you set…"), not as a
  present fact.
- The coach cannot record its own reasoning unprompted. That is a real capability
  cost, taken deliberately — it is the conservative half of a one-way door, and
  the open question is recorded in DECISIONS_NEEDED.md rather than decided by
  implementation. Reversing later is additive (a new author value); reversing the
  other direction would mean auditing every note ever written.
- Append-only means the table grows. At n=1, a note per plan change plus manual
  entries is negligible; no pruning policy is defined, and inventing one now
  would be premature (§11).
