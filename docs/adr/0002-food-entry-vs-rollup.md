# ADR-0002 — Food: entries are the fact, daily totals are a view

Status: Accepted · Date: 2026-07-18

## Context

A day's nutrition can be represented two ways: store each logged food as a row
and compute the day's totals on demand, or store a per-day rollup row and update
it as entries change. The task (T0.1) asked us to justify the call, and to make
two states distinguishable that a naive schema conflates: **"hasn't logged yet
today"** vs **"genuinely ate nothing"**.

## Decision

### 1. The entry is the fact; the daily total is a derived VIEW.

`food_entry` holds one row per logged item (or per source-provided daily total,
or per declared fast). `food_daily_by_source` and `food_daily` are **views** that
SUM entries per day. No day-total is ever stored.

Rationale:

- **§2.1 — canonical is regenerable.** A stored total is a second source of truth.
  Edit/delete an entry and the stored total is instantly wrong until something
  remembers to recompute it. A view is always consistent by construction.
- **The only reason to store totals is read performance at scale.** At n=1 that is
  irrelevant; if it ever matters it becomes a materialized view, not a schema
  redesign.
- Partial macros stay honest: SUM() skips NULLs, so instead of letting a partial
  log look complete, the view **exposes completeness counts**
  (`items_missing_kcal_n`, `items_missing_macros_n`, `is_complete`).

Sources that only expose a per-day total (no item breakdown) are still modeled as
a single `food_entry` with `entry_type='daily_total'` — one row, summed like any
other. The entry-vs-rollup distinction is a *column on the fact*, not a separate
storage strategy.

### 2. "Not logged" vs "ate nothing" is encoded by row PRESENCE + `entry_type`.

- **No rows for a `day_key`** ⇒ NOT LOGGED. Unknown. `food_daily` has no row for
  that day, so downstream code sees absence, never a misleading `0`.
- **A row with `entry_type='fast'`** ⇒ KNOWN ZERO. The user declared they ate
  nothing (more). `is_fast=1`, `kcal_total=0`, `is_complete=1`.

A 0-kcal *item* (black coffee) is deliberately NOT a fast — only an explicit
`fast` row asserts intent. This is the load-bearing distinction: the coach must
say "you haven't logged today" on an empty day and "you fasted" on a declared-zero
day, and never confuse the two.

### 3. Cross-source resolution picks ONE source per day (no double-count).

`food_daily` applies source precedence (`manual > healthkit/health_connect >
food-db`) and takes the single highest-precedence source present that day, so two
sources logging the same meal never sum to 2× calories.

**Tradeoff, explicitly:** this assumes a source that logs a day is logging the
*whole* day, not a supplement to another source. If a user ever logs breakfast in
HealthKit and dinner manually on the same day, the resolved total will undercount
(it takes manual only). For the single-user v0 with one active food source per day
this is correct and safe; a future "merge/supplement mode" is deferred, not
precluded — the sibling rows and `food_daily_by_source` view retain everything
needed to build it.

## Consequences

- Totals are always correct-by-construction and cost nothing to keep in sync.
- Missing/partial data is visible, not silently zeroed — required for §2.2
  (the coach says "I don't have that" rather than inventing).
- Multi-source *merge* for food is an open future decision, flagged here and in
  `food_daily`'s comment so it is findable when it becomes real.
