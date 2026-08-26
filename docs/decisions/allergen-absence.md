# Decision: an absent allergen is a value, and it is not a negative

**Issue:** [#20](https://github.com/gganssle/chip_chat/issues/20) (bead `cc-2bv`) · **Decided:** 26 August 2026
**Implements:** PRD K3, and the system design's fifth trap, *"hand-waving allergens"*
**Unblocks:** [#24](https://github.com/gganssle/chip_chat/issues/24) (`menu_catalog`), [#22](https://github.com/gganssle/chip_chat/issues/22) (PDF nutrition sheets)

---

*"Does this contain dairy?"* is about to be asked by strangers on the open internet. This
is the decision about what Cilantro is allowed to answer.

## The fact that forces a decision

Chipotle publishes allergen marks against twenty-six of its foods, in four categories:
dairy, soy, gluten and sulphites. The control that applies those marks is labelled
**"I'm Avoiding"**, and its published explanation is:

> Tagged items contain your selection.

A mark means *contains*. That much is unambiguous. What an *absent* mark means is where
the trouble starts, and Chipotle answers it directly on the same page:

> Individual foods may come into contact with one another during preparation, which is
> not reflected on this chart. Although we do not use eggs, mustard, peanuts, tree nuts,
> sesame, shellfish, or fish as ingredients in our food, Chipotle cannot guarantee the
> complete absence of these allergens in its restaurants. Additionally, limited time
> offerings or menu items in test may include one or more allergens, including allergens
> not identified on this page.

So an unmarked item is not a safe item. It is an item Chipotle has declined to make a
promise about. And separately from both of those, there are items Chipotle publishes no
allergen data about at all — napkins and serving hardware today, and anything new
tomorrow.

That is three facts, and a boolean column can hold one of them.

| What is true | What a boolean would say |
| --- | --- |
| Chipotle marks Monterey Jack with dairy | `true` |
| Chipotle publishes allergen data for steak and does not mark dairy | `false` |
| Chipotle publishes nothing at all about Napkins & Utensils | `false`, or a missing row |

The second and third rows are the problem. Both come out of a boolean as *no dairy*, and
one of them is a sentence nobody at Chipotle has ever written down.

## The decision

**Model the answer as three values, and give every orderable item a row for every
published allergen.**

```
CONTAINS       Chipotle marks this item with this allergen.
NOT_LISTED     Chipotle publishes allergen data for this item and does not mark this one.
NOT_PUBLISHED  Chipotle publishes no allergen data for this item at all.
```

`item_allergens` is dense: every item on the harvested menu — its modifiers included —
crossed with every published allergen code. Napkins get four `NOT_PUBLISHED` rows rather
than no rows. **A join that misses is a silence, and a silence is what gets read as
reassurance.** There is no encoding in this dataset in which a missing value can be
mistaken for a negative one.

The same rule runs one column at a time through `item_nutrition`, where `value` is
`null` when the item published no figure and `0` when it published zero. Trans fat
published as `0` is a measurement. Trans fat that is absent is not. A test asserts on the
serialised bytes that the two do not converge.

**`NOT_LISTED` is not "free of".** It is the weakest of the three and the easiest to
render wrongly, so the published caveats are carried in the dataset — verbatim, hedges
included, in the `caveats` table — and the parser refuses to build if the page ever stops
publishing them. An answer that reports the mark without the hedge has changed what
Chipotle said.

## What this costs

`item_allergens` is 1,644 rows for one restaurant where a sparse table of marks would be
109. That is the price, and it is four hundred kilobytes.

The three-valued column also cannot be used in a `WHERE contains_dairy = false` clause
without someone deciding which of the two negatives they meant. That is not a cost. That
is the decision doing its job: the choice between *not marked* and *nothing known* is a
judgement about what to tell a customer, and it belongs in the answer layer, in daylight,
not hidden in a column default.

## Two things this deliberately does not do

**It does not merge the two published sources.** Chipotle publishes allergen data twice —
in the chart behind `/allergens`, and as tags on the menu metadata the nutrition
calculator reads. They agree exactly about allergens today, on all twenty-six foods both
describe. The parser asserts that agreement on every run and **raises** if it ever breaks,
rather than preferring one document. A harvest that picks a winner between two disagreeing
allergen sources has started guessing about safety data.

They do *not* agree about diets: the chart marks nine foods Whole30 under the code `whol`
and the metadata marks two under `wh30`, and nothing published says those are the same
diet. So `item_diets` records the document alongside the answer and merges nothing.

**It does not decide what an allergen is.** Which codes count as allergens is read from
Chipotle's own classification — the chart sorts every code it uses into an `allergens`
list and a `diets` list — and inherited across the published tag groups. Nothing matches
on a group's display name or on the spelling of a code. `dair` is an allergen because the
chart publishes it as one, not because it looks like "dairy". A fifth allergen added to
that group next month is covered next month, without anyone editing a constant.

## The one derivation, named

Everything in this dataset is a published value with its provenance attached, except one
thing: **a tag code inherits its kind from its published group.** If a group contains a
code the chart classifies as an allergen, every code in that group is an allergen. That is
the mechanism that makes the vocabulary extensible without a hand-maintained list, and it
is the only inference in the parser. It fails loudly — a group that mixed a chart-allergen
with a chart-diet would raise rather than pick.

Two codes come out of it unclassified: `whol` and `wh30`, neither of which the published
data ever labels. They keep a null name and a null kind. Calling them "Whole30" would be
putting a word in a restaurant's mouth about food, which is the one place this demo cannot
afford to be creative.

## See also

- The hand check against the live site: [`../chipotle-nutrition-spot-check.md`](../chipotle-nutrition-spot-check.md)
- The tables and what each column means: `harvest/src/chip_chat/harvest/sources/chipotle/nutrition_records.py`
- The related pricing decision: [`menu-pricing.md`](menu-pricing.md)
