# ADR-0006 — Store UTC offset and IANA zone in separate columns (D1)

Status: Accepted · Date: 2026-07-18 · Implements CLAUDE.md §2.6 / §9 D1

## Context

CLAUDE.md §2.6 requires every timestamp to carry a UTC instant, the **IANA
timezone name**, and a `day_key`. The WHOOP v2 API provides only a UTC **offset**
(`-05:00`), never an IANA name, and there is no reliable offset→IANA mapping
(many zones share an offset; DST is ambiguous).

The first cut (pre-decision) stored the offset string *in* `tz_name` via an
`offset_tz_name()` helper. That overloads one column with two different kinds of
value — an IANA name and a raw offset — which §2.6 explicitly forbids and which
would mislead any consumer that assumes `tz_name` is a zone.

## Decision

Split the two concerns into their own columns:

- **`utc_offset`** — the raw offset string (`-05:00`), or `NULL` when the source
  gives none. `Z` canonicalizes to `+00:00`.
- **`tz_name`** — **strictly IANA**, `NULL` when unknown. For WHOOP (offset-only)
  it is always `NULL`; it stays reserved for IANA-capable sources (HealthKit can
  backfill a true zone later).

`day_key` is derived from instant + offset and is exact regardless of whether the
named zone is known — correctness was never at risk; this is about honest schema.

Migration `0004_utc_offset.sql` adds `utc_offset` to `recovery` and `workout`.
`recovery_resolved` uses `SELECT *`, so it picks up the column with no view
rebuild. `timeutil.offset_tz_name()` is replaced by `normalize_offset()` (returns
`None` for absence, never `"UTC"`). Normalizers set `utc_offset=<offset>`,
`tz_name=None`.

## Consequences

- Rows written before this migration have `utc_offset = NULL` until
  `coach normalize --rebuild` re-derives them from raw (§2.1 makes this safe).
- Absence is represented as absence (§2.7): a missing offset is `NULL`, not a
  fabricated `"UTC"`.
- Adding an IANA-capable source later requires no schema change — it just
  populates `tz_name`.
- Supersedes the earlier "store the offset in `tz_name`" approach entirely.
