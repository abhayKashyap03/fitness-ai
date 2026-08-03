# ADR-0019 — The host is the owner's own instance; the beta is deferred

## Status
Accepted 2026-08-02. Decided by the human across a single questioning session;
recorded here after the fact. **Amends [ADR-0018](0018-multi-tenant-foundation.md)**,
whose "invite-only beta, ~5–30 users" framing this supersedes, and reorders
ROADMAP P11.

## Context
ADR-0018 lifted §11's ban on auth and multi-tenancy and built the foundation for
an invite-only beta. The machinery landed and works. What had never been examined
is the premise underneath it: *who is the hosted instance actually for?*

Put plainly, the answer is the owner. The dashboard is useful on a phone, the
laptop is the wrong place for live data to live, and none of that requires a
second human. The beta was a plan attached to the same infrastructure, not the
reason for it.

Leaving the record as-is is the real cost. A future session reading ADR-0018 and
ROADMAP P11 would find an invite-only beta described as live work and plan
against it.

## Decision

**1. The hosted instance is the owner's own. The beta is deferred, not
cancelled.** Multi-tenancy is insurance that happens to already be built.

**2. The host is the only instance.** The laptop stops holding live data. The
web UI is the primary surface; the CLI is reached over SSH. There is no
laptop↔host sync to design, because there is only one live database.

**3. WHOOP is authorised on the host, via a public redirect URI.** Register
`https://<domain>/callback` and complete the OAuth flow there rather than
copying the laptop's token up. This was chosen over the alternative on an axis
worth recording: it keeps a rollback path alive, because the host authorises
itself instead of stealing the only working token.

**4. A small VPS with a self-run reverse proxy** (Caddy class). Not a PaaS, not
home-server-plus-tunnel. TLS terminates at the proxy; `coach web` keeps speaking
plain HTTP to it.

**5. Backups are a nightly `coach db backup`, pulled off-host to the laptop.**
The laptop's role inverts: it stops being the primary and becomes the archive.
**The restore must be rehearsed, not assumed** — an untested backup is a belief,
not a backup.

**6. Cutover is one-shot.** Copy the database up, verify the canonical
fingerprints match on both machines, host goes live, and the laptop database is
**renamed, never deleted**.

**7. Recovery-informed inference stays on the roadmap.** Recommended for cutting;
the human chose to keep pursuing it.

**8. It is gated on a pre-registered test: n = 150 HRV days**, judged by the
existing `hrv_verdict()` thresholds (lag-1 autocorrelation ≥ 0.30 **and** a
next-day |r| ≥ 0.20). Meets the bar → build on it. Misses → cut it, on that date,
without relitigating. Pre-registering the threshold *before* the data arrives is
the whole point; risk #6 asks for an honest null and this is how it gets one.

**Narration ships now regardless.** "Recovery is low, train lighter" needs no
statistical claim — §8.6 already sanctions treating recovery as a signal. Only
*computation* driving numbers waits on the gate.

**9. ADR-0018's medical-disclaimer prerequisite stays armed but does not fire.**
The invite and member machinery is retained as hosting infrastructure. "No second
human logs in until medical disclaimers land" remains true and remains binding —
it is simply not a scheduled task, because no second human is expected. It is a
tripwire, not a milestone.

## Consequences

**Ordering changes.** Decision 3 makes a domain and TLS **prerequisites** of
WHOOP ingest on the host, not follow-on polish. The order is now: VPS → domain →
TLS → headless OAuth → migrate → cron sync → rehearse restore.

**Four gaps this exposes, all currently unbuilt:**

- `run_login` (`src/coach/adapters/whoop/flow.py`) calls `webbrowser.open` and
  binds a local `HTTPServer` for the callback. **It cannot run on a headless
  host.** A variant is required.
- `coach web` runs plain uvicorn and terminates no TLS. That is correct — the
  proxy does it — but it means the proxy is load-bearing for cookie `Secure`
  flags, not optional.
- **Nothing in the repo schedules anything.** Nightly sync needs host cron.
- WHOOP tokens still live in a file (`.credentials/u<id>/whoop_token.json`), not
  the encrypted `user_secret` store built in ADR-0018. Wiring that up is unbuilt.

**`COACH_DB_PATH` is a filesystem path handed to `sqlite3.connect`.** There is no
remote-database mode and none is planned (ADR-0018 decision 2). That is precisely
why decision 2 here forces SSH-or-web: the CLI must run *on* the host.

**What this does not change.** No code is deleted. The compute, coach and tool
layers are untouched. ADR-0018's security decisions — hashed session tokens,
scrypt passwords, per-user AEAD secrets — all stand; they now protect one user's
data on a public network, which is a smaller claim but not a weaker one.
