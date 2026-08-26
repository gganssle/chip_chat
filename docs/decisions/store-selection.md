# Decision: which fifty of four thousand stores get harvested

**Issue:** [#21](https://github.com/gganssle/chip_chat/issues/21) (bead `cc-6o8`) · **Decided:** 26 August 2026
**Depends on:** [`menu-pricing.md`](menu-pricing.md), whose reference restaurant this has to include
**Unblocks:** [#24](https://github.com/gganssle/chip_chat/issues/24) (`menu_catalog`), [#23](https://github.com/gganssle/chip_chat/issues/23) (action surface), [#27](https://github.com/gganssle/chip_chat/issues/27) (loyalty ledger), RFC-001 §04 `stores`

---

Issue #21 asks for "enough stores, with city/region/hours, to populate the `stores`
table and make 'the Ballard store' mean something". Chipotle's locator publishes 4,143
of them. Thirty is the floor. So somebody has to choose, and the choice is worth
writing down because two obvious ways of making it are both wrong.

## The fact that forces a decision

The locator's sitemap lists every store, but a store's page publishes its restaurant
number only *inside* the page — the URL is built from the street address. So the set
cannot be chosen by number, and it cannot be chosen by asking the API: the restaurant
search endpoint returns operational metadata with no address and no hours in it.

At the same time, `item_prices.restaurant_id` from issue #19 points at restaurant
**0679**, and a price whose restaurant has no address is exactly what
[`menu-pricing.md`](menu-pricing.md) says a quoted price must not be.

## The decision

**Fifty stores, chosen by round-robin across the states in the published sitemap,
with the reference restaurant's page always first and always checked.**

```
sitemap.xml  →  sitemap1.xml, sitemap2.xml  →  4,143 store URLs
             →  sort, group by the state in the path
             →  one per state in turn until fifty
```

Sorting first makes the choice a function of the sitemap and nothing else: the same
sitemap always yields the same fifty, which is what lets an offline re-parse address
exactly the pages the harvest landed rather than a list it had to remember.

Taking one state at a time is what stops fifty stores being fifty branches of Los
Angeles — California alone has 532 pages, and a plain "first fifty sorted" would have
been all of them. As of this harvest the round-robin covers all forty-nine states and
territories the locator lists, plus restaurant 0679.

`REFERENCE_STORE_URL` is the one hardcoded value, and it is hardcoded because it cannot
be derived. It is **checked rather than trusted**: the parser raises unless a store with
number 679 comes out of the harvest. A locator that moved that URL to a different
restaurant would otherwise silently attach issue #19's prices to the wrong address.

The parser also raises below thirty stores, so issue #21's acceptance criterion is
verified on every run rather than asserted once in a document.

## Why not the alternatives

**A hand-picked list of interesting cities.** Readable, and dishonest in a way that
compounds: whoever picks the cities picks what the demo can talk about, and the next
person cannot tell which entries were chosen for a reason and which were typed. It also
goes stale silently — a closed restaurant becomes a 404 in a constant nobody re-reads.

**Every store.** 4,143 pages at 90 KB each is 370 MB pulled from a third party over
two and a half hours of continuous crawling at issue #18's two-second rate, to populate
a demo that shows one store at a time. The politeness rules exist to prevent exactly
this.

**The first N in sitemap order.** Deterministic only as long as the sitemap's order is,
and it is not sorted — it is whatever the locator's build emitted. A regenerated sitemap
would silently change the dataset.

## The cost, stated plainly

**The fifty stores are not near anybody.** They are one per state, chosen
alphabetically within each — `al/alabaster`, `ar/bentonville`, `az/anthem`. A visitor
in Seattle will not find their own restaurant in this dataset, and the demo should not
pretend otherwise. What the dataset supports is *a* real store with a real name, a real
address and real opening hours behind every persona and every order; what it does not
support is "find the Chipotle nearest me".

Widening it is an argument, not a schema change:

```bash
python -m chip_chat.harvest.sources.chipotle --landing landing \
    --dataset policy --stores 200
```

## Two tables, not one

RFC-001 §04 wants `stores store_id, name, city, region, hours`. The harvest lands three
tables instead, and the reason is provenance rather than normalisation:

| Table | Document it comes from |
| --- | --- |
| `stores` | the locator page — address, city, region, postcode, coordinates, telephone |
| `store_profiles` | `restaurant/v3/restaurant/<id>` — the name, status, operational region |
| `store_hours` | the locator page — one row per store per day |

Every locator page in the country calls its restaurant "Chipotle Mexican Grill". The
name that makes "the Ballard store" resolve — `Lakewood Mall`, `Capitol Hill`,
`Interbay` — is published only by the restaurant endpoint. Merging the two would produce
a row with two `source_url`s and therefore, in practice, none, and RFC-001 §08 needs
every row to be able to say where it came from. Issue #24 joins them on `store_id` when
it builds the serving-layer table.

`store_hours` holds seven rows per store whether or not seven were published, with an
`is_published` flag, for the same reason `item_allergens` holds a row per allergen (see
[`allergen-absence.md`](allergen-absence.md)): a missing row reads as "nothing to worry
about", and "we publish nothing about Sunday" is not the same answer as "closed on
Sunday".
