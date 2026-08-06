# ADR-0020 — Medical disclaimer: one text, recorded acknowledgement, every advice surface

## Status
Accepted 2026-08-02. Discharges the prerequisite recorded in
[ADR-0018](0018-multi-tenant-foundation.md) and re-affirmed in
[ADR-0019](0019-hosting-the-owners-instance.md) ("no second human logs in until
medical disclaimers land"). Implements the §8.6 obligation. Migration 0015.

## Context
§8.6 has said from the beginning that this app steers eating and training for a
real person. ADR-0018 made the point sharper: the calorie floor and
maximum-loss-rate guardrails were written by one consenting adult *for himself*,
and the moment anyone else's data is in the database they are protecting a
stranger.

What actually existed was a paragraph on the invite form. The dashboard, the
plan page and the coach chat — the three surfaces that put a daily calorie
target in front of a person — carried nothing at all. The CLI carried nothing.
The model's system prompt carried two lines, written separately and drifting
freely from the user-facing text.

ADR-0019 then deferred the beta, which could easily have been read as deferring
this too. It does not: the owner is also a person acting on the numbers, and the
host is about to be reachable from the public internet.

## Decision

**1. One canonical text, in `coach/disclaimer.py`.**

The notice appears in the web UI, in the CLI, and inside the model's own system
prompt. This repo has already shipped the same-logic-in-two-places bug twice —
`plan set` drifted between the CLI and the web form, and the credential path
drifted between users. A disclaimer that tells the user one thing and the model
another is that bug with worse consequences, and it would be invisible from
either side. `SYSTEM_PROMPT` is now composed from `LLM_SCOPE`, and a test pins it.

**2. Acknowledgement is recorded, not assumed** (migration 0015,
`user_disclaimer_ack`).

A footer nobody reads is precisely the failure the auth work already rejected —
"a runtime warning nobody reads is not good enough". The notice is shown once,
blocking, and the acceptance is stored. For any user we can now answer *what
were they shown, and when did they agree*, rather than assuming.

**3. The acknowledgement is versioned.**

Consent is to a specific text. When the notice is revised — a new limitation
found, a new data source added — `DISCLAIMER_VERSION` is bumped and everyone
re-acknowledges. An old row is not consent to new wording, and the history of
what was previously agreed to is kept (append-only, §2.1).

**4. The gate lives in the middleware, and applies to everyone.**

Same reason authentication does: a page added tomorrow is gated by existing
rather than by someone remembering. It applies to the **open-local** case too
(loopback, no account claimed) — the single-user laptop workflow ADR-0018
deliberately preserved. That allowance is about *credentials*; this is about
*advice*, and the owner reading what the numbers are worth once is not a
hardship. The JSON API is gated as well, because gating only the HTML would
leave the same numbers one `/api/` call away.

**5. `/logout` and `/healthz` stay reachable before acknowledgement.**

A gate you cannot retreat from is a trap: someone who reads the notice and
decides not to agree must still be able to leave. And a reverse proxy's health
check is not a person and cannot consent — gating it would take the host down on
deploy for a property that does not apply to it.

**6. The text names concrete failures, not boilerplate.**

"Consult a physician before beginning any exercise program" is worth nothing to
the reader because it cost the writer nothing. The notice instead states the
limitations this codebase has actually measured: self-reported intake running
20–40% below actual (risk #7) and every calorie target inheriting that error;
recovery as a signal to train lighter and never as illness; safety limits that
are fixed constants rather than clinical judgement; and the disordered-eating
case, because a tool that puts a calorie number in front of someone every
morning is not a neutral object. A test asserts these survive future edits.

## Consequences

**Good.** The ADR-0018 prerequisite is discharged; a second human logging in is
no longer blocked on this. Every advice surface carries the notice, and new ones
inherit it. The model and the user are provably held to the same scope.

**The cost.** One extra screen on first run, for the owner too — 46 existing web
tests had to opt past the gate via a named fixture (deliberately not autouse: a
fixture that silently disables a safety gate everywhere is how the gate stops
being tested). Two existing tests were **changed rather than deleted**, and the
change is behavioural, not cosmetic: `test_localhost_with_no_account_claimed_still_works`
now acknowledges first, and `test_invite_link_creates_an_account_and_signs_in`
now asserts a new account gets 403 from the API until it accepts.

**This is not legal advice or a legal shield.** It is an honest description of
what the tool does, what it cannot know, and where it is likely to be wrong. If
this ever goes public, ROADMAP P14's "legal review" item is still real and is
not satisfied by this ADR.

**Not decided here.** Whether the coach should refuse certain question classes
outright rather than declining in prose; whether acknowledgement should expire.
Both are additive.
