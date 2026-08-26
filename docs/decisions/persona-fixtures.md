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
| The Lapsed Customer | is stored value surfaced unasked (P3) | `days_since_order ≥ 90` **and** `points_balance ≥ 1250` |
| The Explorer | are answers hedged appropriately | `usual_share ≤ 0.15`, `distinct_baskets ≥ 20`, `distinct_stores ≥ 5` |

Every bound, not a weighted score. A score would let a Regular with no dominant usual
place well by ordering a lot, which is exactly the fixture that cannot demonstrate its
own metric.

**The Lapsed Customer has the least margin, and it is worth knowing.** Six of the
sixty lapsed customers in the shipped population clear the points bar; four are needed.
The rest have spent their balance down — `[loyalty].redemption_probability` is 0.65, so
most customers redeem on their way out rather than forgetting. That margin of two is
the tightest constraint anywhere in this ticket, and it moves when #27 retunes the
rewards arithmetic. It is decoupled deliberately (see below) so the movement cannot be
silent: `test_fixtures.py` fails rather than the roster quietly thinning.

**An archetype that cannot fill its roster contributes fewer fixtures.** It does not
get topped up with the best of the customers who failed. The failure this avoids is
specific and bad: a demo assigning a visitor "the Regular" who turns out to have no
usual, because a quota had to come out to four. A short roster is reported by the CLI
and asserted against the shipped population by `test_fixtures.py`, so it is loud rather
than silent — but it is honest, and a sixty-customer population genuinely demonstrates
less than a five-hundred-customer one.

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

**Its points balances are not reconciled.** `points_balance` is what this generator's
provisional `[loyalty]` arithmetic sums to. It is stored value the assistant can
surface, which is what P3 needs of it; it is not a Chipotle Rewards balance, and issue
#27 owns making it one. Two consequences worth naming:

- `personas[lapsed].fixture.at_least.points_balance` is deliberately the *same number*
  as the current `redemption_threshold` and deliberately **not a reference to it**. It
  is a selection bar — "enough stored value to be worth interrupting someone about" —
  and when #27 moves the threshold, this is retuned beside it rather than dragged along
  by it. The two answer different questions.
- The Office Manager's narrative says nothing about points, because that archetype
  outruns the redemption rule: `[loyalty]` allows at most one redemption per order, so
  a customer earning more than `redemption_threshold` per visit accumulates a balance
  no real programme would show (38,359 points, on the shipped seed). Filed as `cc-5si`
  against #27. What that fixture is here to demonstrate is group ordering, so that is
  what its sentence is about.

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
