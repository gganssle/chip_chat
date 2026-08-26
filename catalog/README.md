# catalog

The consolidated menu catalogue: the single source of truth for what is orderable.

Three harvests go in — the menu of issue #19, the nutrition and allergen data of
issue #20, the stores of issue #21 — and eight tables come out. None of the three
is the catalogue on its own. The menu knows what exists and what it costs, the
nutrition data knows what is in it, the policy corpus knows where you can buy it,
and the catalogue is the join.

Three subsystems resolve against it, and none of them may name a food that is not
in it:

1. **The synthetic order generator** (issue #25) composes orders only from
   catalogue rows, which is what keeps the fake-accounts / real-menu boundary
   honest.
2. **The vision matcher** (issue #54) resolves described slots to catalogue SKUs
   through a vocabulary *generated from this table at build time*, so the model's
   vocabulary cannot drift from what is orderable (RFC-001 §07).
3. **The retrieval chunker** (issue #35) treats one row of `menu_items` as one
   chunk, carrying its nutrition and allergen fields as metadata (RFC-001 §08).

## Building one

```bash
# Harvest first. The catalogue is a consolidation and fetches nothing new.
python -m chip_chat.harvest.sources.chipotle --landing landing --dataset all

# Then consolidate what is cached, and generate the vision vocabulary.
python -m chip_chat.catalog --landing landing --offline \
    --vocabulary build/vision_vocabulary.py
```

`--offline` makes no requests at all. Run it twice and compare the
`content_version` it prints: that is how the reproducibility claim is checked
rather than asserted. Without `--offline` it harvests whatever the landing zone
is missing first, at the politeness rate the harvest framework enforces.

```python
from chip_chat.catalog import build_catalog, load_catalog

catalog = build_catalog(menu, nutrition, policy)
catalog.write(blobs)

again = load_catalog(blobs)
assert again == catalog
```

## Eight tables come out

Written as JSON Lines under `catalog/chipotle/`, with a `manifest.json` carrying
each one's row count and SHA-256 alongside the two versions below.

| Table | What it is |
| --- | --- |
| `menu_items` | Every item, with its calories and allergen marks merged in. `category IS NULL` means it can only be ordered inside something else. |
| `item_prices` | Money and availability, one row per item *per restaurant*. |
| `modifiers` | What may go in which slot on which item, with the portion words each accepts. |
| `stores` | Where the restaurants are, what they are called, and their week. |
| `item_allergens` | The three-valued allergen answer, per item per published allergen. |
| `allergens` | The published allergen vocabulary — codes, and whatever labels came with them. |
| `caveats` | Chipotle's published prose about what its allergen chart does not cover. |
| `vocabulary` | The vision model's constrained vocabulary, per RFC-001 §07 slot. |

`docs/decisions/catalog-shape.md` argues every column that is not RFC-001 §04's.

## Before you answer an allergen question out of this

**An absent allergen code is not an absent allergen.** Chipotle's control is
titled "I'm Avoiding" with the subheader "Tagged items contain your selection", so
a tag means *contains* and an untagged item is not a published negative. Three
states, never a boolean:

```python
item.allergen_status("dair")
# AllergenStatus.CONTAINS      Chipotle marks this item with it.
# AllergenStatus.NOT_LISTED    It publishes marks for this item; this is not among
#                              them. NOT a claim that the item is free of it.
# AllergenStatus.NOT_PUBLISHED It publishes nothing about this item at all.
```

`menu_items.allergens` is the `CONTAINS` set and `menu_items.allergen_disclosure`
is the item-level half; together they reconstruct all three, and
`test_catalog_allergens.py` asserts that reconstruction against `item_allergens`
for every pair. Do not narrow any of it to `bool` or to `bool | None` on the way
to an answer — a single `or False` or `.get(code, False)` turns "nothing is
published" into "does not contain" with no error and no failing test.

And carry `caveats` with the answer. Chipotle states that cross-contact is not
reflected on its chart, and that it cannot guarantee the absence of eggs, mustard,
peanuts, tree nuts, sesame, shellfish or fish even though they are not used as
ingredients. An answer without that makes a stronger claim than the source does.

## Before you quote a calorie figure out of this

**A composed item's published figure is its own component's, not the meal's.**
`CMG-2` is "Steak Burrito" on the menu and 150 calories of steak in the nutrition
metadata; the tortilla, the rice, the beans and the toppings are separate items
that Chipotle's calculator adds. `menu_items.is_composed` says which kind of
figure a row holds.

The catalogue does not sum them. A component may be `None` because nobody
published a figure for it, and every rule for what `None` contributes to a sum
understates the total. Deciding what to show a visitor is the confirmation card's
job (issue #62), in the open.

## Before you quote a price out of this

**There is no `base_price` column, because Chipotle does not have one.** A Steak
Burrito was $11.15 at one restaurant and $13.15 at another on the same afternoon.
Prices live in `item_prices`, keyed by `restaurant_id`, so a quoted price always
has a store and a `harvested_at` attached to it. `docs/decisions/menu-pricing.md`
has the numbers and the argument.

## The vision vocabulary is generated, not maintained

RFC-001 §07 requires the model's enums to come from the live catalogue at build
time. `--vocabulary PATH` writes a module of `StrEnum`s — one per slot — with the
term-to-SKU map and the stage-4 response schema beside them:

```python
class Vessel(StrEnum):
    BOWL = "bowl"
    BURRITO = "burrito"

SLOT_ITEMS = {"rice": {"white_rice": ("CMG-5001", "CMG-5375")}, ...}
DESCRIBE_SCHEMA = {...}  # the model may return nothing else
```

**A term names a set of SKUs, not one.** Chipotle publishes guacamole as both
`CMG-1001` and `CMG-5301`, and white rice, brown rice, black beans, pinto beans
and the honey vinaigrette under two identifiers each. Which one a described meal
means depends on the entree it is on, so `SLOT_ITEMS` is the candidate set and
the resolution is `(entree, term)` against `modifiers` — which is keyed by
`(item_id, modifier_item_id)` for exactly this reason. `vessel` and `protein` are
empty for a different reason: each is half of an entree, and the pair resolves
through `(item_type, primary_filling)`.

Two things about the real vocabulary look wrong and are not. Rice and beans also
appear under `toppings`, because on a salad Chipotle really does publish them as
toppings — both slots resolve to the same SKU, so the overlap costs nothing.
And `vessel` holds `kidsbyo` and `byochips` while `protein` holds `cheese_only`
and `large_chips_2`, because those are the published entree types and fillings;
dropping them would make the Cheese Only Quesadilla and every Kids Build Your Own
unreachable.

**Nothing in this repository commits one.** A module generated from a smaller
catalogue and then committed is the hand-maintained list the generation exists to
replace. The one copy under `tests/fixtures/vision-vocabulary.py.txt` is generated
from the fixture site, kept as `.txt` so nothing can import it by accident, and
regenerated and compared on every test run.

Every vocabulary row carries a `derivation` saying how it got its slot. Three of
the four are published columns; the fourth, `NAME_SUFFIX`, is the one inference in
the package — Chipotle publishes a `Salsa` modifier type, but only on its
build-your-own items, so on an ordinary entree the same four salsas arrive as
toppings with nothing published separating them. RFC-001 §07 gives salsas a slot
of their own, so the split is made from the published name and labelled as the
inference it is. A column rather than a secret.

## Two versions, because there are two questions

- **`catalog_version`** — a digest over every table, provenance included. It
  identifies one harvest exactly, and moves when the same menu is read again.
- **`content_version`** — the same digest with every `source_url` and
  `harvested_at` stripped. It moves only when what is orderable changes. This is
  the one issue #25 records against a batch of generated orders: two harvests
  sharing a `content_version` compose the same orders.

`load_catalog` recomputes both from the rows it read rather than trusting the
manifest, so a table edited after it was written is refused rather than served.

## Identifiers survive a re-harvest

Item identifiers are Chipotle's own. The one derived identifier —
`modifiers.modifier_id`, which is `<item_id>:<modifier_item_id>` — is derived from
two published ones, because the same ingredient on a different item is a different
modifier with a different allowance. Nothing is keyed on a name, a position or a
hash of content, so a re-harvest that finds new prices leaves every order composed
against the old one still resolvable. `tests/test_catalog_stability.py`
demonstrates it with a second harvest at a later clock against a repriced site.

## Starting without a harvest

`tests/fixtures/catalog/` holds a complete small catalogue built from the harvest
tests' fixture site, with its row counts in `manifest.json`. It exists so that
downstream work can start on a laptop with no network:

```python
from chip_chat.catalog import load_catalog
from chip_chat.harvest.blobs import LocalBlobStore

catalog = load_catalog(LocalBlobStore(Path("catalog/tests/fixtures")), "catalog")
```

It is a *fixture*, not a menu: two entrees, one side, one drink and one non-food
item. Nothing built from it should ever be served. For scale, one real harvest of
the reference restaurant and fifty stores on 26 August 2026 produced 192
`menu_items`, 192 `item_prices`, 1,385 `modifiers`, 50 `stores`, 768
`item_allergens` and 48 `vocabulary` rows — see
`docs/decisions/catalog-shape.md`. Regenerate the fixture with

```bash
uv run python catalog/tests/regenerate_fixture.py
```

after any deliberate change to the tables — `test_catalog_fixture.py` regenerates
and compares on every run, so the sample cannot quietly describe last week's
schema.
