# Decision: what `menu_catalog` adds to RFC-001 §04, and what it refuses to

**Issue:** [#24](https://github.com/gganssle/chip_chat/issues/24) (bead `cc-88y`) · **Decided:** 26 August 2026
**Amends:** RFC-001 §04, whose `menu_items` carries `allergens[]` and one `source_url`
**Builds on:** [`menu-pricing.md`](menu-pricing.md), [`allergen-absence.md`](allergen-absence.md)
**Unblocks:** [#25](https://github.com/gganssle/chip_chat/issues/25) (generator),
[#35](https://github.com/gganssle/chip_chat/issues/35) (chunking),
[#42](https://github.com/gganssle/chip_chat/issues/42) (Snowflake DDL),
[#53](https://github.com/gganssle/chip_chat/issues/53) / [#54](https://github.com/gganssle/chip_chat/issues/54) (vision)

---

RFC-001 §04 fixes the catalogue's schema, and issue #24 says to match it rather than
invent a parallel shape. This document is the list of places where matching it exactly
would have made the table say something untrue, what was done instead, and what each
change costs. Everything not listed here is the RFC's schema unchanged.

## The four additions

### 1. A merged row carries every provenance it merged

RFC-001 §04 gives `menu_items` one `source_url` and one `harvested_at`. But a
catalogue row is a join of three documents: the online menu publishes the item's
identity, the nutrition metadata publishes its calories, and the allergen data
publishes its marks — and for a handful of foods that last one is a *different*
document again, because the published chart names some items the metadata does not.

One `source_url` would have to name one of them and let the row's other columns
borrow it. RFC-001 §08 requires a quoted figure to cite where it came from; a calorie
figure citing the menu endpoint is a citation that does not support it.

So `menu_items` carries three pairs — `source_url`, `nutrition_source_url`,
`allergen_source_url`, each with its `harvested_at` — and `stores` carries two,
because a store's name and a store's address are published by different endpoints.
A null second provenance means the second document was silent about that row, which
is itself a fact worth carrying: it is exactly the case where nothing is known.

**Cost:** six columns instead of two, and a consumer has to know which one to quote.
The alternative was one column that is right about a third of the row.

### 2. `is_composed`, because a published figure is published for something

`CMG-2` is "Steak Burrito" on the menu and 150 calories of *steak* in the nutrition
metadata. The tortilla, the rice, the beans and the toppings are separate items that
Chipotle's own calculator adds up. A row that carried 150 in a column called
`calories` and said nothing else would be the easiest confidently-wrong number in the
project — off by roughly a factor of seven, and wrong in the direction that flatters.

`menu_items.is_composed` says which kind of figure the row holds. True means the item
is built from modifiers and a total is this figure plus each chosen modifier's
`delta_calories`.

The catalogue does **not** sum them, and that is deliberate. Any of the components may
be `None` because nobody published a figure for it, and every rule for what `None`
contributes to a sum is a lie: zero understates, skipping the row understates, and
deciding to refuse the question belongs to the confirmation card of
[#62](https://github.com/gganssle/chip_chat/issues/62), which is the thing that has to
show the visitor a number.

**Cost:** a consumer that wants a total has to write the sum and decide what an
unpublished component means. That decision is now visible instead of inherited.

### 3. `allergen_disclosure`, so `allergens[]` cannot lie by omission

This is the change that matters most, and it exists to preserve something the
nutrition harvest already got right rather than to fix something.

Chipotle's allergen control is titled "I'm Avoiding" with the subheader "Tagged items
contain your selection". A tag means *contains*; an absent tag is **not** a published
negative. `chip_chat.harvest`'s `AllergenStatus` encodes that in the type, with three
members: `CONTAINS`, `NOT_LISTED` and `NOT_PUBLISHED`. See
[`allergen-absence.md`](allergen-absence.md) for the harvest's argument.

RFC-001 §04's `allergens[]` is a single array. An array has room for the codes that
are `CONTAINS` and nothing else, so both kinds of silence fall out of it and both read
as "does not contain" — which is the flattening that a consolidation is the natural
place to commit, and which no test would catch, because a flattened model gives
confident, well-typed answers that are wrong only about safety.

Two things prevent it here:

- `menu_items.allergen_disclosure` is `PUBLISHED` or `NOT_PUBLISHED`. `NOT_PUBLISHED`
  is an item-level fact in the source — Chipotle either describes a food's allergens
  or it does not — so those two columns together reconstruct all three states, and
  `MenuItem.allergen_status()` is the method that does it. There is nowhere in that
  method for a boolean to appear.
- `item_allergens` carries the harvest's rows through unchanged, `AllergenStatus` and
  all, with a row for every catalogue item crossed with every published allergen code.
  A join that misses is a silence, and a silence about an allergen reads as
  reassurance.

`test_catalog_allergens.py` asserts the reconstruction against `item_allergens` for
every pair, and separately asserts that no column anywhere in the catalogue is a
nullable boolean — because `bool | None` is not a safe carrier for three states
either. The `None` would have to survive as *unknown* all the way to the answer, and a
single `or False` or `.get(code, False)` anywhere in the chain destroys it with no
error and no failing test.

`caveats` is carried into the catalogue for the same reason. Chipotle states that
cross-contact is not reflected on its chart, and that it cannot guarantee the absence
of eggs, mustard, peanuts, tree nuts, sesame, shellfish or fish even though they are
not used as ingredients. A table that answers allergen questions without carrying that
prose makes a stronger claim than the source does.

**Cost:** one enum column and a table of caveats that most queries will not read.

### 4. Columns the vision vocabulary is generated from

RFC-001 §07 requires the vision model's enums to be "generated from the live catalogue
at build time, so the model's vocabulary cannot drift from what is orderable", and
issue #24 asks that the catalogue be structured so that generation is *mechanical*.
Four columns exist for that and would otherwise have been dropped:

| Column | Slot it generates |
| --- | --- |
| `menu_items.item_type` | `vessel` — `Burrito`, `Bowl`, `Tacos` |
| `menu_items.primary_filling` | `protein` — `Chicken`, `Steak`, `Sofritas` |
| `modifiers.modifier_type` | `rice`, `beans`, `salsas`, `toppings` |

`modifiers` also keeps `modifier_item_id`, without which a modifier cannot be joined
to its own price or its own calories, and `portion_options`, which is the published
list of what "extra", "light", "side" and "half" are allowed on — so "extra cheese"
resolves to a published option or is refused.

A `vocabulary` table falls out of those columns, and the generated enum module falls
out of that table. Nothing is hand-maintained anywhere in the chain.

## The one thing derived rather than read

**Salsas published as toppings are split out by name.** Chipotle does publish a `Salsa`
modifier type — but only on its build-your-own items. On an ordinary entree the same
four salsas arrive with `itemType` `Toppings`, the same as cheese and lettuce, and
nothing published separates them. RFC-001 §07 gives salsas their own slot in the
stage-4 schema, so the split has to happen somewhere.

It happens here, from the published name, and every term it produces carries
`derivation = NAME_SUFFIX` — against `ITEM_TYPE`, `PRIMARY_FILLING` and
`MODIFIER_TYPE` for the three that are read off published columns. A reader can
therefore tell the inferred part of the model's vocabulary from the published part by
querying the table, without reading any code.

The alternative was to collapse §07's two slots into one. That is defensible and was
rejected because it changes the RFC's response schema to accommodate a classification
problem, rather than solving the classification problem and saying how.

## Three things the first real harvest changed

The fixture site the tests run against is a trimmed recording, and three of these
decisions did not survive contact with the whole menu. Recording them because each was
a design that looked obviously right until 192 items said otherwise.

**A vocabulary term resolves to a *set* of SKUs, not to one.** `VocabularyTerm` first
carried a single `item_id`, and the first real build failed on a collision: guacamole
is published as both `CMG-1001` and `CMG-5301`, and white rice, brown rice, black
beans, pinto beans and the chipotle-honey vinaigrette are all published under two
identifiers each. Which one a described meal means depends on which entree it is on.
So the column is `item_ids`, a sorted tuple, and it is the *candidate set* rather than
the answer: the matcher of #54 resolves `(entree, term)` against `modifiers`, which is
already keyed by `(item_id, modifier_item_id)`. A collision is now an error only when
two *different* published names slugify onto one value, which is the genuine ambiguity.

**The slot comes from `modifier_type`, not from the content group.** The content group
was the first signal used, and it is the less reliable one: rice is a
`RiceContentGroup` choice on a burrito and a `ToppingsContentGroup` choice on a salad,
and both rows are the same rice. `itemType` answers it for every entree in one column,
and it is also where the `Salsa` type turned up. A consequence, kept rather than
patched: rice and beans also appear in the `toppings` vocabulary, because on a salad
Chipotle really does publish them as toppings. Both slots resolve to the same SKU, so
the overlap costs nothing and hiding it would mean overriding the source.

**The `vessel` and `protein` vocabularies contain things that are neither.** The
published entree types include `KidsBYO`, `KidsQuesadilla`, `BYOProtein` and
`BYOChips`; the published primary fillings include `Cheese Only`, `Guacamole`,
`Queso Blanco` and `Large Chips (2)`. None of those is a vessel you would photograph
or a protein you would eat, and all of them were kept — because `(item_type,
primary_filling)` is how a described meal resolves to an entree, and dropping the odd
ones would make the Cheese Only Quesadilla and every Kids Build Your Own unreachable.
RFC-001 §07's illustrative `bowl|burrito|tacos|salad|quesadilla` is the subset a photo
would show, not the whole published vocabulary, and the RFC itself says the enum is
generated from the catalogue rather than fixed.

## Versioning: two digests, because there are two questions

Issue #24 says the catalogue is "harvested and versioned, and the synthetic account
data is generated against a specific version of it". Two different questions get asked
of a version, and one number cannot answer both:

- **`catalog_version`** is a SHA-256 over every table's serialised bytes, provenance
  included. It identifies one harvest exactly. Re-reading an unchanged menu next week
  produces a different one, correctly: it is a different read, and `harvested_at` is
  one of the things a citation quotes.
- **`content_version`** is the same digest with every `source_url` and `harvested_at`
  column stripped. It changes when what is orderable changes and not when it is merely
  re-read. This is the one [#25](https://github.com/gganssle/chip_chat/issues/25)
  records against a batch of generated orders: two harvests sharing a `content_version`
  compose the same orders.

Both are in `manifest.json` beside each table's row count and digest, and
`load_catalog` recomputes them from the rows it read rather than trusting them, so a
table edited after it was written is refused rather than served.

Item identifiers are Chipotle's own (`CMG-2`), and the one derived identifier —
`modifiers.modifier_id`, which is `<item_id>:<modifier_item_id>` — is derived from two
published ones. Nothing in the catalogue is keyed on a name, a position or a hash of
content, so a re-harvest that finds new prices leaves every identifier intact.
`test_catalog_stability.py` demonstrates that with a second harvest at a later clock
against a repriced site, which is issue #24's stability criterion.

## What one real harvest produces

One reference restaurant (0679) and fifty stores, harvested 26 August 2026. The
committed fixture under `catalog/tests/fixtures/catalog/` is a much smaller catalogue
built from the harvest tests' fixture site; these are the real numbers, for scale.

| Table | Rows |
| --- | --- |
| `menu_items` | 192 |
| `item_prices` | 192 |
| `modifiers` | 1,385 |
| `stores` | 50 |
| `item_allergens` | 768 |
| `allergens` | 4 |
| `caveats` | 5 |
| `vocabulary` | 48 |

`item_allergens` is 192 items crossed with the four published allergen codes — `dair`,
`glut`, `soy`, `sulp` — and reaches all three states: 61 `CONTAINS`, 695 `NOT_LISTED`
and 12 `NOT_PUBLISHED`. Those twelve are three non-food items (napkins and the like)
crossed with four codes, which is exactly the case the third state exists for. Three
items carry no published calorie figure, and they are the same three.

## What this leaves for later

- **Rewards do not join to items yet.** The harvest README nominates #24 for it, with
  the whole catalogue in hand. The published tiles do not support a mechanical join:
  half use marketing art, "ENTRÉE" is a category rather than an item, and "SIDE
  TORTILLA" matches no item name. A join built on the `cmg-NNNN` slug in the image
  path would also inherit the `CMG-1002` ambiguity the nutrition harvest found, where
  one identifier is both "Crispy Corn Tortilla" and "Tortilla Chips". Filed separately
  rather than guessed at here.
- **Meals are not in the catalogue.** `meals`, `meal_contents` and `meal_prices` are
  in the menu harvest and are a list of complete orders a customer really can place —
  which is what the action surface of
  [#23](https://github.com/gganssle/chip_chat/issues/23) wants. They are not what
  "what is orderable" means for the three consumers #24 names, and adding them would
  have widened the vocabulary generation with a second kind of thing. Filed separately.
- **Nutrition beyond calories.** `item_nutrition` publishes fourteen figures per item.
  The catalogue carries the one the confirmation card and the vision draft need. The
  rest stays queryable in the harvest tables, where the retrieval chunker of
  [#35](https://github.com/gganssle/chip_chat/issues/35) can pick it up as chunk
  metadata.
- **No PDF nutrition data exists, and that is a finding rather than a gap.** Chipotle
  published no PDF anywhere on 26 August 2026 — see
  [`../chipotle-pdf-spot-check.md`](../chipotle-pdf-spot-check.md). Nothing in the
  catalogue is built on the assumption that those tables will fill.
