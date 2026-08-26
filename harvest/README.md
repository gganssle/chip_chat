# harvest

Public menu, nutrition, and policy ingestion.

This package is the framework the source-specific harvesters sit on. The
manners live here — reading `robots.txt` and obeying it, a politely slow
request rate, an honest User-Agent, and caching every response so a URL is
fetched exactly once — so that no individual harvester has to remember them,
and none can quietly skip them.

## Using it

```python
from pathlib import Path

from chip_chat.harvest import Harvester, HttpxTransport, LocalBlobStore

with Harvester(LocalBlobStore(Path("landing")), HttpxTransport()) as harvester:
    menu = harvester.fetch_json("https://example.com/api/menu")
    page = harvester.fetch("https://example.com/nutrition")

print(page.source_url, page.harvested_at)
```

The second run of that program makes no network requests at all.

Prefer `fetch_json` against the endpoints a site's own front end calls over
`fetch` against its HTML. Those endpoints are more stable than the markup
around them, far lighter to fetch, and arrive structured instead of scraped.

## What the framework does on every fetch

1. **Reads `robots.txt` for the origin and obeys it.** A disallowed path
   raises `RobotsDisallowedError`. Nothing catches it and fetches anyway. If
   `robots.txt` cannot be read at all — a 5xx, a dead connection — the
   framework refuses everything for that origin rather than guessing in the
   permissive direction. A 404 means the site published no rules, which does
   mean everything is permitted. Rules are re-read after a day.
2. **Looks in the cache.** A hit returns without touching the network.
3. **Waits at the politeness gate.** A real delay since the last request
   (two seconds, or longer if the site declares a `Crawl-delay`), and a
   process-wide ceiling on requests in flight. Both live on the shared
   `GLOBAL_GATE`, so adding a fourth harvester cannot triple the request rate
   a site sees.
4. **Fetches, with backoff.** Timeouts, 429s and 5xxs are retried with
   exponential backoff, honouring `Retry-After`. A 4xx is never retried.
5. **Writes the raw bytes to the landing zone**, untouched and unparsed, with
   `source_url` and `harvested_at` recorded alongside. Those two fields are
   captured here, at the edge, because by the time a chunk reaches the
   retrieval index there is nowhere left to recover them from — and RFC-001
   section 08 requires them to survive into the response payload as citations.

## The cache

Bodies are stored under the SHA-256 of their own bytes, with one small JSON
pointer per URL naming the digest it currently resolves to. Re-harvesting an
unchanged page therefore writes nothing, and re-harvesting a changed one
writes a new blob *beside* the old one and records the digest it replaced.
A weekly re-harvest can diff rather than blindly overwrite.

`BlobStore` is the storage seam. `LocalBlobStore` writes a directory tree and
`InMemoryBlobStore` writes nothing at all; the ADLS Gen2 raw landing zone
plugs in behind the same four methods without a harvester noticing.

## Sources

Source-specific harvesters live in `chip_chat.harvest.sources`, one subpackage
per site. Each is split into a fetch step that lands raw bytes and a parse step
that reads only from the cache, so a parser bug costs a re-run rather than
another pass over someone else's servers.

### Chipotle's menu — `sources.chipotle`

Issue #19. Items, descriptions, the modifier taxonomy, and prices.

```bash
python -m chip_chat.harvest.sources.chipotle --landing landing
python -m chip_chat.harvest.sources.chipotle --landing landing --offline
```

The first run fetches four documents for one restaurant and parses them. The
second parses what is cached and cannot fetch anything at all. Run it twice and
the manifests it prints are identical, which is how issue #19's reproducibility
criterion is checked rather than asserted.

**The API address is discovered, not hardcoded.** Chipotle's public page hands
its own front end the services host and subscription key in a pair of `<meta>`
tags, and this source reads the same two tags. Nothing here holds a copied
credential that goes stale the day it is rotated; if the page stops publishing
them, the harvest stops with an error rather than falling back on a remembered
value.

**Ten tables come out**, written as JSON Lines beside the raw bytes under
`parsed/chipotle/menu/`, with a `manifest.json` carrying each one's row count
and SHA-256:

| Table | What it is |
| --- | --- |
| `menu_items` | Every item, orderable alone or only as a modifier. A null `category` means the latter. |
| `item_prices` | Money and availability, one row per item *per restaurant*. |
| `modifier_groups` | The slots on an item, and how many choices each accepts. |
| `modifiers` | Which item may go in which slot on which other item. |
| `portion_options` | Light, Extra, Side, Half — where each is allowed. |
| `meals` | The preconfigured orders Chipotle names and describes. |
| `meal_contents` | What each of those is made of. |
| `meal_prices` | What each restaurant charged for them. |
| `ingredients` | The published prose about each ingredient. |
| `item_ingredients` | The published taxonomy: proteins, rice and beans, toppings, sides. |

