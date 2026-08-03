# ADR-0018 — Multi-tenant foundation: invite-only beta, SQLite on a volume, per-user secrets encrypted at rest

## Status
Accepted 2026-08-02. Lifts the remaining §11 ban (auth, multi-tenancy, hosted
backend) that [ADR-0014](0014-local-web-ui.md) deliberately left standing.
Implements ROADMAP P11. Decided by the human; the three forks below were put to
them explicitly rather than chosen here.

**⚠️ Amended same day by [ADR-0019](0019-hosting-the-owners-instance.md).**
Decision 1 below — "invite-only beta, ~5–30 users" — is **no longer the plan**.
The hosted instance is the owner's own; the beta is deferred, not cancelled. The
invite and member machinery is **retained as hosting infrastructure** and nothing
here is removed. Decisions 2 and 3 (SQLite on a volume; per-user secrets
encrypted at rest) are unaffected and remain in force. The medical-disclaimer
prerequisite in *Consequences* also remains in force — read it as an armed
tripwire rather than a scheduled task, since no second human is expected.

## Context
The local web dashboard proved useful enough on a phone (LAN, added to the home
screen) that the next step is a hosted backend other people can reach. §11 has
banned exactly this from day one — "auth systems, user registration, billing,
multi-tenancy machinery" — with good reason: it is the classic side-project
graveyard, and none of it makes the coach better at coaching.

Two facts make the lift cheaper than §11 feared.

**`user_id` is already on every row** (§2.4, from the first migration), so
multi-tenancy is an *activation*, not a schema migration. That column has been
carried for exactly this moment.

**Credentials are already namespaced per user** on disk (`.credentials/u<id>/`,
fixed 2026-07-25 after an external review caught a real collision bug). The
shape is right; only the storage medium changes.

What is genuinely new and genuinely dangerous: on a laptop, a WHOOP refresh
token and an MFP session cookie sit in a file only the owner can read. On a host,
they sit in a database that could be dumped, backed up, or restored somewhere
else — and they are credentials to *other people's* health accounts.

## Decision

**1. Invite-only beta, ~5–30 users. No public signup.**
_(Superseded by [ADR-0019](0019-hosting-the-owners-instance.md) — the beta is
deferred and the host is the owner's own instance. The no-public-signup rule
below still holds, and the machinery it describes is kept.)_

Registration, email verification, password reset and abuse handling are each a
project in themselves and none of them are the product. An invite creates the
user; the invitee sets a password once. If this ever needs public signup, that is
a later, additive decision.

**2. SQLite on a persistent volume. Not Postgres.**

ROADMAP P11 assumed hosted Postgres. That was wrong at this size. SQLite keeps
all 13 existing migrations, every view, and the gap-aware EWMA (cross-validated
against pure Python in ADR-0011) working **unchanged**. Porting them would be
weeks of migration risk for no benefit at n≤30, and would mean re-validating the
one piece of SQL whose correctness we proved by hand.

This is a **two-way door**: the compute layer speaks plain SQL through one
`sqlite3` connection, so moving later is a real but bounded project. Revisit when
concurrent writers, not row counts, become the problem.

**3. Per-user source credentials are encrypted at rest in the DB, with the key
supplied by the host environment.**

The key lives in the host's environment and **never in the database**, so a
stolen dump, a leaked backup, or a restored snapshot yields ciphertext alone.
This needs a real AEAD, which the Python standard library does not provide —
hence one new runtime dependency, `cryptography` (§6.4 sign-off recorded here).
Everything else stays stdlib: `hashlib.scrypt` for passwords, `secrets` +
`hmac.compare_digest` for session tokens.

Three consequences of that choice are load-bearing:

- **Session tokens are stored hashed, never in plaintext.** The DB holds
  SHA-256 of the token; the raw token exists only in the user's cookie. A dump
  cannot be replayed as a live session.
- **`user_secret` is the one table we deliberately do NOT keep history for.**
  Everything else here is append-only on principle (§2.1, `plan`, `coach_note`,
  `llm_call`). A rotated credential is different: a superseded refresh token has
  no analytical value, and retaining every historical ciphertext only widens the
  blast radius of a future key compromise. Secrets are updated in place.
- **Encryption is per-user-per-name**, so one compromised value is one value.

## Consequences

**Good.** The phone gets a real backend. `user_id` finally earns the eleven
months it has been carried. Credentials stop living in a laptop `.env`. The
compute, coach and tool layers are untouched — they already take `user_id` as a
parameter, so nothing about the grounded-answer guarantee changes.

**The cost.** This is the largest surface expansion in the project's history and
every item §11 warned about is now real: sessions expire, passwords get
forgotten, a host goes down, backups must actually be tested. None of it makes
the coaching better.

**§8.6 stops being theoretical.** The calorie floor and maximum-loss-rate
guardrails currently protect one consenting adult who wrote them. With other
people logging in they protect strangers, and medical disclaimers move from
`[Backlog]` to a prerequisite. **No second human logs in until that lands.**

**Not decided here.** The host itself, the backup mechanism, billing (there is
none), and whether ingest runs server-side per user or stays laptop-driven. Each
is a separate, smaller decision that this foundation does not force.
