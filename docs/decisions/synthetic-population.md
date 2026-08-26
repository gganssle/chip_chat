# Decision: what the synthetic population carries beyond RFC-001 §04

**Issue:** [#25](https://github.com/gganssle/chip_chat/issues/25) (bead `cc-6fj`) · **Decided:** 26 August 2026
**Amends:** RFC-001 §04, whose "demo accounts" tables this adds five columns to
**Unblocks:** [#26](https://github.com/gganssle/chip_chat/issues/26) (persona fixtures), [#27](https://github.com/gganssle/chip_chat/issues/27) (loyalty reconciliation), [#28](https://github.com/gganssle/chip_chat/issues/28) (proving the population is not thin), [#33](https://github.com/gganssle/chip_chat/issues/33) (bronze ingestion)

---

Issue #25 says the schema is fixed in RFC-001 §04 and to match it rather than invent
a parallel shape. This document is the list of places where matching it exactly would
have produced a table nobody could check, and what was added instead. Everything not
listed here is §04 verbatim.

## What `personas` is a table of

§04 gives `personas` the columns `persona_id, label, home_store, seed_points,
narrative` and gives `demo_visitors` a `persona_id`. That is a many-to-one, so
`personas` is a table of **archetypes** — seven of them — and the five hundred
synthetic customers are rows of `demo_visitors`. Issue #25's own wording settles it:
"500 synthetic customers with `demo_id`, display name and a persona assignment" is
`demo_visitors`, column for column.

`personas.home_store` is therefore the store an archetype's *narrative* is set at,
drawn from the roster by seed. A customer's own store is `customer_360.favourite_store`,
derived from their orders. The two may disagree and §04 already says what happens when
they do: the serving layer says so rather than reconciling silently.

## The five added columns

Each of these exists because the table cannot be checked without it.

| Column | Why |
| --- | --- |
| `order_items.line_number` | §04 keys a line `(order_id, item_id)`, which cannot hold two burritos built differently. A group order that cannot hold two different burritos is not a group order. |
| `order_items.unit_price` | Without it `orders.total` is a number nobody can audit, and "prices computed from the catalogue, not invented" is a claim rather than a test. |
| `order_items.line_total` | The same, per line: a reviewer re-derives the total from `item_prices` and finds it. `test_referential_integrity.py` does exactly that. |
| `orders.channel` | Chipotle publishes two prices per item, counter and delivery. A total is unexplainable until the row says which was used. |
| `orders.priced_restaurant_id` | See below. |
| `loyalty_ledger.order_id` | Issue #27 reconciles the ledger against published rewards terms. Reconciling it against the orders that earned it should be a join, not a regeneration. |

## Thirty stores, one priced restaurant

`docs/decisions/menu-pricing.md` established that Chipotle prices per restaurant and
that the harvest prices one reference restaurant by default — an eighteen per cent
spread makes a single `base_price` a false statement rather than a simplification. The
population, meanwhile, orders from thirty stores, because a demo with one store has
nothing to say about a favourite one.

Three options, and only one of them is honest:

1. **Only generate orders at priced restaurants.** One store. Loses
   `customer_360.favourite_store`, `home_store_override`, and the store-locator lane.
2. **Interpolate prices for the other twenty-nine.** Invents money. The whole point of
   the real-menu / fake-accounts boundary is that money is published, not modelled.
3. **Price every order at a published menu, and say whose.** Chosen.

`orders.priced_restaurant_id` is the store's own restaurant when the catalogue priced
it and the catalogue's reference restaurant when it did not. So a quoted historical
total always has a restaurant and a `harvested_at` behind it, which is what §08's
citation requirement needs. When a later harvest prices all thirty, the column starts
saying each store's own id and nothing else changes.

Availability is read from the reference restaurant for the same reason: a store priced
by fallback sells what the reference restaurant sells, because saying otherwise would
be inventing a stock level.

## Opening hours have a timezone and the locator does not publish one

"Weekday lunch peaks" is a claim about local time, and so is "inside the store's
opening hours". The locator publishes hours as bare `HH:MM` with no zone, and the
harvested stores span most of the United States.

`timing.store_timezones` in `population.toml` maps the published `stores.region` — a
state abbreviation — to an IANA zone, with a default. States split across two zones
are mapped to the one most of their restaurants are in. That is an approximation, and
it is a visible table rather than a constant in a function so that it reads as one.
When the locator harvest carries a store's own zone, the table goes away.

## The loyalty arithmetic is provisional

`points_per_dollar` and `redemption_threshold` are parameters in `[loyalty]`, not
facts. Issue #27 reconciles them against the rewards terms the policy harvest already
carries. Until it does, nothing in this package asserts them as Chipotle's programme,
and `loyalty_ledger.order_id` is there so that the reconciliation can be a join.

## What is not in this package

**A Python loader.** The catalogue has one because three subsystems resolve against it
in process. These tables are consumed by Auto Loader (issue #33) and, in tests, by
calling the generator again — which is free, because the same seed produces the same
population. A loader would be a second definition of the schema to keep true.

**A real ADLS write.** `SyntheticPopulation.write` takes a `BlobStore`, which is the
same interface the harvest and the catalogue land through. An ADLS Gen2 implementation
of it is `cc-b15`, and it lands all three streams at once rather than one of them
specially.
