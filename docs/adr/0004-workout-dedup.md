# ADR-0004 — Workout dedup via session_group_id

Status: Accepted · Date: 2026-07-18

## Context

The same real workout can arrive from several sources (WHOOP + Apple Watch +
Strava). Summed naively, one run becomes 3× calories/strain. We need to count it
once without destroying the per-source rows (§2.3).

## Decision

Keep every source row; attach a shared `session_group_id` to rows that represent
the same real session. Compute (T3) then aggregates **per group**, not per row.

**Grouping rule** (`normalize/dedup.py`), tolerance configurable
(`--tolerance`, default **300 s**):

Two workouts join the same group iff all hold:
1. same `user_id`,
2. same canonical `sport_type` (a concurrent run + lift are different sessions),
3. start times within `tolerance_s`, and
4. their `[start, end]` intervals overlap.

Overlap (4) on top of the start-window (3) prevents merging genuinely
back-to-back sessions of the same sport that happen to start close together.

`session_group_id` is deterministic — `grp:{user}:{sport}:{anchor_start}` where
the anchor is the group's earliest member — so `--rebuild` reproduces identical
grouping. `dedupe_hash` stores the same key.

## Consequences

- Cross-source double-counting becomes a solved, tested case, not a lurking bug.
- Tolerance is a knob, not a constant, because the right window depends on how
  clock-synced the sources are; 300 s is a safe default for WHOOP-vs-phone drift.
- Greedy single-pass grouping is O(n·g). At n=1 volumes this is nothing; if it
  ever matters, sort-based bucketing replaces it without changing the rule.
- Limitation: matching is by time+sport only. Two different people's data or a
  mislabeled sport could mis-group — not possible at n=1, revisit for multi-user.
