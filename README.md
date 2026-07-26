# Unified AI Health & Fitness Coach

A personal, single-user (n=1) AI coach that reads **all** of one person's health
data — WHOOP recovery/HRV/sleep/strain, MyFitnessPal food + weight, and
training — into one grounded local store, then guides structured cuts and bulks.

Two things make it different from every existing product:

1. It sees recovery + food + weight + training **together**.
2. It **never hallucinates your numbers** — code computes every value; the LLM
   only narrates. If data is missing it says so; it never interpolates.

See [`CLAUDE.md`](CLAUDE.md) for the full architecture and non-negotiable
principles, and [`docs/adr/`](docs/adr/) for the decisions behind them.

## Data sources

| Source | Role | Status |
|---|---|---|
| WHOOP Cloud API (OAuth) | recovery, sleep, strain, workouts | ✅ live daily |
| MyFitnessPal v2 API (session cookie) | food diary + weight | ✅ daily driver ([ADR-0009](docs/adr/0009-myfitnesspal-direct-api.md)/[0010](docs/adr/0010-override-mfp-scraping-ban.md)) |
| Apple Health export (.xml/.zip) | body-comp backfill (OKOK scale etc.) | ✅ occasional, manual |
| WHOOP local BLE (Adapter B) | subscription-free recovery after membership ends | 🔬 recon done ([ADR-0012](docs/adr/0012-ble-adapter-approach.md)); hardware spike pending |

Multiple sources for the same fact coexist as sibling rows; precedence is
resolved at **read** time, never by overwriting data.

## Requirements

- Python **≥ 3.11** (developed on 3.14 — see
  [ADR-0003](docs/adr/0003-runtime-and-tooling.md)).
- SQLite (bundled with Python). No server, no cloud, no UI.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # then fill in your values (never commit .env)
```

Every `.env` value is documented in [`.env.example`](.env.example):

- **WHOOP** — OAuth app credentials from <https://developer.whoop.com>
  (needs an active membership), then `coach auth whoop` once.
- **MyFitnessPal** — paste your logged-in browser session cookie as
  `MFP_SESSION_COOKIE` (lasts ~weeks; re-paste when ingest 401s).
- **Coach LLM** — provider-agnostic. Default is Google Gemini
  (`GOOGLE_API_KEY`, free tier at <https://aistudio.google.com/apikey>);
  Anthropic (`COACH_LLM_PROVIDER=anthropic`) and xAI Grok
  (`COACH_LLM_PROVIDER=grok`, `XAI_API_KEY`) are a config switch away.

## Usage

```bash
coach db init                      # create/upgrade the DB from schema/migrations
coach auth whoop                   # one-time WHOOP OAuth
coach ingest whoop --since <date>  # first backfill (then incremental automatically)
coach ingest mfp   --since <date>  # first MFP backfill (diary + weight)

coach sync                         # the daily driver: WHOOP + MFP + normalize
coach status [--date YYYY-MM-DD]   # one day's rollup (recovery/food/weight/training)
coach tdee                         # adaptive TDEE from weight trend + logged intake
coach plan set --rate -0.5 --goal-weight 78   # set a cut/bulk plan (ADR-0013)
coach plan status                  # daily calorie goal + timeline + adherence
coach ask "am I on track for my cut?"         # the coach — grounded in YOUR data

coach db backup                    # consistent online snapshot
coach db verify                    # integrity + row counts + rebuild fingerprint
coach doctor                       # config/db/token sanity report
coach eval hrv                     # is HRV signal or noise on YOUR data? (no tokens)
coach eval grounding               # live zero-fabrication eval (burns tokens)
coach normalize --rebuild          # re-derive ALL canonical rows from raw
```

An occasional Apple Health export ingests with
`coach ingest healthkit --file export.zip` (or `coach sync --hk-file …`).

## Data & privacy

All health data stays **local** in a single SQLite file (`COACH_DB_PATH`,
default `./data/coach.db`). The `data/` directory and every `*.db` / `.env` are
gitignored. Raw payloads are stored verbatim and append-only; canonical tables
are fully regenerable from raw (`coach normalize --rebuild` proves it
byte-identically). The only network calls are to the source APIs and the
configured LLM provider — and the LLM sees computed summaries, never your raw
data or credentials.

## Development

```bash
ruff check . && ruff format --check .   # lint + format
mypy src                                # types
pytest                                  # tests (no network, ever)
```

Every normalizer and compute function has unit tests; adapters are tested
against recorded fixtures in `tests/fixtures/`, never live API calls. Schema
changes only via numbered forward-only migrations in
[`schema/migrations/`](schema/migrations/). Track progress in
[`TASKS.md`](TASKS.md) and [`SESSION_LOG.md`](SESSION_LOG.md).
