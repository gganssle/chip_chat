# Decision: `persona_fixtures`, a sixth table RFC-001 §04 does not list

**Issue:** [#26](https://github.com/gganssle/chip_chat/issues/26) (bead `cc-5jk`) · **Decided:** 26 August 2026
**Builds on:** [#25](https://github.com/gganssle/chip_chat/issues/25) (bead `cc-6fj`) and `docs/decisions/synthetic-population.md`
**Unblocks:** [#67](https://github.com/gganssle/chip_chat/issues/67) (entry flow), [#28](https://github.com/gganssle/chip_chat/issues/28) (proving the population is not thin)

---

Issue #26 asks for the three PRD §02 personas as **fixtures** — "not just design
documents", but "the fixtures a visitor is actually assigned on entry". Issue #25 had
already delivered a `personas` table exactly as RFC-001 §04 specifies it. The two are
not the same thing, and this records why a sixth table was the answer rather than
either extending the fifth or writing no table at all.

## `personas` answers a different question

`docs/decisions/synthetic-population.md` settled that `personas` is a table of
**archetypes**: seven rows, five hundred customers, a many-to-one through
`demo_visitors.persona_id`. That is what §04's schema describes and it is right.

But #26 says the `narrative` column "is what the opening message is built from" and
gives the sentence it wants:

> *a regular at the Ballard store, 1,250 points, and a well-documented weakness for
> double barbacoa*

Every fact in that sentence is about **one customer**. A row shared by eighty
customers cannot carry it — there is no single store, no single balance and no single
usual to name. Widening `personas` to hold it would mean one row per customer, which
is `demo_visitors`, and then `personas` no longer means what §04 says it means.

So the two narratives are two different sentences with two different jobs:

| Column | Rows | Sentence |
| --- | --- | --- |
| `personas.narrative` | 7 | *"Same bowl, same store, the same day nearly every week."* — what this kind of customer is like. From the config. |
| `persona_fixtures.narrative` | 28 | *"a regular at NC Town 1 Mall, 1,288 points on the card, and 99% of 79 orders the same Chicken Bowl with guacamole, white rice, black beans and cheese."* — what **this** customer is like. Measured. |

## Why it is a table and not a query

The alternative was to ship the selection as a query for #67 to run. Two reasons not
to.

**It cannot be recomputed cheaply downstream.** Choosing the Regular means ranking
every customer by how dominant their commonest basket is, which is a group-by over
forty-nine thousand order lines. Doing it at entry, per visitor, to answer "who am I"
is the wrong shape.

**A fixture is a curated thing, and curation should be reviewable.** Twenty-eight rows
that a person can read is how "the narrative text is good enough to paste directly
into an opening message" gets checked at all. A query has no artefact to review.

## The selection rule, and why it refuses to pad

The ticket sets the standard: *"if a fixture cannot demonstrate its own metric, it is
not finished."* So each archetype carries bounds on its own defining behaviour, in
`population.toml` beside it, and a customer becomes a fixture **only** by clearing
every one of them.

| Persona | PRD §02 measurement | The bound |
| --- | --- | --- |
| The Regular | turns-to-reorder, target 1 | `usual_share ≥ 0.85`, `store_share ≥ 0.90`, `order_count ≥ 30` |
| The Lapsed Customer | is stored value surfaced unasked (P3) | `days_since_order ≥ 90` **and** `points_balance ≥ costliest_reward` |
| The Explorer | are answers hedged appropriately | `usual_share ≤ 0.15`, `distinct_baskets ≥ 20`, `distinct_stores ≥ 5` |

Every bound, not a weighted score. A score would let a Regular with no dominant usual
place well by ordering a lot, which is exactly the fixture that cannot demonstrate its
own metric.

**The Lapsed Customer's margin was the tightest thing in this ticket and is now the
loosest, and both facts are about issue #27 rather than about this rule.** Against
issue #25's provisional arithmetic, six of sixty lapsed customers cleared the bar and
four were needed — a margin of two, and the reason the original text warned about it.
Under the published terms the earn rate is ten points a dollar, this archetype redeems
at 0.04, and fifty-nine of sixty now clear the costliest published reward. The bar did
not move down; the population moved up. `test_fixtures.py` fails rather than the roster
quietly thinning, which is what makes either direction visible.

**An archetype that cannot fill its roster contributes fewer fixtures.** It does not
get topped up with the best of the customers who failed. The failure this avoids is
specific and bad: a demo assigning a visitor "the Regular" who turns out to have no
usual, because a quota had to come out to four. A short roster is reported by the CLI
and asserted against the shipped population by `test_fixtures.py`, so it is loud rather
than silent.

The test that guards this checks the *criteria*, not a count. A count is the obvious
assertion and the wrong one: whether a sixty-customer population happens to yield fewer
exemplars than a five-hundred-customer one depends on where the bars sit, and it
stopped being true when issue #27 changed the earn rate. What must never stop being
true is that everybody on the roster earned their place — and, so that the bounds are
shown to bite at all, that the thin population really did contain customers they turned
away.

## Three things `persona_fixtures` deliberately does not claim

**It is not the `usual_order` gold mart.** `usual_share` is a blunt count — the share
of a customer's orders that are their commonest basket. The mart of RFC-001 §04
computes a `confidence` its own way, nightly, in Databricks. The column is not called
`confidence` for that reason. What makes the two agree about a fixture is not a shared
definition but the *selection*: fixtures are taken from the extremes, a Regular above
85% and an Explorer below 15%, so any reasonable definition of confidence puts them on
the same sides of the line. If they ever disagree about a fixture, the mart is right.

**Its personas vary by behaviour, not by food.** The committed fixture catalogue has
two entrees in it, so a fixture's orders can differ in structure and quantity and not
in ingredient. Every food named in a narrative is a real catalogue row and a test
asserts it — but "the population offers genuine variety of food" is issue #28's claim
to make, against a real harvest, and is not made here.

**Its points balances are Chipotle's arithmetic, not its rewards policy.**
`points_balance` is what the ledger sums to, and since issue #27 every number that
ledger uses — the earn rate, the expiry window, the daily cap, every reward price —
is read off Chipotle's published terms. What is *not* claimed here is that any
particular customer's balance is one Chipotle would recognise: how eagerly an
archetype redeems is a tuning parameter in `population.toml`, and it is the one thing
about the ledger this project still chooses.

## The bar for stored value is read, not chosen

This is the part of the ticket that had to be redone, and the reason is worth writing
down because the mistake was a reasonable one.

The Lapsed Customer's criterion is "enough stored value to be worth interrupting
someone about". Issue #26 first shipped it as `points_balance = 1250` — the same
number as `[loyalty].redemption_threshold`, and *deliberately not a reference to it*.
The argument was that the two answer different questions: the threshold is arithmetic
and the bar is a selection decision, so #27 retuning one should not silently move the
other. Written as a reference it would have moved without anyone deciding it should.

Issue #27 did not retune `redemption_threshold`. It **deleted** it. The earn rate and
every reward price now come from the published terms, and `redemption_threshold`
survives only in `PUBLISHED_KEYS`, as a key a config is refused for still carrying. So
the copy outlived the thing it was a copy of, and the criterion went on comparing
against a number nothing published any more — which is exactly the decoupling working
as designed, and exactly the wrong outcome.

The repair is not to point the bar back at the config. It is to notice that this
particular bar was never ours to choose:

```toml
[personas.fixture.at_least]
days_since_order = 90
points_balance = "costliest_reward"
```

A bound's value is now a number chosen here **or** a name from
`config.PUBLISHED_BOUNDS`, resolved against `RewardsTerms` at selection time by
`fixtures.PUBLISHED_READERS`. Two names exist, both about the Rewards Exchange.
`cheapest_reward` is the least a balance can be and still buy anything — the
redemption threshold, and the ledger already uses it as one. `costliest_reward` is the
price of the most expensive thing published, and issue #27 had already named it: the
docstring on `RewardsTerms.costliest` says it is "the number a balance worth surfacing
unprompted is measured against", and `test_ledger_population.py` asserts that ninety
per cent of lapsed customers clear it. Issue #26 is joining that decision rather than
making a second one beside it.

Which is the distinction the two vocabularies now draw. `MEASURES` says what may be
**bounded**; `PUBLISHED_BOUNDS` says what a bound may be **read from**. "At least
twenty orders" is a product decision and belongs in the config. "Enough points to
matter" is a fact about someone else's programme.

The test that pins it reprices the published Rewards Exchange out of every lapsed
customer's reach and requires the roster to empty, while every other archetype's
roster — all of them bounded on facts about orders — comes out identical. A bar stored
anywhere in this package leaves that first assertion standing.

## The Office Manager could not spend what they earned

The same pass fixed `cc-5si`, filed against this work when the original was written.

`ledger_for` redeemed at most once per visit. Nothing Chipotle publishes says that; it
was invented here. For most archetypes it never bound, but the Office Manager puts in a
hundred-and-forty-dollar group order and earns about fourteen hundred points for it,
against a costliest published reward of 1,625. They could not keep up with their own
earning however much they wanted to, redeemed 46% of what they earned, and finished
eighteen months carrying sixty-five thousand points. That is not a number any rewards
programme would show anyone.

Two things were wrong and they needed different fixes.

The **cap** is gone: the redemption branch is a loop, and
`redemption_probability` is asked again after each redemption. That is issue #27's own
rule applied to the one rule in the module that was not published — and on its own it
fixed the Weekend Family, whose balance was drifting up more slowly for the same
reason.

The **eagerness** was retuned, which the cap's removal did not fix, because 0.18 was
never the cap biting. It was tuned against issue #25's provisional arithmetic; under
ten published points per dollar it describes someone who earns a free entrée every
lunch run and shrugs. It is now 0.60, and the archetype redeems 96% of what it earns.
The number is behaviour, not arithmetic, and behaviour is what `population.toml` is
still allowed to hold.

The Lapsed Customer's 0.04 was **not** touched. Their balance accumulates because they
never spend it, which is the persona, and `test_ledger_population.py` requires ninety
per cent of them to end above the costliest published reward. The distinction the
regression test draws is therefore not about the size of a balance but about whether
the customer *could* have spent it: a customer who always wants to redeem must end
every visit unable to afford anything at all, whatever they earn.

## No name in the narrative

RFC-001 §04 makes `display_name` one of three columns a visitor may edit. A narrative
with a name baked into it is a sentence that is wrong the moment they change it, so
the sentence is nameless — as the ticket's own example is — and #67 joins the live
name to it at entry.

The same containment goes further and is worth stating as a property: **fixture
selection reads no editable column.** It reads `orders`, `order_items`,
`loyalty_ledger` and `demo_visitors.persona_id`, which a visitor cannot change. It
never reads `display_name`, `home_store_override` or `stated_preferences`. That is the
same rule §04 imposes on the gold marts, and `test_fixtures.py` asserts it by rewriting
all three columns for all five hundred customers and checking the fixtures come out
identical.

## Where `home_store` comes from

`persona_fixtures.home_store` is derived from `orders.store_id` — it is
`customer_360.favourite_store`'s definition, not `personas.home_store`, which
`synthetic-population.md` established is the store an *archetype's* narrative is set
at. The two may legitimately differ for the same customer, and §04 already says the
serving layer surfaces the disagreement rather than reconciling it silently. The
narrative names the store the customer actually uses, because that is the one they
would recognise.