**Prices are per restaurant, and that is a decision** — Chipotle's own prices
vary by nearly twenty percent between stores. See
[`docs/decisions/menu-pricing.md`](../docs/decisions/menu-pricing.md) for what a quoted price means
and why one reference restaurant is harvested by default.

**Descriptions are joined exactly or not at all.** Chipotle publishes prose
about ingredients and prose about meals, and none about a Steak Burrito. The
ingredient corpus links an ingredient to every item that *contains* it — black
pepper lists the Steak Burrito — so an item takes an ingredient's description
only when the ingredient is named after it and lists it. Everything else keeps a
null description. The prose is all still there, in `ingredients` and `meals`,
correctly keyed to what it actually describes.

### Chipotle's nutrition and allergens — `sources.chipotle`, `--dataset nutrition`

Issue #20. The safety-critical half, kept as a separate dataset because it is
answering a different kind of question.

```bash
python -m chip_chat.harvest.sources.chipotle --landing landing --dataset nutrition
python -m chip_chat.harvest.sources.chipotle --landing landing --dataset all
```

Five documents: the public page again, the menu metadata *with nutrition* that
the nutrition calculator reads, the allergen and diet endpoint the `/allergens`
chart is drawn from, the `/allergens` page itself for the prose around that
chart, and the restaurant's menu — for its item list, not its prices. Both
datasets share one cache, so `--dataset all` costs seven documents rather than
nine.

**Eight tables come out**, under `parsed/chipotle/nutrition/`:

| Table | What it is |
| --- | --- |
| `nutrients` | The published nutrient vocabulary: `tcal` is `Total Calories` in `cal`, `calc` is `Calcium` as a `%`. |
| `item_nutrition` | Every published figure, one row per item per nutrient, with the portion it is for. |
| `item_group_calories` | Published calorie *ranges* for interchangeable items — the lemonades are "170-250 cal". |
| `dietary_tags` | The published tag vocabulary, allergens and diets alike, classified as Chipotle classifies them. |
| `item_allergens` | The three-valued allergen answer, for every orderable item. |
| `item_diets` | What each document says about each diet, per item, unmerged. |
| `allergen_chart` | The published chart in its own shape, one row per line. |
| `caveats` | Chipotle's published prose about what the chart does not cover. |

**An absent allergen is a value, and it is not a negative.** `item_allergens`
holds `CONTAINS`, `NOT_LISTED` or `NOT_PUBLISHED` — never a boolean — and there
is a row for every item on the menu crossed with every published allergen, so
"nothing is published about this" is a row that says so rather than a row that
is not there. `item_nutrition.value` is `null` for a figure nobody published and
`0` for a published zero. See
[`docs/decisions/allergen-absence.md`](../docs/decisions/allergen-absence.md) for the decision
and what it costs, and
[`docs/chipotle-nutrition-spot-check.md`](../docs/chipotle-nutrition-spot-check.md) for the hand
check against the live site.

**A composed entree's published figure is its own ingredient's, not the meal's.**
`CMG-2` is "Steak Burrito" on the menu and 150 calories of steak in the
nutrition metadata; the tortilla, rice, beans and toppings are separate items
that the calculator adds. Reading a component figure as a total is the easiest
way to publish a confidently wrong calorie count.

**Two published sources are checked against each other rather than merged.** The
chart and the metadata agree exactly about allergens today, on all twenty-six
foods both describe, and the parser asserts that on every run — a disagreement
raises rather than picking a winner. They disagree about Whole30, which is why
`item_diets` records the document alongside the answer.

## Testing against it

`chip_chat.harvest.testing` ships `FakeTransport` and `FakeClock`. Use them —
a harvester test must never fetch from a real site, and a rate-limiter test
must never actually sleep.

```python
from chip_chat.harvest import Harvester, InMemoryBlobStore
from chip_chat.harvest.testing import FakeClock, FakeTransport, fake_response

transport = FakeTransport({"https://example.test/api/menu": fake_response(...)})
harvester = Harvester(InMemoryBlobStore(), transport, clock=FakeClock())
```

`transport.requests` records every call, which is how `test_harvester.py`
proves that a warm cache makes zero of them.
