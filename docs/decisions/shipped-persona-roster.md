# Decision: ship the persona fixtures in the image, and read them only when Snowflake is absent

**Issue:** [#66](https://github.com/gganssle/chip_chat/issues/66), [#67](https://github.com/gganssle/chip_chat/issues/67) · **Decided:** 27 August 2026
**Builds on:** `docs/decisions/persona-fixtures.md` (#26)
**Retired by:** `cc-lpy4` — the Snowflake connection factory

---

## The gap this closes

`chip_chat.api.visitors` was written correctly and, on every deployment that has
ever existed, assigned nobody.

The chain is short and every link is defensible on its own. `SnowflakeRoster`
reads `ACCOUNTS.persona_fixtures` through `VisitorPool.unbound()`. `VisitorPool`
needs a `Callable[[], SessionConnection]`. `build_service` takes that as an
argument and **nothing supplies one**, because there is no Snowflake driver in
this lockfile — `chip_chat.snowflake.snow` shells out to the `snow` CLI, which
is a developer tool and not something a container calls per request. So
`build_visitors(None)` returned `VisitorDesk(StaticRoster())`, an empty roster is
a roster that assigns nobody, and `VisitorDesk.admit` returned `None` for every
visitor on the live URL.

`None` is a *decided* state and the code says so at length. What it means for a
visitor is the exact failure PRD §06 names as the largest threat to the demo:

> a visitor types their name, arrives at an empty account, asks the only
> question that occurs to them, and is told they have zero points and no order
> history. There is nothing wrong with any component in that exchange and the
> visitor leaves anyway.

That was the deployed behaviour. Not a risk — the behaviour.

## The decision

**Export the twenty-eight rows of `ACCOUNTS.persona_fixtures` into the image at
`api/src/chip_chat/api/fixtures/persona_fixtures.json`, and read them only when
`build_service` is called with no connection factory.**

```
$ snow sql -q "select demo_id, persona_id, label, rank, home_store,
      home_store_name, points_balance, usual_item_id, order_count,
      lifetime_spend, narrative
    from chip_chat.accounts.persona_fixtures order by persona_id, rank"
```

Seven archetypes, four exemplars each, every one of them carrying the measured
narrative #26 curated.

That query is how the file was first produced and is no longer how it is
maintained; the amendment at the foot of this document replaces it with an
exporter that reads the committed roster instead of the live account, and says
why the live account turned out to be the wrong thing to read.

## Why this is not "inventing accounts"

`visitors.py` refuses to hand a visitor an unpopulated fixture and says, in its
own docstring, that *"an invented account is the empty account issue #66 is
written to prevent"*. That rule is intact and this does not bend it.

These are not invented. They are the rows `data-gen` generated, the fixture
selection #26 curated against per-archetype bounds, and a human reviewed as
twenty-eight sentences. The export carries the **same `demo_id`s** Snowflake
holds, so a session bound from this file is bound to the same synthetic customer
a Snowflake-backed deployment would have bound it to. When the connection
factory lands, the identities do not move.

The thing that would be inventing accounts is a roster generated at start-up
from a random seed, or a single hardcoded account served to everybody. Neither
is what this is.

## The rule that keeps it honest

**The shipped roster is consulted only on the `connect is None` path.**

```python
store = VisitorSessionStore(journal_from_env())
if connect is None:
    return VisitorDesk(shipped_roster(), store=store), None
pool = VisitorPool(connect, sessions=store, size=pool_size)
return VisitorDesk(SnowflakeRoster(pool), store=store), pool
```

There is no merge, no fallback-on-error, and no precedence question. A
deployment either has a connection and reads the live table, or has none and
reads the file. The moment `cc-lpy4` supplies a factory, `shipped_roster()`
stops being called and the file becomes dead weight to be deleted — which is the
right shape for a stopgap, and the reason it is a whole separate function with
its own name rather than three lines inside `StaticRoster`.

`shipped_roster()` logs a `WARNING` naming the file every time it is used. An
operator reading the container's first ten lines can see which roster is in play
without asking.

## What it costs

**The row access policies are not exercised.** A file-backed roster is read by
Python rather than by a session that #43's `entry_roster` policy narrowed. That
is not a regression — the pool is `None` on this path, so *nothing* was being
enforced by Snowflake before either — but it means "identity is enforced by row
access policies" remains a claim about the Snowflake tier that this deployment
does not yet demonstrate. `api/tests/test_identity_binding.py` still holds the
app tier to the absence, and `test_two_sessions_see_only_their_own_rows` still
exercises the pool against a fake account.

**The file goes stale if the population is regenerated.** #47's nightly load can
change `persona_fixtures` underneath it. The cost of stale is a narrative that
quotes a points balance the account no longer has — which is exactly the
disagreement between the opening message and the account tool that
`persona.py` is written to avoid. Two mitigations: the export is small enough to
re-run in one command, and it is only read at all on deployments where the
account lane is *also* unwired, so there is no account tool to disagree with.

## The alternative that was rejected

Leave the roster empty and let #67's opening message say "this deployment has no
synthetic accounts loaded".

That sentence exists and is what an unpopulated deployment gets
(`unbound_opening_message`). It is honest and it is a bad demo. The URL is
handed to strangers; a stranger who is told there is no account has been given a
menu search box with a disclaimer on it, and every one of #67's acceptance
criteria fails by design rather than by accident. Shipping the real rows costs
one JSON file and a deletion later.

---

## Amendment, 28 August 2026: it went stale, and the export is now a program

**Issue:** bead `chip-4da`, following `chip-qvg` · **Amends:** "What it costs",
second paragraph, and the `snow sql` recipe under "The decision"

The cost this document named as a risk arrived within a day of it being written,
by the route it described and for the reason it gave.

`chip-qvg` found `CHIP_CHAT.ACCOUNTS` holding two halves of two different
generations: `orders` and `loyalty_ledger` for five hundred synthetic customers,
and `persona_fixtures` for the sixty-customer generation that preceded them. It
reloaded the account from the correct generation and — the durable half of that
fix — committed the roster it loaded at `data-gen/roster/`, so that "the rows the
account is supposed to be holding" became a thing this repository has a copy of
rather than a thing one Snowflake account asserts. `data-gen/tests/test_roster.py`
holds that copy to what the shipped `population.toml` generates.

It did not touch this file, because `api/src` was owned by another agent in that
wave. So the shipped export kept the sixty-customer generation's twenty-eight
rows, `demo-0004` through `demo-0058`, and the claim two sections above — *"a
session bound from this file is bound to the same synthetic customer a
Snowflake-backed deployment would have bound it to"* — stopped being true. It
stopped being true in the most awkward available way: two `demo_id`s, `demo-0021`
and `demo-0024`, appear in both generations naming **different customers**, with
different home stores, different order counts and different balances. A visitor
bound to one of those on the `connect is None` path would have been handed a real
identifier attached to somebody else's history, which is not a missing row and
does not look like an error anywhere downstream. The other twenty-six named
customers the account no longer contains at all.

### The part the bug report did not say

Comparing the two exports turned up something worse than a wrong number, and it
is worth writing down because it is the fourth invariant and not a data-quality
nit.

The sixty-customer generation was composed against the **committed fixture
catalogue** — the two-entree menu and the thirty invented stores that
`catalog/tests/fixtures/` carries so that a laptop with no network can run the
suite. Its twenty-eight fixtures therefore named home stores called `MA Town 1
Mall`, `KS Town 1 Mall`, `Lakewood Mall` and eighteen more of the same shape, and
between them they had exactly two distinct usual items, `CMG-2` and `CMG-101`. Not
one of those stores is a restaurant Chipotle publishes, and sixteen of the
twenty-eight narratives named one in prose: *"a regular at KY Town 1 Mall until
March 2026, and not seen since — 14,495 points still unredeemed from 43 orders."*

That is the boundary the fourth invariant is about. Everything the assistant says
about food and about restaurants comes from what Chipotle publishes; everything it
says about "you" comes from a generated customer. A synthetic customer with an
invented order history is the invariant working. A synthetic customer whose home
store is an invented restaurant with an invented name, said out loud in the
opening message, is the invariant blurred — the visitor cannot tell which half of
that sentence is real, and the honest answer is that half of it is not. #106
replaced the account's catalogue with the real harvest for exactly this reason and
`data-gen/roster/inputs/` exists so the roster is regenerated from it; the shipped
export was the one copy that had not moved. It now names published restaurants —
`Addison - Lake 53`, `1001 Penn Ave NW`, `Annapolis Mall` — because it is a
projection of a generation composed against the real harvest, and twenty distinct
usual items rather than two, because that harvest publishes 192 orderable things
rather than ten.

Nothing was being served from it — the deployed app has a connection factory and
`/healthz/lanes` reports the account lane up, so `shipped_roster()` is not called
— but "a stale file that nothing reads" is a description of the moment before it
is read, not a property of the file, and on this path the file was the *only*
thing a local run without credentials would have had to talk about.

### What changed

**The export is now taken from `data-gen/roster/persona_fixtures.jsonl` rather
than from the live account, and it is taken by a committed program.**

`data-gen/tests/export_shipped_roster.py` reads the committed roster, projects
each row onto `chip_chat.api.visitors.ROSTER_COLUMNS` — imported from `visitors.py`
rather than retyped, so a column renamed there moves the exporter with it instead
of quietly dropping a key the reader would then read as `None` — sorts by
`persona_id, rank` the way `_ROSTER_QUERY` does, and writes the JSON. It computes
nothing. Every value in the output is the value on the JSONL line it came from,
which is the entire point: the two files have to be one generation, so the export
has to be a projection and not a derivation.

```bash
uv run python data-gen/tests/export_shipped_roster.py
```

Reading the committed roster rather than the account is the substantive part of
the change. The account is a mutable thing a nightly job writes to and a human can
reload; the committed roster is a file under version control that a test already
holds to the generator. Exporting from the account made this file a copy of a copy
whose ancestor could move without leaving a diff — which is exactly how it went
stale — while exporting from `data-gen/roster/` puts every copy of
`persona_fixtures` in this repository on one chain that `make ci` walks end to
end: `population.toml` and `roster/inputs/` generate the roster
(`data-gen/tests/test_roster.py`), and the roster generates the export
(`api/tests/test_shipped_roster.py`).

**Ordering is part of the projection, not formatting.** The roster is a sequence
the entry flow assigns from, so a file sorted the generator's way rather than the
query's would hand the fourth visitor a different customer than Snowflake would —
a difference that produces two working demos which disagree about who somebody is,
and no failure anywhere.

### The mitigation that was wrong

The paragraph this amends offered two mitigations, and the first one was the
mistake:

> Two mitigations: the export is small enough to re-run in one command, and it is
> only read at all on deployments where the account lane is *also* unwired, so
> there is no account tool to disagree with.

"Small enough to re-run in one command" is not a mitigation. It is a description
of how easy the fix is once somebody has noticed, and noticing was the whole
problem — a stale export is not visibly stale, because every row in it is
well-formed, internally consistent and carries a plausible sentence. What was
missing was not ease. It was anything that compared the file to something.

The second mitigation was sound when written and has since expired on its own
terms: `cc-lpy4` landed the connection factory, so the account lane is now wired
on the deployment where this file lives, and "there is no account tool to disagree
with" is no longer true of any environment except a local run with no credentials.
That is the environment the file now exists for, and it is one where a visitor can
still be shown an opening message composed from a narrative.

So the mitigation is now a test rather than a sentence about effort.
`api/tests/test_shipped_roster.py` regenerates the projection on every `make ci`
and compares it byte for byte, checks that the `demo_id` sets match, that every
row carries exactly `ROSTER_COLUMNS` in order, that `shipped_roster()` hands out
all twenty-eight rather than silently dropping fixtures that failed `populated`,
and that every narrative quoting a points balance quotes the one beside it — the
last of these being the same assertion `data-gen/tests/test_roster.py` makes on
the roster, deliberately repeated here on the copy an opening message is actually
composed from.

### What is still true, and what still costs

The row access policies remain unexercised on this path, for the reason the
original section gives, and that is unchanged.

The deletion this document anticipated has not happened. `cc-lpy4` landed, so by
the letter of "The rule that keeps it honest" this file became dead weight to be
deleted — but `build_service(connect=None)` is still the path a local run without
Snowflake credentials takes, and deleting the export would return that run to the
empty roster #66 is about. Keeping it is therefore a decision rather than an
oversight, and the price of keeping it is the test above: a third copy of a table
is only defensible while something checks that it is the same table.

**What was not measured.** Nobody has confirmed against a running deployment that
`shipped_roster()` is uncalled in production; the evidence is that the connection
factory is supplied and the lane reports up, which is an inference from
configuration rather than an observation of the log line `shipped_roster()` emits
every time it is used. If that inference is wrong, the population being served was
wrong too, for as long as the deployment has been up.
