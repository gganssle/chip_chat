# Decision: prices are stored per restaurant, and one is the reference

**Issue:** [#19](https://github.com/gganssle/chip_chat/issues/19) (bead `cc-6xy`) · **Decided:** 26 August 2026
**Amends:** RFC-001 §04, whose `menu_items.base_price` is a single column
**Unblocks:** [#24](https://github.com/gganssle/chip_chat/issues/24) (`menu_catalog`), [#23](https://github.com/gganssle/chip_chat/issues/23) (action surface)

---

Issue #19 asks for this choice to be made deliberately and written down. The
confirmation card shows a total, and a demo that quotes a price should be able to say
what that price is grounded in.

## The fact that forces a decision

Chipotle prices per restaurant. This is not a rounding difference. Four restaurants,
asked within a few minutes of each other on 25 August 2026:

| Restaurant | Steak Burrito | Chicken Bowl |
| --- | --- | --- |
| 1 | $11.15 | $9.15 |
| 679 | $13.15 | $11.15 |
| 1200 | $11.65 | $9.65 |
| 2500 | $11.55 | $9.80 |

An eighteen percent spread on the same item. A catalogue with a single `base_price`
column would have to pick one of those numbers and then present it as *the* price,
which is not a simplification but a false statement.

## The decision

**Model per-store pricing. Populate one reference restaurant by default.**

Every price row carries a `restaurant_id` — `item_prices` and `meal_prices` both.
Identity and structure live in `menu_items`, `modifiers`, `modifier_groups` and
`portion_options`, which are the same at every store and are stored once.

The default harvest asks one restaurant, `REFERENCE_RESTAURANT_ID` — restaurant
**0679**. Harvesting more is an argument, not a schema change:

```bash
python -m chip_chat.harvest.sources.chipotle --landing landing \
    --restaurant 0679 --restaurant 1200
```

The first identifier given is the reference: it defines the catalogue's structure,
and it is the restaurant whose prices a demo quotes.

## Why not the alternatives

**One flat price set, restaurant forgotten.** Cheapest, and the failure is silent.
A visitor is shown $13.15, asks why the restaurant down the road charges $11.65, and
the system has thrown away the only field that could have answered. RFC-001 §08
requires a response to cite its source; a price whose store has been dropped cannot.

**Every store, all the time.** There are several thousand of them. Each restaurant
costs two requests and about 800 KB, so a full sweep is a couple of gigabytes pulled
from a third party over hours of continuous crawling at the two-second rate issue #18
sets — to answer a question a demo never asks. Those politeness rules exist to prevent
exactly this, and the way to respect them is to want less data, not to fetch the same
amount faster.

## What a quoted price means

> A Steak Burrito is $13.15.

is short for

> At restaurant 0679, as published on the date in `harvested_at`, a Steak Burrito
> was $13.15.

Both halves of that sentence are columns on the row: `restaurant_id`, `harvested_at`,
and `source_url` pointing at the endpoint it came from. Nothing has to be reconstructed
later, because by the time a price reaches a confirmation card there is nowhere left to
recover it from.

## What this leaves for later

- **The restaurant identifier is opaque.** `0679` is Chipotle's own; it has no street
  address attached to it here. [Issue #21](https://github.com/gganssle/chip_chat/issues/21)
  harvests store metadata, and joining it is what turns "restaurant 0679" into a place a
  visitor recognises.
- **`menu_catalog` has to flatten or not.** [Issue #24](https://github.com/gganssle/chip_chat/issues/24)
  builds the single source of truth for what is orderable. If it wants RFC-001 §04's
  single `base_price` column, the reference restaurant is the deterministic answer to
  which price that is — and the choice stays recoverable, because the per-restaurant
  rows are still there underneath it.
- **Delivery is a second price, not a surcharge.** Chipotle publishes
  `unitDeliveryPrice` as its own number, roughly thirty percent higher and not a fixed
  ratio. Both are carried. A demo that quotes a delivery total must use the delivery
  column rather than marking up the other one.
