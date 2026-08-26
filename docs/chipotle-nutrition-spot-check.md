# Spot check: the nutrition and allergen harvest against the live site

**Issue:** [#20](https://github.com/gganssle/chip_chat/issues/20) (bead `cc-2bv`) · **Checked:** 26 August 2026
**Checked by hand against:** `https://www.chipotle.com/nutrition-calculator/burrito` and
`https://www.chipotle.com/allergens`, both read in a browser, as a customer sees them.

Issue #20 asks for at least twenty items to be spot-checked by hand and the check
recorded. This is the record. It is worth being explicit about why it exists: the unit
tests run against fixtures, so a green suite proves the parser is self-consistent, not
that it agrees with what Chipotle is publishing this afternoon. Only this does.

The check compares the rendered pages against
`landing/parsed/chipotle/nutrition/item_nutrition.jsonl` and `allergen_chart.jsonl` from a
harvest run the same day.

---

## Nutrition — 24 items, all matching

Read off the burrito builder, which shows each ingredient's own calories and macros. Every
figure below matched the harvested row exactly; nothing was rounded, converted or adjusted
to make it match.

| Item | `item_id` | Live cal | Harvested cal | Live fat | Harvested fat |
| --- | --- | ---: | ---: | ---: | ---: |
| Tortilla | `CMG-4025` | 320 | 320 | 9g | 9 |
| Chicken | `CMG-1` | 180 | 180 | 7g | 7 |
| Steak | `CMG-2` | 150 | 150 | 6g | 6 |
| Beef Barbacoa | `CMG-4` | 170 | 170 | 7g | 7 |
| Carnitas | `CMG-3` | 210 | 210 | 12g | 12 |
| Sofritas | `CMG-5` | 150 | 150 | 10g | 10 |
| White Rice | `CMG-5001` | 210 | 210 | — | 4 |
| Brown Rice | `CMG-5002` | 210 | 210 | — | 6 |
| Black Beans | `CMG-5051` | 130 | 130 | — | 1.5 |
| Pinto Beans | `CMG-5052` | 130 | 130 | — | 1.5 |
| Guacamole | `CMG-1001` | 230 | 230 | 22g | 22 |
| Fresh Tomato Salsa | `CMG-5201` | 25 | 25 | — | 0 |
| Roasted Chili-Corn Salsa | `CMG-5202` | 80 | 80 | — | 1.5 |
| Tomatillo-Green Chili Salsa | `CMG-5203` | 15 | 15 | — | 0 |
| Tomatillo-Red Chili Salsa | `CMG-5204` | 30 | 30 | — | 0 |
| Sour Cream | `CMG-5251` | 110 | 110 | — | 9 |
| Cheese | `CMG-5252` | 110 | 110 | — | 8 |
| Queso Blanco | `CMG-1029` | 120 | 120 | — | 9 |
| Fajita Veggies | `CMG-5101` | 20 | 20 | — | 0 |
| Romaine Lettuce | `CMG-5351` | 5 | 5 | — | 0 |
| Chips | `CMG-1002` | 540 | 540 | — | 25 |
| Soft Flour Tortilla (taco) | `CMG-5501` | 250 | 250 | — | 8 |
| Chipotle-Honey Vinaigrette | `CMG-5353` | 220 | 220 | — | 16 |
| Jarritos Guava | `CMG-2022` | 110 | 110 | — | 0 |

A dash means the builder did not surface that macro in the row; the calories were the
compared figure. Protein and carbs were also checked on the first six and matched
(Chicken 32g/0g, Steak 21g/1g, Carnitas 23g, Sofritas, Tortilla 8g/50g, White Rice 40g,
Brown Rice 36g, Black Beans 22g, Pinto Beans 21g).

### The trap this check confirms

**A composed entree's published figure is its own ingredient's, not the meal's.** With
only the tortilla selected, the live builder's header reads **320 cal** — the tortilla —
and every ingredient adds to it. `CMG-2` is on the menu as "Steak Burrito" and in the
nutrition metadata as 150 calories of steak for a four-ounce portion.

Reading `item_nutrition[CMG-2].tcal` as "a Steak Burrito is 150 calories" would be
confidently wrong by about a thousand. This is stated in `ItemNutrient`'s docstring for
exactly that reason.

## Allergens — all 26 chart rows, all matching

The live chart publishes four allergen columns — **Dairy, Soy, Gluten, Sulphites** — which
is exactly the vocabulary the harvest derived from Chipotle's own classification
(`allergen_codes` in the manifest: `dair`, `glut`, `soy`, `sulp`).

Eight of the twenty-six rows carry a mark on the live page. All eight matched, and the
eighteen unmarked rows were unmarked in the harvest too:

| Chart row | Live marks | Harvested `allergen_codes` |
| --- | --- | --- |
| Flour Tortilla (Burrito) | G, Su | `["glut", "sulp"]` |
| Flour Tortilla (Taco) | G, Su | `["glut", "sulp"]` |
| Monterey Jack Cheese | D | `["dair"]` |
| Queso Blanco | D | `["dair"]` |
| Sour Cream | D | `["dair"]` |
| Sofritas | S, Su | `["soy", "sulp"]` |
| Red Chimichurri Sauce | Su | `["sulp"]` |
| Chipotle Honey Vinaigrette | Su | `["sulp"]` |
| *the other eighteen rows* | none | `[]` |

The eighteen unmarked: Barbacoa, Black Beans, Brown Rice, Carnitas, Chicken, Crispy Corn
Tortilla, Fajita Vegetables, Fresh Tomato Salsa, Guacamole, Pinto Beans, Roasted Chili-Corn
Salsa, Romaine Lettuce, Steak, Supergreens Lettuce Blend, Tomatillo Green-Chili Salsa,
Tomatillo Red-Chili Salsa, Tortilla Chips, White Rice.

Every one of those eighteen is `NOT_LISTED` in `item_allergens`, never `false` and never
missing. See [`decisions/allergen-absence.md`](decisions/allergen-absence.md) for why that
distinction is the whole point.

## Caveats — verbatim

Both footnotes and the contact-during-preparation paragraph were compared against the
rendered page and match character for character in `caveats.jsonl`, including the
sulphites footnote's en dash and the "10 ppm" figure.

## What was not checked by hand, and why

- **The other 385 items** in the metadata document. They are not reachable through the
  burrito builder without walking every meal type, and the twenty-four above already
  cross proteins, rice, beans, salsas, dairy, tortillas, chips and drinks.
- **The three non-food items** — Napkins & Utensils, 6 Serving Bowls, Serving Utensils —
  which are the twelve `NOT_PUBLISHED` rows in the manifest's coverage. They are absent
  from every allergen surface, which is the fact being recorded; there is nothing on the
  live site to compare them against.
- **Per-restaurant variation.** Nutrition and allergen data are published once for the US
  web channel, not per store, unlike prices. The harvest reads the restaurant menu only
  for its item list.

## Redoing this check

```bash
python -m chip_chat.harvest.sources.chipotle --landing landing --dataset nutrition
```

Then open the two pages above and compare. The manifest's `coverage` block is the fast
signal that something has moved:

```json
"coverage": {
  "allergen_codes": 4,
  "items": 411,
  "contains": 109,
  "not_listed": 1523,
  "not_published": 12,
  "nutrient_figures": 5712,
  "nutrient_figures_null": 0
}
```

`allergen_codes` dropping, or `not_published` climbing, means the published data moved and
this check is due again.
