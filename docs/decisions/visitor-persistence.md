# Decision: a visitor's state persists between visits, by cookie

**Issue:** [#9](https://github.com/gganssle/chip_chat/issues/9) (PRD Q1) · **Decided:** 25 August 2026, by Graham
**Changes:** [#47](https://github.com/gganssle/chip_chat/issues/47) (the nightly reset's whole shape), RFC-001 §04 (`demo_visitors.last_seen` becomes load-bearing), RFC-001 Q4 (answered by consequence)
**Does not change:** RFC-001 §05. Identity still originates server-side and is still bound to the Snowflake session

---

This record is written late. The decision was made on 25 August 2026 and the
four documents that depend on it have cited it ever since —
`docs/demo-reset.md`, `docs/phase-0-verification.md`,
`decisions/persona-editing.md` and `decisions/foundry-agent-shape.md` all say
"because #9" — but the record itself was never filed, so the reasoning lived only
in a closed issue's comment thread. That is the gap [#92] exists to close, and
this is the highest-value one of the five, because more documents lean on this
decision than on any of the others.

## The decision

**A returning visitor comes back to the account they left.** A cookie maps to a
server-side `demo_visitors` row; the row is durable.

The PRD framed the alternative honestly — a fresh persona every visit is
"simpler and slightly colder" — and the colder version is genuinely easier. It
was declined because the demo's whole proposition is *personalization*, and a
system that forgets you between visits cannot demonstrate the thing it is for.
"What's my usual?" is a different sentence on a second visit than on a first.

## The four consequences, and the one that reshaped another issue

**1. The app tier holds a durable session store.** Persistence changes the
*lifetime* of the identity binding, not its trust model. The cookie is a pointer
to a server-side row; it is not an identity the client asserts, and nothing about
it reaches the model. `api/src/chip_chat/api/visitors.py` is where it lives.

**2. The nightly reset can no longer truncate. It has to age sessions out.** This
is the consequence that mattered most and it landed on somebody else's issue.
Truncating visitor-scoped tables would empty the account of a returning visitor
mid-story, so [#47] had to become *expire on last-seen, then restore that
visitor's rows to generated state* — which is exactly the resolution RFC-001 Q4
anticipated. **RFC Q4 is therefore answered by consequence and needs no separate
record.** `docs/demo-reset.md` is the whole shape that fell out of this sentence,
including the `demo_visitor_baseline` table that exists only because "restores
generated state exactly" has to be checkable rather than assertable.

**3. Demo data accumulates**, so the reset job has a real job rather than being a
convenience. Growth is bounded by the session TTL, which becomes a tuned
parameter rather than an afterthought — two days, derived rather than invented,
in `docs/demo-reset.md` §3.

**4. `last_seen` becomes load-bearing rather than decorative.** It was already in
the RFC §04 schema; it is now the input to the ageing policy. `docs/demo-reset.md`
§3 records the complication that followed: only one of the four write procedures
writes it, so the reset reads a visitor's activity off four clocks rather than
one, and a visitor with no clock at all is *held* rather than aged.

## What it costs

A returning visitor is a visitor whose account has state somebody else's turn
put there. Everything in `docs/snowflake-isolation.md` — two row access policies
keyed to a session variable, default deny written out rather than inherited —
would be simpler if every visit started empty. It does not start empty, so the
isolation has to be real rather than incidental, and it is checked by
`make snowflake-verify`'s #41.3 group on every run.

## Revisit trigger

If the demo is ever shown at a volume where accumulation outruns the TTL, or if a
visitor's persisted state turns out to be the thing that makes a bad demo
unrecoverable in front of an audience. Neither has happened; the manual reset in
`docs/runbook.md` §6 exists for the second.

[#47]: https://github.com/gganssle/chip_chat/issues/47
[#92]: https://github.com/gganssle/chip_chat/issues/92
