# Unified AI Health & Fitness Coach

A personal, single-user (n=1) AI coach that reads **all** of one person's health
data — WHOOP recovery/HRV/sleep/strain, nutrition, body weight/composition, and
training — into one grounded store, then guides structured cuts and bulks.

Two things make it different from every existing product:

1. It sees recovery + food + weight + training **together**.
2. It **never hallucinates your numbers** — code computes every value; the LLM
   only narrates. If data is missing it says so; it never interpolates.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture and non-negotiable
principles, and [`docs/adr/`](docs/adr/) for the decisions behind them.

## Status

Early. Building one thin vertical slice at a time (ingest → raw → canonical →
compute → query) on WHOOP first. Track progress in [`TASKS.md`](TASKS.md) and
[`SESSION_LOG.md`](SESSION_LOG.md).

## Requirements

- Python **3.14** (`requires-python = ">=3.14"` — see
  [ADR-0003](docs/adr/0003-runtime-and-tooling.md)).
- SQLite (bundled with Python). No server, no cloud, no UI.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # then fill in your values (never commit .env)
```

Required `.env` values are documented in [`.env.example`](.env.example). WHOOP
API credentials come from <https://developer.whoop.com> (needs an active
membership). The `ANTHROPIC_API_KEY` is only needed for the coach layer (later).

## Usage

```bash
coach db init        # create/upgrade the local SQLite DB from schema/migrations
coach db status      # show current schema version + pending migrations
```

More commands (`auth`, `ingest`, `normalize`, `status`) land as the WHOOP slice
and compute layer come online.

## Data & privacy

All health data stays **local** in a single SQLite file (`COACH_DB_PATH`,
default `./data/coach.db`). The `data/` directory and every `*.db` / `.env` are
gitignored. Raw payloads are stored verbatim and append-only; canonical tables
are fully regenerable from raw.

## Development

```bash
ruff check . && ruff format --check .   # lint + format
pytest                                  # tests
```

Every normalizer and compute function has unit tests; adapters are tested
against recorded fixtures in `tests/fixtures/`, never live API calls.
