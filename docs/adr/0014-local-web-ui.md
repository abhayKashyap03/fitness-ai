# ADR-0014 — Local web UI (lifting the §11 UI ban and §6.4 framework ban)

## Status
Accepted 2026-07-26, with explicit user sign-off. Supersedes the blanket
prohibitions in §11 ("Any UI", "Web servers, REST APIs") and §6.4 ("web
frameworks") **for a local, single-user UI only**.

## Context
§3 set the condition for a UI plainly: *"Local CLI, no server, no UI — prove the
spine before any UI."* That gate is now cleared:

- Live ingest works (WHOOP + MyFitnessPal), running daily on real data.
- `normalize --rebuild` is byte-identical on the real database.
- `coach eval grounding` and `eval hrv` pass.
- The plan layer (P7) is live-verified. 328 tests green, schema v10.

The CLI has also proven the seams a UI needs. `coach/tools.py` handlers already
return JSON-serializable dicts with explicit nulls and insufficient-data markers,
`services/sync.py::run_sync()` returns data rather than printing, and
`status`/`tdee`/`plan status` already support `--json`. A UI is a second consumer
of contracts that exist, not a new architecture.

Meanwhile CLI friction is a real risk to the project (risk #8 — the coach starves
if the user stops logging), and the user wants to actually *use* what is built.

## Decision
Build a **local web dashboard**: FastAPI serving server-rendered HTML plus a JSON
API, reading the existing compute/coach layers.

1. **Scope is local and single-user.** No auth, no hosting, no multi-tenancy.
   This is not the P11 multi-tenant backend; it is the personal UI. §11's ban on
   auth systems, billing, and multi-tenancy machinery **still stands**.
2. **The web layer is a presentation boundary, exactly like the CLI.** It calls
   `coach/tools.py` handlers and `services/sync.py`. It contains **no arithmetic
   and no domain logic** (§2.2). If a UI needs a number that does not exist, the
   number gets added to the compute layer with tests — never to a template.
3. **Absence renders as absence** (§2.7). "Not logged" is displayed as not
   logged, insufficient data as an explicit need/have marker. A template must
   never render a missing value as `0` or a dash that reads like zero.
4. **Binds `127.0.0.1` by default.** This serves personal health data with no
   authentication; exposing it on a network by default would be indefensible.
   Binding elsewhere requires an explicit `--host` and prints a warning.
5. **Dependencies are an optional extra** (`pip install -e ".[web]"`):
   `fastapi`, `uvicorn`, `jinja2`. The core CLI keeps its three-dependency
   footprint; someone who only wants the CLI installs nothing new.
6. **FastAPI over stdlib**, deliberately. §6.4 prefers the standard library, and
   `http.server` could technically serve this. But the JSON API written here is
   the same contract the future iOS app consumes, and FastAPI is the shape the
   P11 backend will use — so this code carries forward instead of being
   rewritten. The dependency buys real future work, which is the bar §6.4 sets.

## Consequences
- The user gets a usable dashboard now, and the API contract that the iOS app
  (P13) will consume gets exercised early against real data.
- **Honest limitation:** this is a laptop-local server. Phone access works only
  over LAN with the laptop awake. Genuine daily phone use still requires P11
  (backend) + P13 (app). This is a dashboard, not a pocket app.
- Three new dependencies exist to maintain, isolated behind an optional extra and
  a single module (`src/coach/web/`). If they rot, the CLI is unaffected.
- Two documents drift out of date unless updated: CLAUDE.md §11 and §6.4. Both
  are amended to point here.
- The temptation this creates is putting logic in the UI because it is faster
  than adding a compute function with tests. That is the failure mode to watch;
  point 2 exists to make it a reviewable violation rather than a judgment call.
