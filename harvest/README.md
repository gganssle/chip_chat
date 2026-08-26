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
chart, and the restaurant's menu — for its item list, not its prices. This
dataset and the menu share one cache, so building both costs seven documents
rather than nine. `--dataset all` adds the policy corpus below.

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

### Chipotle's rewards, FAQ, catering and stores — `sources.chipotle`, `--dataset policy`

Issue #21. The policy half of the corpus: what the rules actually say, what a
reward actually costs, whether Chipotle caters, and where the restaurants are.

```bash
python -m chip_chat.harvest.sources.chipotle --landing landing --dataset policy
python -m chip_chat.harvest.sources.chipotle --landing landing --dataset policy --stores 200
```

Much the largest of the three datasets, because of the stores: a locator page and
a profile for each of fifty of them, on top of a sitemap that lists all four
thousand. A cold run makes 113 requests and lands about eight megabytes, which at
the two-second politeness gate takes a little under four minutes. A warm one makes
none.

Five kinds of thing, from five different places:

| Where | What comes out of it |
| --- | --- |
| `/rewards-terms` | The programme's terms, split into their published sections |
| `/rewards` | The Rewards Exchange line-up, with its point costs |
| `graphql/execute.json/chipotle/FAQ-Query` | 136 published answers in 40 sections |
| `catering.chipotle.com` + `cateringorder/v1/menu/tiered` | Six catering packages, priced |
| `locations.chipotle.com` + `restaurant/v3/restaurant/<id>` | Fifty stores with hours, addresses and names |

**Ten tables come out**, under `parsed/chipotle/policy/`:

| Table | What it is |
| --- | --- |
| `policy_documents` | One row per policy page, labelled `TERMS` or `OVERVIEW`. |
| `policy_sections` | Its sections, in order, with their headings — the boundaries a chunker needs. |
| `faq_categories` | The FAQ's own two-level table of contents, in its published order. |
| `faq_entries` | Every published question and answer, with any URLs the answer linked to. |
| `rewards` | The published rewards and what each costs in points. |
| `catering_packages` | What a catering order can be made of, with prices and party sizes. |
| `catering_package_options` | What goes in each one, and whether it is chosen or included. |
| `stores` | Address, city, region, coordinates and telephone, per store. |
| `store_profiles` | The store's *name* and operational region, from the endpoint that publishes one. |
| `store_hours` | Opening times, one row per store per day of the week. |

**Section boundaries are preserved rather than re-derived.** Issue #21 says the
policy corpus chunks by section and not by a fixed window, and a boundary thrown
away at harvest time cannot be recovered at index time. The rewards terms arrive
as one authored block and leave as nineteen sections, because the page marks each
heading as a paragraph that is entirely bold — which is also how the parser tells
a heading from the many bold *lead-ins* inside a section.

**The published point costs are read, not reconstructed.** The signed-in Rewards
Exchange is not public, and this source does not try to reach it; the rewards
landing page publishes the whole line-up with its prices in plain markup — 85
points for a side tortilla, 1,625 for an entrée — and that is what lands. Nothing
here turns a number of dollars into a number of points. The earn rate is published
as prose and stays prose, in `faq_entries` and `policy_sections`, where an answer
can quote it.

**A reward's picture is not a reward's item.** Half the tiles use marketing art,
and an "ENTRÉE" is not the burrito its photograph happens to show, so `rewards`
keeps the published `image_path` verbatim and derives no `item_id` from it.
Joining a reward to what it redeems for is issue #24's job, with the whole
catalogue in hand.

**The catering API has its own subscription key.** Not the one the www page
publishes in its `<meta>` tags — that key answers 401 there. It is read the same
way, from where the catering site hands it to its own front end: the
`VUE_APP_SUBSCRIPTION_KEY` in its script bundle, whose hashed filename is itself
read out of the catering page rather than remembered. Two documents to reach one
key, and no copied credential in this repository.

**Fifty stores, and which fifty is a decision.** They are chosen by round-robin
across the states in the published sitemap, so they are fifty states rather than
fifty branches of Los Angeles, and the reference restaurant of issue #19 is always
among them — checked by number, so that a locator page moving would stop the
harvest rather than attach the harvested prices to the wrong address. The parser
refuses to build a dataset with fewer than thirty stores, which is how issue #21's
criterion is checked rather than asserted. See
[`docs/decisions/store-selection.md`](../docs/decisions/store-selection.md) for what
that choice costs — chiefly that these stores are not near anybody in particular.

**A store's name and a store's address are two tables.** Every locator page in the
country calls its restaurant "Chipotle Mexican Grill"; the name that makes "the
Ballard store" mean something is published only by the restaurant endpoint. Two
documents, two rows, two `source_url`s — rather than one row whose provenance is
half of each.

**A day nobody published hours for is a row saying so.** `store_hours` holds seven
rows per store whether or not seven were published, with an `is_published` flag,
for the same reason `item_allergens` holds a row per allergen: a missing row reads
as "nothing to worry about", and "we publish nothing about Sunday" is a different
answer from "closed on Sunday".

The rewards, the terms, the catering prices and one store were compared against the
live pages by hand on 26 August 2026 — see
[`docs/chipotle-policy-spot-check.md`](../docs/chipotle-policy-spot-check.md), which is
also where the section boundary this dataset nearly lost is written down.

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
