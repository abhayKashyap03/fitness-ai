# ADR-0017 — LLM spend is recorded with the rates in effect, and unpriced never means free

## Status
Accepted 2026-07-30. Implements the measurement half of the P10 "model routing +
prompt-cache / COGS audit" item (§8.7). Migration 0013 (`llm_call`).

## Context
The provider API is the project's only meaningful running cost (§8.7), and nothing
measured it. `coach ask` printed a token count to stderr and discarded it; the
grounding eval — one agent loop per scenario, 50 of them, by far the largest single
spend — reported no usage at all. There was no way to answer "what did this month
cost", "which command is expensive", or "is prompt caching actually working" (the
specific lever §8.7 names).

§11 forbids optimizing before a measured problem exists. So the routing question —
should cheap sub-tasks go to a smaller model? — cannot honestly be answered yet,
because nothing knows which command is expensive. Measurement has to come first,
and it has to be a measurement the project can trust as much as it trusts a weight
reading.

Two questions were genuinely non-obvious.

**When should a call be priced?** The tempting design is a price table in code plus
a cost computed on read. Both halves are wrong here.

**What should a call cost when the price is unknown?** The convenient answer, zero,
is a lie that compounds silently.

## Decision

**1. The unit rates in effect at call time are stored ON the row.**

Prices change. If cost were recomputed later from a current price table, a call
made in July would silently change price in September, and every historical total
would be fiction. This is the same provenance rule the rest of the schema already
follows (§2.3): keep what was actually used, resolve nothing destructively. It is
the reason `recovery` stores `score_method` beside `score`, and the reason a July
call keeps July's rates forever.

**2. There is no built-in price table. Rates come from configuration, and absent
rates mean UNPRICED — never free.**

Shipping default per-token prices would fabricate exactly the kind of number this
project refuses to fabricate. A wrong rate does not fail loudly; it flows straight
into a confident "you spent $12.40" that no one can distinguish from a correct one.
A published price is also not ours to assert on the user's behalf: it depends on
their plan, their region, and the day.

So `COACH_PRICE_*_PER_MTOK` supplies rates, and when they are absent the call is
still recorded with its **real token counts** and reported as UNPRICED. This is
§2.7 applied to money: "we don't know what this cost" and "this cost nothing" are
different facts, and only one of them is usually true.

The rule is applied consistently rather than only at the top level:

| Situation | Reported as |
|---|---|
| No rates configured | UNPRICED; tokens still counted |
| Rates for some calls only | priced subtotal **plus** an explicit unpriced count |
| A rate missing for a bucket that has tokens | that call unpriced, not partially priced |
| No calls recorded at all | `Insufficient`, not "$0.00 spent" |
| Malformed rate in config | hard error, so a typo cannot hide behind a plausible report |

**3. Recording is code-authored primary data, and failing to record must not fail
the coach.**

`llm_call` is append-only with no `raw_ref`, and takes no part in the normalize
rebuild or its fingerprint — like `plan` (ADR-0013) and `coach_note` (ADR-0016).
Otherwise asking a question would make `coach normalize --rebuild` look
non-reproducible. A test asserts that.

Accounting is a side effect of doing the work, not the work itself: if the ledger
write fails, the answer the user already received still stands. The failure is
printed to stderr rather than swallowed, so a silently broken ledger cannot
masquerade as a zero-cost month.

## Consequences

**Good.** Spend is measurable per command, so the routing decision can be made
from data instead of intuition. Cache-hit rate is visible, which is the §8.7 lever
worth watching — the system prompt is stable and should be cache-hitting, so a
collapsing rate means caching regressed. Historical costs are stable under price
changes. The eval's cost is finally attributed to the eval.

**The cost.** The user must supply rates before seeing currency, and the first
`coach cost` run therefore reads UNPRICED. That is deliberate friction in exchange
for never showing an invented figure. Tokens are useful on their own in the
meantime.

**Cross-provider caveat.** `cache_hit_pct` assumes `cached_input_tokens` sits
*alongside* fresh input rather than inside it. All three shipped adapters normalize
to that — Anthropic reports `cache_read_input_tokens` separately, and the Grok and
Google adapters subtract cached from their all-inclusive prompt count. A future
adapter that bundles them would read as 0%: absence showing as absence, not a claim
that caching failed. A fourth provider should be checked against this convention.

**Not decided here.** Model routing itself, and any per-command spend cap. Both
wait on what the ledger actually shows. A cap in particular is a policy decision
about the user's money, not an implementation detail.
