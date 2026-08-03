# Session Log

> Rolling handoff (CLAUDE.md §6.3). Current state + latest sessions only. Older
> sessions → `docs/sessions/`; `git log` is the real history.

---

## Session 2026-07-30 (b) — protein target: a setting that was stored and never read

`plan set --protein` has written `protein_g_per_kg` to the plan row since Phase 7
and **nothing ever consumed it** — the 0010 migration comment says "future use".
A user could set a protein target and the tool would silently ignore it.

Worth naming: the 2026-07-26 **placeholder sweep missed this**. That sweep looked
for *fake output* (like the old `eval hrv` legend), and this produces no output at
all — it accepts input and discards it. A different failure shape than the one
being hunted.

- **`protein_status()`** (pure, `compute/plan.py`) — target grams/day from g/kg ×
  **trend** weight, the same choice every other steering number makes (raw daily
  weight is noise; a target that moves 0.8 kg overnight is not a target).
- **No default g/kg, deliberately.** A recommended protein intake is a coaching
  opinion about a real body; supplying one would put a number the user never chose
  in front of them as if it had been measured. Unset → no target (§2.7).
- **Not-logged is not a missed target.** With no logged protein the target still
  shows and adherence stays `None` — never "short" for a day the tool knows
  nothing about.
- **A bug I introduced and caught by running it:** my first version put protein
  inside `PlanStatus`, which returns `Insufficient` when TDEE is missing — so the
  target vanished for the ten logged-intake days it takes TDEE to appear, i.e.
  exactly when a user has just set it. Now reported *alongside* status, because it
  only needs the trend weight. One pure function, three surfaces (tool, CLI, web).
- +10 tests. **551 green**; ruff + mypy clean.

**Live-verified** on a throwaway DB (deliberately *not* the real one — g/kg is the
user's call, not mine to pick): target renders with insufficient TDEE, reads
`NOT LOGGED` with no food, and `147.5 g target / 120 g logged / −27.5 g / short`
with food. Web plan page screenshotted showing the line below the insufficient
daily goal.

**Also re-ran `coach eval hrv`** (free): still **NOISE** — autocorr +0.199, best
next-day r 0.133, 72 HRV days (unchanged, so no new recovery data in the window).
Recovery→macro stays correctly gated.

---

## Session 2026-08-02 — multi-tenant foundation (ADR-0018, migration 0014)

The §11 lift, for real. The human chose all three forks explicitly: **invite-only
beta (~5–30)**, **SQLite on a persistent volume** (not Postgres), **per-user
secrets encrypted at rest with a host-held key**. Recorded in
[ADR-0018](docs/adr/0018-multi-tenant-foundation.md) before any code.

**Also a correction worth keeping:** I had repeatedly called the BLE spike
"blocked, needs the strap for an evening", parroting ADR-0012 and TASKS' Blocked
list. The user has worn the strap daily for the whole project. It was never
blocked on hardware availability — only on nobody doing it. Check whether a
documented blocker is still real before repeating it.

- **Migration 0014** — `app_user`, `user_invite`, `user_session`, `user_secret`.
  `user_id` has been on every row since 0001 (§2.4); this cashes it in. Nothing
  existing changes: user 1 is seeded as the owner, **unclaimed** (no email, no
  password), which is the honest state rather than a fabricated address.
- **Passwords and tokens are never stored.** scrypt (stdlib) for passwords;
  sessions and invites persist only SHA-256 of the token, so a stolen dump can't
  be replayed as a live session.
- **`user_secret` is the one deliberately non-append-only table.** Everything
  else keeps history on principle (§2.1); a superseded refresh token has no
  analytical value and retaining every ciphertext only widens the blast radius.
- **AES-256-GCM with `user_id`+`name` as AAD**, so a ciphertext lifted from one
  user's row fails to decrypt under another's — tested by transplanting a row.
- **One new dependency, `cryptography`** (§6.4 sign-off in ADR-0018). The stdlib
  ships no AEAD and hand-rolling one here would be the wrong instinct.
- **Bug found in my own code, before shipping:** `login()` carried a comment
  claiming timing-equalisation it did not implement — `verify_password`
  short-circuits when no digest exists, so an unknown email answered measurably
  faster than a real one. That is an account-enumeration oracle by stopwatch, on
  a health app. Added `_burn_password_work()` and a ratio test.
- +80 tests. **639 green**; ruff + mypy clean.

**Nothing is wired to the web app yet** — merging this changes no behavior. That
is PR B.

⚠️ **§8.6 stops being theoretical here.** The calorie floor and max-loss-rate
guardrails currently protect one consenting adult who wrote them. With other
people logging in they protect strangers. **Medical disclaimers must land before
a second human logs in** — recorded in ADR-0018 as a hard prerequisite, not a
backlog item.

---

## Session 2026-08-02 (b) — auth wired into the web app (PR B)

Where the localhost-only assumption dies. Every route used to hardcode
`user_id=1`; now the request decides.

**The fail-closed rule** is the load-bearing part, not the login form. Exactly one
case may proceed without a session: **loopback bind AND nobody has claimed an
account** — the existing single-user laptop workflow, which must not break. The
moment either condition stops holding:

- any user has a password → auth required even on localhost (you cannot lock the
  door and leave the back window open);
- non-loopback bind with no claimed account → **the app refuses to start**, with
  the commands to fix it. A runtime warning nobody reads is not good enough when
  the failure is "personal health data served to a network with no credentials".

- **Per-request user via a ContextVar** set by middleware, rather than threading a
  parameter through two dozen handlers that are not about authentication.
  Closed by default: only `/login`, `/invite`, `/healthz`, `/static` are public,
  so a route added tomorrow is protected by existing rather than by remembering.
- **API gets 401, pages get 303 to /login** — a `fetch()` cannot follow a
  redirect usefully and a browser cannot do anything with a 401 body.
- Cookies HttpOnly + SameSite=Lax, Secure whenever the request arrived over HTTPS
  (so a TLS proxy gets it automatically without breaking plain-HTTP localhost).
- **`coach user genkey|list|set-email|set-password|invite`.** `set-password`
  prompts without echo and has no `--password` flag on purpose: a flag puts the
  secret in shell history and the process list. `genkey` prints to stdout and
  writes nowhere.
- **Bug caught by the suite, not by me:** background jobs (sync/ingest/normalize)
  run on other threads and outlive the request, so the ContextVar was gone by the
  time they executed. Now the submitting user is captured at submit time and
  re-established inside the worker — which also means a job can never act as
  somebody else because it happened to run later.
- **Bug caught by running it:** `coach web` printed "Dashboard: http://…" *before*
  building the app, so a refusal was preceded by a cheerful URL that never became
  true. Also replaced the now-false "NO authentication" warning with an accurate
  one about the missing HTTPS.
- +17 tests. **656 green**; ruff + mypy clean.

**The test that matters** is `test_a_member_cannot_see_the_owners_data`: two
tenants, one database, owner has a weigh-in, member has none. If the member's
session ever returns 83.0 kg, multi-tenancy is broken and one person's health data
is reaching another. Passed first run, as did "a member's plan lands on their own
account".

**Live-verified** on a throwaway DB: network bind unclaimed refuses with
instructions; after `set-email` + `set-password` it starts; anonymous `GET /` →
303 `/login`, `/api/status` → 401, `/healthz` → 200; after login both → 200. Login
page screenshotted (nav correctly hidden when signed out).

⚠️ Still true from ADR-0018: **medical disclaimers must land before a second human
logs in.** The invite page carries a plain-language "not a medical device" notice,
which is a start, not the whole obligation.

---

## Session 2026-08-02 (c) — the record caught up with the plan (ADR-0019)

No behaviour changed. The repo was **describing a different project than the one
being built**, which is the kind of drift that costs a whole session to discover.

The human ran a questioning session and made eight decisions. None had been
written down. `ADR-0018` still read as a live commitment to a 5–30 user
invite-only beta, and `ROADMAP` P11 planned against it — so the next session
would have picked up work for a beta that is not happening.

- **[ADR-0019](docs/adr/0019-hosting-the-owners-instance.md)** records all eight.
  The load-bearing one: **the hosted instance is the owner's own**, and the host
  becomes the **only** instance — the laptop stops holding live data and becomes
  the backup archive. WHOOP is authorised **on the host** via a public
  `https://<domain>/callback`, which is why **domain + TLS are prerequisites, not
  polish**, and which keeps a rollback path alive (the host authorises itself
  rather than stealing the laptop's only working token).
- **The beta is deferred, not cancelled** (option C). No code deleted; the invite
  and member machinery is retained as hosting infrastructure. ADR-0018's "no
  second human logs in until medical disclaimers land" prerequisite **stays
  armed** — a tripwire, not a scheduled task.
- **Recovery-informed inference is pre-registered, not abandoned**: judged at
  **n = 150 HRV days** on the existing `hrv_verdict()` thresholds (autocorr ≥ 0.30
  AND next-day |r| ≥ 0.20). Meets → build; misses → cut, on that date. Fixing the
  threshold before the data arrives is what makes a null honest (risk #6).
  **Narration is not gated** and ships now — "recovery is low, train lighter"
  needs no statistical claim (§8.6). Only computed numbers wait.
- **Committed the human's own `/account` nav fix** (+3 tests). The tab was
  owner-gated, so an invited member had no route to the password-change form and
  no reset flow exists. Owner-only controls inside the page stay gated, pinned by
  a test.
- **Two stale blockers corrected in TASKS.** The BLE spike was never blocked on
  hardware — the strap is worn daily; it needs an evening and `bleak` (§6.4
  sign-off). "Live WHOOP API verification — needs real credentials" was removed
  outright; WHOOP has ingested live since 2026-07-25. Both had been repeated
  forward unchecked. **Verify a documented blocker is still real before
  repeating it.**

**668 green; ruff + mypy clean** — unchanged, as expected for a docs commit.

### Four gaps ADR-0019 exposes, all unbuilt
1. `run_login` (`adapters/whoop/flow.py`) calls `webbrowser.open` and binds a
   local `HTTPServer` — **cannot run on a headless host**. Needs a variant.
2. **Nothing in the repo schedules anything.** Nightly sync needs host cron.
3. WHOOP tokens still live in `.credentials/u<id>/whoop_token.json`, not the
   encrypted `user_secret` store ADR-0018 built for them.
4. `COACH_DB_PATH` goes straight to `sqlite3.connect` — there is **no remote-DB
   mode**, which is why the CLI must be reached over SSH.

⚠️ **The owner has no password set.** `app_user` row 1 is owner/active with an
email and no digest. Once #30 merges, `coach web --host 0.0.0.0` **refuses to
start** — LAN phone access breaks until `coach user set-password` is run.

---

## Session 2026-08-02 (d) — hosting prerequisites: everything except the credit card (PR #31)

Asked to take the project to the finish in one run. **It cannot be** — and the
reasons are structural, not effort: buying a VPS and registering a domain are
purchases; the BLE spike needs a radio talking to the strap; P9's recovery
engine is pre-registered to n=150 HRV days (72 today) and building it now would
destroy the pre-registration decided the same day; App Store and legal review
are the human's. So the work taken was the slice that is needed under **every**
branch: everything ADR-0019's hosting plan requires that does not need a card.

Four commits on `feat/medical-disclaimers`, stacked on #30.

**1. Medical disclaimer on every advice surface** —
[ADR-0020](docs/adr/0020-medical-disclaimer.md), migration 0015. Discharges
ADR-0018's prerequisite. Only the invite form carried a notice; the dashboard,
plan page and coach chat carried nothing. One canonical text now feeds the web
UI, the CLI and the model's `SYSTEM_PROMPT`, so the user and the model are
provably held to the same scope — this repo has shipped the
same-logic-in-two-places bug twice already, and here it would be invisible from
both sides. Acknowledgement is **recorded and versioned**; a footer nobody reads
is the failure the auth work already rejected. Gate lives in the middleware,
covers `/api/`, and leaves `/logout` reachable — a gate you cannot retreat from
is a trap. **Applies to open-local too**: that allowance is about credentials,
this is about advice.

**2. Rehearsed restore** — `coach db rehearse-restore` (destroys nothing, safe
on cron) and `coach db restore --yes`. ADR-0019 §5 demanded a rehearsal and
there was nothing to rehearse with. The real restore verifies the snapshot
**before** touching anything and **preserves** the database it replaces (§8.5).
A stale backup is reported as a *delta*, not a failure — calling a
behind-by-a-day backup broken trains you to ignore the check.

**3. Headless WHOOP OAuth** — `coach auth whoop --headless`. `run_login` opens
a browser and binds a local socket; neither exists on a VPS. Both flows share
one `_exchange()`, because a headless path that forgot the state check would be
a real vulnerability no test of the laptop path would catch. A bare code is
refused: state is the only proof the code came from the login you started.

**4. Cron-ready sync + deploy artifacts + runbook** — Caddyfile, systemd unit
(loopback-bound, hardened), cron file, a laptop-side `pull-backup.sh` that
**pulls** so the host holds no credential that can write to the archive, and
[docs/DEPLOY.md](docs/DEPLOY.md) in ADR-0019's forced order.

### Two real bugs, both found by running it rather than by a test
- **`run_sync` conflated "not configured" with "auth needed"** and always exited
  0. Moved to cron, a revoked token would ingest nothing every night while the
  job reported success every night — a silent failure with no upper bound.
  `needs_attention` now separates them and sync exits 1.
- **`--quiet` was not quiet.** It suppressed the command's own output and then
  printed fifteen httpx INFO lines — a nightly email about nothing, which is how
  a real failure ends up filtered away unread.

Also fixed: both restore commands raised a raw `sqlite3` traceback on a corrupt
snapshot. Data was intact, but a stack trace mid-incident does not answer the
only question that matters ("did my live database survive?").

**724 green (+56); ruff + mypy clean.** Live-verified against the real DB and
real APIs: disclaimer gate end to end; rehearsal against a real snapshot (2402
raw_events, fingerprint identical) and a full restore drilled into a scratch
target; `coach sync --quiet` silent, exit 0.

⚠️ **The live database is now at v15** — that sync applied migration 0015
(additive, forward-only). The web app will therefore ask you to accept the
safety notice once.

**Two existing tests were changed, not deleted** (§6.2), both behaviourally:
`test_localhost_with_no_account_claimed_still_works` acknowledges first, and
`test_invite_link_creates_an_account_and_signs_in` now asserts a new account
gets 403 until it accepts.

### Still needs the human
- **VPS + domain** — purchases; not something I can do.
- **Owner password** — `coach web --host 0.0.0.0` refuses to start without one.
- **BLE spike** — `bleak` signed off and the strap is worn daily, but the
  acceptance gate is empirical and wants someone at the keyboard.

---

## Session 2026-08-03 (b) — Adapter B ingest, encrypted credentials, own food logging

Continued straight through after the gate passed. Four more commits.

**BLE live-HR ingest (`coach ble record`).** The gate turned up something better
than the plan assumed: standard SIG Heart Rate (`180d`/`2a37`) carries **RR
intervals**, and RR intervals are what HRV is computed from. So a textbook
`hrv_rmssd_ms` is available from an independent instrument with **no reverse
engineering at all** — 2a37 is a public spec WHOOP cannot quietly change without
breaking every generic HR app. That is §5's "objective measurement, comparable
across sources" arriving from Adapter B, which is the whole calibration play.
Writes `whoop_ble` sibling rows; wired into `normalize_all` so `--rebuild`
covers it.
- **No composite score is emitted, deliberately.** A "textbook recovery score"
  in the same column would look like WHOOP's number and not be it. `score` is
  NULL; `score_method='textbook'`, `is_official=0`.
- Care where a wrong answer looks plausible: RR arrives in **1/1024 s ticks**
  (treating them as ms scales every HRV figure by 2.4% and nothing looks
  broken); energy-expended sits *between* HR and the RR block; artefacts are
  filtered *before* the sufficiency check; thin data yields None, not a small
  number. +25 tests, byte-level against the SIG layout.
- ⚠️ **Not live-verified.** Recording needs the radio and this agent's process
  is still refused Bluetooth by macOS TCC — the grant reached the human's own
  Terminal, not the agent's. Parser, HRV math and normalizer are covered
  offline; the notification subscription itself is unproven.

**Credentials into the encrypted store** (`services/credentials.py`). ADR-0018
built `user_secret` and nothing was wired to it. The rule: **if
`COACH_SECRET_KEY` is configured, the database wins** — not a flag, because a
flag makes the secure path the one you have to remember. Migration **moves**: an
existing file is adopted on first load, then renamed aside (never deleted, §8.5).
Once the DB holds a value it wins outright, so a stale file cannot override a
rotated token.

**Own food logging on Open Food Facts (P12).** The sanctioned nutrition path —
MFP is a personal-only override (ADR-0010) that must never ship. `coach food
search|log` plus a **Food page in the dashboard**, because logging from a CLI
over SSH is not a food-logging feature (risk #8). The 0002 schema already listed
`openfoodfacts` as a valid source, so no migration.
- The dominant risk is **unknown macros silently becoming zero**. Crowd-sourced
  products are half-filled, and defaulting fibre to 0.0 under-reports intake in
  exactly the direction self-reporting is already biased (risk #7). Unknown
  stays None through parsing, scaling and into a NULL column.
- Live-verified against the real API: Nutella logged with `protein=1.9,
  fiber=NULL` beside Coke with `protein=0.0`. **Those two zeros mean different
  things**, and both survive to the day total.
- **USDA FoodData Central is a clean seam, not built** — one vertical slice
  beats two half-built ones (§3), and it needs an API key this install lacks.

### A pre-existing portability bug, found on the way
`ruff`'s `target-version` was `py314` while `requires-python` is `>=3.11`. At
py314 the **formatter rewrites** `except (A, B):` into PEP 758's unparenthesized
`except A, B:` — a **SyntaxError on 3.11–3.13**. Two such lines had already
shipped (`normalize/myfitnesspal.py:113` predates this session). The package
installs on those versions, because the metadata allows it, and then fails at
import. Both fixed; `target-version` now tracks the floor.

⚠️ **This deviates from a written decision.** CLAUDE.md §3 explicitly permits
ruff's `target-version` to track the dev interpreter. That permission predates
this consequence, and the decision's own rationale is portability — which the
py314 setting was undoing. Flagged rather than done quietly; revert if you
disagree.

**784 green; ruff + mypy clean.** All on `feat/medical-disclaimers` → PR #31.

---

## Session 2026-08-03 — the BLE gate PASSED on the real MG strap 🎉

**The project's single biggest technical risk (§10.1) is retired.** ADR-0012's
acceptance test — does the owner's own MG-variant strap expose the `fd4b`
family — was run in the human's own terminal and **passed**.

`coach ble scan` found `WHOOP MG…` at −53 dBm **advertising the `fd4b` family**,
and `coach ble probe` enumerated four services. Full output, identifiers
scrubbed, in `tests/fixtures/whoop_ble/mg_gatt_probe_2026-08-03.json` — kept as
the **baseline**, because firmware moves (risk #9) and the only way to notice is
to have written down what it looked like when it worked.

**Two findings that shape the adapter:**

1. **`fd4b0006` is absent.** whoop-vault documents `fd4b0002–0007` on r52
   "Maverick"; this MG exposes five (`0002, 0003, 0004, 0005, 0007`). Exactly
   the MG-variant divergence ADR-0012 named as residual risk, now a concrete
   known difference. **The drain must not assume `0006` exists — check this
   first when building it.**
2. **Live HR needs no reverse engineering.** Standard SIG Heart Rate
   (`180d`/`2a37`) is exposed. 1 Hz HR with no `fd4b` framing, no CRC, no
   handshake — a fallback that survives independently of firmware pushes
   breaking the proprietary protocol. Materially de-risks §10.9.

Also present: Battery (`180f`/`2a19`) and Device Information (`180a`).

**Getting there took disproving two blockers and finding a third.** "Needs the
strap" was false for the whole project. "Needs a `bleak` decision" was signed
off. The real one was **macOS TCC**: `BleakScanner.discover()` died with SIGABRT
(exit 134) and no Python exception, C stack showing
`__TCC_CRASHING_DUE_TO_PRIVACY_VIOLATION__`. Granting Bluetooth to the host app
**and restarting it** cleared it — the restart is the easy-to-miss part, since
TCC caches per responsible app at launch.

**Not built:** the historical drain, and any ingest. ADR-0012 §4 gates ingest on
this result, and the result only just arrived. Nothing writes `whoop_ble` rows
yet, so `compute/calibration.py` still has no honest cross-instrument pair.

---

## Where the code stands (verified 2026-08-02)

- **668 tests green; ruff + mypy clean. Schema at v14** (migrations 0001–0014).
  PRs #12–#29 merged; web auth (#30) open for review.
- **Zero-fabrication eval at 50 scenarios covering all 9 tools — 50/50 passing
  live on Grok.** Zero fabrications found.
- **LLM spend is recorded and reportable** (`coach cost`); rates unset, so spend
  reads UNPRICED until the human fills in `COACH_PRICE_*_PER_MTOK`.
- Phases 0–7.5 done, plus the source-ownership fix (ADR-0015). WHOOP + MFP (food+weight) run **live**; the coach (`coach
  ask`) answers grounded questions live; it **steers** — a cut/bulk plan sets a
  daily calorie goal + timeline + adherence; and there is now a **local web
  dashboard** (`coach web`, ADR-0014). `coach eval grounding` and
  `eval hrv` both pass. Recovery + sleep + weight + food + plan show together on
  one `coach status` screen — the §1 thesis, working, in daily use.
- **Data types all have a canonical home:** recovery (0001), workout, food
  (0006), weight/body-comp (0005/0007), sleep (0009), plan (0010 — first
  user-authored table). `raw_events` is sacred and append-only; `normalize
  --rebuild` proven **byte-identical on the real DB**.
- **LLM layer is provider-agnostic** (§2.5 applied to vendors): Google Gemini
  (default, free tier), Anthropic, and xAI Grok — each a different wire shape,
  one module apiece, agent loop vendor-blind. Grounding verified 3/3 on Grok.
- CLI surface: `db init|status|backup|verify`, `auth whoop`, `ingest
  whoop|healthkit|mfp`, `normalize [--rebuild]`, `status`, `tdee`, `plan
  set|status`, `ask`, `eval grounding|hrv`, `doctor`, `sync`, `web`.
- **GateGuard disabled** via `.claude/settings.local.json` — **user-authorized
  2026-07-19; do NOT re-flag** the §8.2 tension. File stays untracked.
- **Open item (unchanged):** `~/.zshrc` plaintext API keys flagged, unrotated —
  user's call, do not act.

### What's next → see [TASKS.md](TASKS.md) ▶ NEXT UP
**Hosting the owner's own instance** ([ADR-0019](docs/adr/0019-hosting-the-owners-instance.md)),
in the order that ADR forces: VPS → domain → TLS → headless OAuth → migrate →
cron sync → **rehearse the restore**. Set an owner password first, or #30 merging
takes LAN phone access down. The **BLE spike** (ADR-0012) is *not* blocked on
hardware — the strap is worn daily and always has been; it is blocked only on
someone doing it, and it needs `bleak` (a dependency needing §6.4 sign-off).

---

## Session 2026-07-26 (b) — Phase 7: the cut/bulk plan layer (PR #16, merged)

The first thing that *steers* rather than observes. D5 resolved as **option C**
([ADR-0013](docs/adr/0013-plan-target-model.md)): a signed target rate is the one
canonical driver; a goal-weight+deadline is convenience that reduces to a
§8.6-clamped rate. Deterministic all the way — the LLM narrates the goal, code
computes it (§2.2), clamped by the guardrails (§8.6).

- **Migration 0010 (schema v10)** — `plan` table, the first USER-AUTHORED table
  (not raw-derived; no raw_ref, absent from the rebuild/fingerprint). Append-only
  history, one `is_active` row per user. `store/plan.py`.
- **`compute/plan.py`** (pure) — daily calorie goal = adaptive TDEE + the rate's
  energy delta (KCAL_PER_KG), through the calorie-floor clamp; timeline
  projection; `Insufficient` without TDEE/trend. `guardrails.clamp_target_loss_rate`
  makes an unsafe deadline safe at set time (timeline stretches, honestly).
- **`get_plan_status`** — 7th coach tool, so `coach ask` reasons about adherence.
- **CLI** — `coach plan set (--rate | --goal-weight --by | --maintain) [--protein]`,
  `coach plan status`, plan line in `coach status`.
- **Mid-cut adoption** — `plan set --start-date/--start-weight` backdates the
  anchor; `plan_status` then reports progress + a deterministic adherence label
  (on_track/ahead/behind/wrong_way). A same-day plan shows no progress yet (§2.7).
- **Live-verified** (T7.5): set/status/adherence work on the real DB. The daily
  calorie goal reads `Insufficient` until TDEE has 10 logged-intake days
  (ADR-0005) — by design, fills in with a few more days.
- +26 plan tests. **328 tests green; ruff + mypy clean.**

---

## Session 2026-07-29 (b) — grounding eval 10 → 50 scenarios

The P10 "expand the zero-fabrication eval toward ~50 queries" item (also the
Notion validation bucket's "~50 coaching queries"). The eval was two-sided but
thin: 10 scenarios touching 4 of the 9 tools, so five tool surfaces had **no
fabrication guard at all**.

- **50 scenarios**, covering **all 9 tools** — a test now asserts that coverage
  (`test_scenario_set_covers_every_tool`), so adding a tool without a scenario
  fails the suite rather than quietly leaving a gap.
- 15 new seed helpers. Grouped: absence-per-tool (8), present-data-must-be-
  reported (12), partial-data traps (10), computed layers — TDEE/plan/safety (10).
- **New traps worth naming:** an *intentional fast* is logged, not missing (the
  mirror of not-logged-isn't-zero, and the eval had no case for it); HRV present
  while the composite score is NULL (§5's objective-vs-composite split made
  adversarial); a nap-only day has no night sleep; `strain` absent on a
  hand-logged MFP session (ADR-0015); a §8.6 alert that must be surfaced, AND a
  safe series where inventing a warning is the failure.
- **Deliberate rule on expected values:** stored numbers are asserted exactly in
  `must_state_numbers`; **computed** ones (EWMA trend, TDEE, calorie goal) are
  not — hand-writing an expected EWMA into a test literal would be the eval
  doing the arithmetic §2.2 forbids. Those scenarios stay two-sided via
  `must_admit_absence=False` plus a substrate check that a real value is served.
  Session duration is likewise not demanded: the tool serves seconds and "97
  minutes" is a unit conversion, not a fabrication.
- **Cost (§8.7):** a full run is now 50 live agent loops, 5× before. Added
  `select_scenarios()` + `coach eval grounding --only <substr> --limit N` so one
  failing scenario can be debugged without paying for 50, and the runner prints
  the run size before spending it.
- **Fixed a real reporting gap:** the CLI never printed `omitted_numbers` on
  FAIL. Omission became a failure mode when the eval went two-sided (2026-07-27)
  and has been scored-but-invisible since — a stonewalling failure would have
  shown `fabricated=[]` and looked inexplicable.
- +85 tests. **524 green**; ruff (check + format) + mypy clean.

### Live run: 38/50 → **50/50**, and 11 of the 12 failures were the SCORER's

Ran live on Grok (~$0.12 for 50 scenarios). First run: **38/50**. Every failure
was worth reading, and almost none were the model's:

- **Thousands separators.** "you logged 1,800 calories" tokenized as `1` and
  `800` — the same correct sentence scored as an invented 800 AND an omitted
  1800.
- **Unit conversion** (4 scenarios). Tools serve 5820 seconds / 460 minutes; the
  model said "97 minutes (1 hour 37 minutes)". A code comment already asserted a
  unit conversion "is not a fabrication" — the scorer had never been taught it.
- **Sign.** `kg_changed_so_far` is `-0.605`; the model wrote "down 0.605 kg".
- **Absence phrasing, again** (3 scenarios). "You didn't log any training
  sessions" (bare `log`, not `logged`) and "there is no visible upward or
  downward direction yet".

`expand_units()` fixes the middle two and is **bounded on purpose**: sign, plus
s→min→h decomposition of each value *on its own*. It never combines two values,
so open arithmetic between different measurements is still caught — pinned by a
test (an invented 45 against an allowed 5820 still fails).

**The twelfth failure was mine.** `fast_has_no_calorie_figure` demanded the coach
admit absence for a declared fast because the kcal column is NULL. That misreads
§2.7 — the fast row exists to record "ate nothing *deliberately*", as distinct
from a day with no rows. A declared fast **is** known-zero intake, so the model
answering "known zero intake (0 calories)" had the domain right and my assertion
was wrong. Renamed `fast_is_known_zero_not_missing` and inverted.

**Second live run: 50/50.** Every failing answer is kept verbatim as a
regression fixture — observed output, not invented examples — and the same
scenario now carries two phrasings, because the model is not deterministic.

⚠️ **50/50 is not permanent.** A rerun can surface a phrasing the scorer hasn't
seen; that is reconciliation, not regression. The durable claim is narrower: no
*fabrication* was found in 50 scenarios across 9 tools.

---

## Session 2026-07-29 — one plan-set seam (CLI and web had drifted)

Opened the web dashboard and it showed a plan (-0.50%/week) that did not match
what the CLI had last set. **The user had changed it themselves from the
dashboard** — the ordinary use of a form that exists to be used.

The mistake worth recording: I treated a human action as an anomaly and went
hunting for a rogue writer, then "restored" the plan to my own last known value.
That was not a restore; it overwrote a deliberate user change. **When the DB and
my last known state disagree, the DB is authoritative — the user may simply have
used the app.** Ask before reverting.

The investigation did surface a REAL bug, independent of who clicked:

- The CLI wrote a `system` coaching note on every plan change. **The web form
  did not** — because the CLI handler and the web POST handler each carried
  their own copy of the resolve/clamp/anchor/insert logic, and they had drifted.
  A plan the user set in the browser was therefore invisible in their own
  history, which is exactly what made it look mysterious.
- Fix: `services/plan.py::set_active_plan()` — one seam both surfaces call,
  mirroring `services/sync.py` / `services/clients.py`. Entry-style resolution,
  the §8.6 clamp, the start anchor and the audit note now happen in exactly one
  place. A test asserts a plan set via the web form is indistinguishable from one
  set via the CLI, note included.
- +10 tests. **439 green**; ruff + mypy clean.

Plan restored to the user's own setting: **-0.50%/week, goal 75 kg, start
2026-07-20** (1640 kcal/day, no floor clamp, currently AHEAD). `plan` is
append-only so every intermediate row stays visible in history.

---

## Session 2026-07-28 (c) — coaching memory (ADR-0016)

The P10 "coaching memory & consistency" item. The coach had data but no record of
its own DECISIONS, so guidance drifted between sessions.

- **Migration 0012 `coach_note`** — append-only, like `plan`. User/code-authored
  primary data (no raw_ref, absent from the rebuild/fingerprint).
- **The load-bearing decision (ADR-0016): the model READS memory and never
  writes it.** Authors are `user` or `system` only, enforced in both the helper
  and a schema CHECK. A model-authored memory would let a fabricated number
  become persistent truth that nothing recomputes — the zero-fabrication
  guarantee would then rest on the model never having erred once.
- Notes record *that a decision was made and why*, never a measurement, so a
  stale note can't be served as a current number. `coach plan set` now writes a
  `system` note automatically.
- `get_coach_notes` (9th tool) + `coach note add|list`. `dispatch`'s today-filler
  now skips tools with no day anchor.
- **D6 logged in DECISIONS_NEEDED** — whether the model may author notes is a
  one-way door; the conservative half shipped, the question is the human's.
- 10 new tests. **429 green**; ruff + mypy clean.

⚠️ **Note:** the live plan was briefly set to -0.85%/wk while verifying the
auto-note, then restored to -1.00%/wk. The `plan` table is append-only, so both
rows remain in history; the ACTIVE plan matches what it was before.

---

## Session 2026-07-28 (b) — plan layer live on real data; floor-clamp projection bug

Ran the daily driver and verified the plan layer end-to-end for the first time.

- **T7.5 fully complete.** Adaptive TDEE crossed its 10-logged-intake-day gate
  (ADR-0005), so the plan now emits a REAL daily goal: TDEE **2040 kcal**, trend
  82.67 kg, goal 75 kg, adherence **ON_TRACK** (-0.86 kg over 8 days).
- **Per-type watermark confirmed in production:** sync reported "incremental
  since 2026-07-26" where it used to be stuck at 07-21.
- **Bug found by the real numbers (fixed here).** The §8.6 calorie floor bound
  for the first time (a -1%/wk target on a 2040 TDEE implies 1131 kcal, below
  the 1200 floor). ADR-0013 promises the timeline "stretches rather than
  promising an unsafe date" — but `plan_status` projected from the TARGET rate,
  so it promised exactly the date the floor forbids. Added
  `effective_rate_kg_per_week` (derived from the CLAMPED goal); timeline and
  adherence are now judged against what is actually achievable. Live: the
  projection honestly moved 9.3 → 10.0 weeks (Oct 1 → Oct 6).
  Also added `effective_daily_kcal_delta` so surfaces are self-consistent —
  the CLI showed "1200 kcal/day (TDEE -909)", which doesn't add up; now -840.
  Adherence is judged against the achievable rate too, so faithfully following
  a clamped plan no longer reads "behind" forever.
- HRV verdict still **NOISE** (autocorr +0.21 < 0.3). `coach doctor`: all clear.

**For the human:** the -1.00%/week target is at the §8.6 ceiling AND the calorie
floor is binding, so the target rate is not achievable within the safety limits.
Either the timeline stretches (as it now honestly shows) or the target eases.
That is a decision for the user, not the tool.

---

## Session 2026-07-27 — MFP training, sync watermark, source ownership (PR #21)

Two bugs the user reported, both root-caused against the REAL database before any
fix was written (systematic-debugging, not guessing).

- **MFP training was invisible.** MFP diaries carry `exercise_entry` items beside
  the food rows; the normalizer filtered to `diary_meal` only — right for not
  fabricating food calories, but nothing ever picked the exercise up. A walk
  logged in MFP reached `raw_events` and stopped. `parse_exercise` + migration
  0011 (drops the `workout.source` CHECK per ADR-0009). **Recovered 81 sessions
  back to 2026-01-06.** Not treated as workouts: `is_calorie_adjustment` rows
  (device burn adjustments) and entries with no usable duration.
- **Sync re-fetched the same days forever.** `auto_since` took the MIN across
  record types, so a QUIET type pinned every window open: no workouts since 07-23
  meant recovery/sleep/cycle re-fetched from 07-21 every run. `auto_since_by_type`
  resumes each type from its own watermark.
- **Cross-source double-counting, found while verifying.** 23 days had the same
  session in both sources. MFP start times are placeholders (every walk at 08:30,
  months apart) so time-window dedup can never match them — both rows survived and
  compute counted the workout AND its calories twice (§5's expected bug class).
  `_absorb_placeholder_time_sources` folds hand-logged rows into the strap group
  for the same day+sport. Trade-off documented in code: two genuinely distinct
  same-sport sessions collapse into one; under-counting is the safer error.
- **ADR-0015 — source-domain ownership (user's rule).** MyFitnessPal is
  authoritative for training AND food; WHOOP is the recovery instrument
  (HRV/sleep/skin-temp) and its auto-detected workouts are a by-product, not the
  training log. `_SOURCE_RANK` puts MFP first. **`strain` is the documented
  exception** — WHOOP-only, aggregated separately once per group, so flipping the
  ranking can never silently drop a health metric. CLAUDE.md §12 + README both
  said the opposite and were corrected.
- **Plan insufficient marker** now propagates the real reason ("need 10, have 9")
  instead of flattening to "need 1, have 0".
- **Grounding eval made two-sided** (3 → 10 scenarios). It only tested absence, so
  a model that always said "I don't have that" scored a perfect run. Added
  `must_state_numbers` + `omitted_numbers()`; an always-refuse model now provably
  fails the present-data scenarios.

- **HRV probe with real power.** The strain probe is capped at n=24 by WHOOP
  coverage (strain is WHOOP-only). Training DURATION comes from every source and
  81 MFP sessions just appeared, so `dev_vs_next_train_min` tests the same
  hypothesis with more data — counted once per session group. Live r=+0.072
  (n=28); **verdict still NOISE**. The extra power did not rescue the
  differentiator, which is exactly the honest answer risk #6 asks for.
- **Session detail.** The rollup said "N session(s)" and nothing else, so what
  was actually trained stayed invisible. `compute.training_sessions()` + a
  `get_training_sessions` tool (8th) + a list under `coach status` + the
  dashboard training card + `/api/training`. One compute function, three surfaces.

Live-verified: today's walk shows (203 kcal / 2700s); 2026-07-23 reports MFP's
745 kcal / 8160s with WHOOP's strain 15.55 intact, listing 'Strength training'
97min/398kcal and 'Walking, 2.0 mph' 39min/347kcal; groups 152 → 123.

---

## Session 2026-07-26 (c) — the first UI: local web dashboard (PR #20)

§3's gate ("prove the spine before any UI") was met, so the UI got built. This
**lifts two explicit CLAUDE.md bans** — §11 (UI, web servers) and §6.4 (web
frameworks) — scoped to a *local single-user* UI and recorded in
[ADR-0014](docs/adr/0014-local-web-ui.md); both sections amended to point there.
Auth/billing/multi-tenancy stay out of scope.

- **`src/coach/web/`** — FastAPI + Jinja templates. A **presentation boundary
  exactly like the CLI**: every number comes from `coach/tools.py`; no arithmetic
  or domain logic in a route or template (§2.2). That rule is the thing to guard
  — putting logic in the UI because it's faster than adding a tested compute
  function is the obvious failure mode.
- **Absence renders as absence** (§2.7) — "NOT LOGGED" never becomes `0`;
  insufficient data shows its need/have marker.
- **`/api/*` is a pass-through of the tool layer** — the same contract the future
  iOS app (P13) consumes, now exercised against real data. A test asserts
  byte-equality with the tool handler so it can't drift.
- **Pages** — dashboard (single circle + plan + TDEE + weight sparkline), plan
  view/set, coach chat. `coach web` binds **127.0.0.1** by default; any other
  `--host` warns (no auth on personal health data).
- Deps behind the optional **`[web]` extra**; core CLI still 3 dependencies.
- **344 tests green** (+16 web), ruff + mypy clean. **Live-verified**: all routes
  200 on the real DB, real recovery/sleep/weight/food rendering, TDEE honestly
  reporting `need 10, have 8`.
- Known nits, deliberately not fixed here: `plan_status`'s insufficient marker
  reads `need 1, have 0` where the TDEE card says `need 10, have 8` (honest but
  less informative — propagate the underlying marker in compute later);
  `ruff format` fails repo-wide on 30 pre-existing files, untouched to avoid
  unrelated churn.

---

## Session 2026-07-26 — Grok provider, HRV verdict, doc/code sweep

Three pieces of work; all on their own branches → PRs (user reviews/merges).

- **Grok provider (PR #13, merged).** Added xAI Grok as a third LLM provider on
  its own branch (user has Grok API credits for eval). It's the OpenAI
  chat-completions wire shape — genuinely different from Gemini and Anthropic —
  and it dropped in with **zero agent-loop changes**: one module
  (`coach/llm/grok.py`) + one registry line. That's the §2.5 vendor abstraction
  proving itself. Verified against xAI's live docs first (don't guess APIs).
  Grounding 3/3 on real credits.

- **HRV deterministic verdict (PR #14, merged).** The user caught that `coach
  eval hrv` printed a static "reading:" legend regardless of the numbers — the 5
  stat lines were real, but the tool never *concluded*. Now `hrv_verdict()`
  (`compute/hrv_validation.py`) makes a signal/noise/insufficient call from the
  real stats vs fixed thresholds (autocorr ≥ 0.30 AND a next-day |r| ≥ 0.20;
  absolute value, so a strong negative still counts; insufficient sub-stats are
  ignored, never zeroed). **Live result on the real DB: NOISE** — 72 HRV days,
  lag-1 autocorr +0.19, best next-day r 0.14. An honest null on risk #6's core
  bet. It shapes Phase 7: lean on weight-trend + intake, treat recovery lighter.

- **Codebase placeholder sweep (this session).** User asked to find any other
  hardcoded/placeholder/"reserved-for-later" output like the old `eval hrv`.
  Swept CLI + compute + coach + tools. **Finding: `eval hrv` was the only one**
  (already fixed). Everything else computes from real data. Two pieces of
  genuinely-gated future code exist but emit **no** fake output — they're just
  not surfaced yet:
  * `compute/calibration.py` (`calibration_report`/`compare_series`) — real math,
    no live caller; needs `whoop_ble` sibling rows that don't exist until the BLE
    hardware spike lands. (Could run today on healthkit-vs-mfp weight, but no
    command wires it — a surfacing gap, not a placeholder.)
  * `compute/guardrails.py` (`clamp_calorie_target`/`calorie_floor_alert`) —
    built for Phase 7's target layer; fires only when a target is proposed.

- **Docs refreshed:** TASKS.md gained a top-of-file ▶ NEXT UP block (so work
  resumes cleanly after any usage-limit cutoff), Phase 7 sketch, and the
  session-4/5 records; README added Grok; DECISIONS_NEEDED added D5 (cut/bulk
  target model); this log rewritten; pre-session-4 detail archived to
  `docs/sessions/2026-07-19-to-25.md`.

---

## Session 2026-07-25 (b) — sleep slice + live verification + review fixes (PR #12, merged)

Live credentials were available, so several long-"unverified" items are now
verified against the real install and real APIs.

- **Model-has-no-clock fix** (`e81a417`) — the first live `coach ask` had Gemini
  guessing a training-era date and querying empty windows. `ask(today=...)` now
  injects the real date (COACH_HOME_TZ); `dispatch(today=...)` fills omitted date
  args server-side (§2.2 extends to the calendar). An explicit model date still wins.
- **Gemini free-tier retry** (`105e4bd`) — honor the JSON `retryDelay` hint in the
  error body (Gemini never sends a `Retry-After` header); unblocked `eval grounding`.
- **Sleep canonical slice** (`886a8ce`, migration 0009, schema v9) — the last §1
  data type without a home. Objective stage durations are cross-source calibration
  currency; composite percentages carry `score_method`/`is_official` (§5). One row
  per session; naps are sibling rows; `day_key` = the day the sleep ended. Field
  names verified against live payloads + developer.whoop.com. **103 real sessions
  canonicalized; rebuild byte-identical on the real DB.**
- **Calibration stats** (`f1a8922`, `compute/calibration.py`) — bias/MAE/correlation
  over shared days; wired to whoop_api vs whoop_ble when Adapter B lands.
- **Credential namespacing** (`28660f2`) — external review caught a real bug:
  global `.credentials/*.json` meant a second user's WHOOP auth would overwrite
  the first's refresh token. Now `.credentials/u<user_id>/`; legacy file MOVED on
  first access (never copied/deleted). Live-verified incl. an OAuth auto-refresh.
- **Sync service seam** (`f68634b`) — `services/sync.py::run_sync()` returns a
  `SyncResult`; the CLI only formats it. Degradation contract tested with zero
  credentials: one dead source never costs you the others.
- **Grounding scorer fix** (`8ecaa52`) — it was failing correct answers 3/3
  (flagging the tool's own window/insufficient counts and prose dates as
  "fabricated"). Now strips prose dates and derives the allowed-number set from
  what the model was actually given (replays each successful tool call). An
  invented measurement is still caught; faithfully restating a tool's counts is not.

### Still not verified
- `coach eval grounding` full 3/3 pass — free-tier 20 req/min quota throttles a
  back-to-back run. Re-run on fresh quota, or point it at Grok/Anthropic.
- BLE hardware spike (needs the strap; ADR-0012's acceptance gate).
