# Spot check: the policy, catering and store harvest against the live site

**Issue:** [#21](https://github.com/gganssle/chip_chat/issues/21) (bead `cc-6o8`) · **Checked:** 26 August 2026
**Checked by hand against:** `https://www.chipotle.com/rewards`,
`https://www.chipotle.com/rewards-terms`, `https://catering.chipotle.com/`,
`https://locations.chipotle.com/ca/lakewood/5310-lakewood-blvd` and
`https://www.chipotle.com/contact-us`, all five opened in a browser and read as a
customer sees them.

This is the same kind of document as
[`chipotle-nutrition-spot-check.md`](chipotle-nutrition-spot-check.md), and it exists for
the same reason: the unit tests run against fixtures, so a green suite proves the parser
is self-consistent, not that it agrees with what Chipotle is publishing this afternoon.
Only this does.

The comparison is against `landing/parsed/chipotle/policy/` from a harvest run the same
morning — `harvested_at` on those rows is `2026-08-26T04:5x`.

---

## The rewards and their point costs — 8 of 8 matching

The acceptance criterion this dataset was least certain of. The signed-in Rewards
Exchange is not public; the rewards landing page publishes the whole line-up anyway, and
this is the check that it publishes the *same* line-up.

| Rendered on `/rewards` | Harvested `name` | Rendered points | Harvested `point_cost` |
| --- | --- | ---: | ---: |
| SIDE TORTILLA | `SIDE TORTILLA` | 85 | 85 |
| CHIPS | `Chips` | 350 | 350 |
| FOUNTAIN DRINK | `Fountain Drink` | 400 | 400 |
| GUAC | `Guac` | 500 | 500 |
| DOUBLE PROTEIN | `DOUBLE PROTEIN` | 700 | 700 |
| 50% OFF AN ENTRÉE | `50% OFF AN ENTRÉE` | 815 | 815 |
| ENTRÉE | `ENTRÉE` | 1625 | 1625 |
| ENTRÉE AND CHIPS | `ENTRÉE AND CHIPS` | 1775 | 1775 |

Eight rewards, eight matches, no ninth on either side.

**The names differ in case, and that is on purpose.** The page renders every tile in
capitals; three of the eight are written in mixed case in the markup underneath. The
harvest keeps what was written rather than what CSS displayed, because upper-casing is
reversible in only one direction and the source spelling is the one a citation can be
checked against.

## The rewards terms — 19 sections, in order

The page's own section headings, read top to bottom in the browser, against
`policy_sections` for `document_id = rewards-terms`. All nineteen present, in the
published order, with `(LAST UPDATED APRIL 13, 2026)` as the section before the first
heading:

`PLEASE READ THESE TERMS AND CONDITIONS CAREFULLY…` · `GENERAL APPLICATION OF TERMS;
PRIVACY` · `ELIGIBILITY` · `HOW CHIPOTLE REWARDS WORKS` · `HOW TO ENROLL` ·
`ACCUMULATING POINTS` · `REWARDS` · `TRACKING YOUR ACCOUNT; POINTS EXPIRATION` ·
`SPECIAL OFFERS` · `LIMITATIONS, RESTRICTIONS, AND OTHER TERMS` · `CHIPOTLE EMPLOYEE
PARTICIPATION` · `CHANGES TO CHIPOTLE REWARDS` · `DISPUTE RESOLUTION – (…)` ·
`ARBITRATION AGREEMENT` · `· CLASS ACTION WAIVER AND JURY TRIAL WAIVER` · `GOVERNING LAW
AND VENUE` · `LIMITATIONS ON LIABILITY/INCONTESTABILITY` · `SEVERABILITY AND SURVIVAL`

Three facts a visitor might actually ask about were read in full and compared word for
word:

| Question | Section it is in | Agreed |
| --- | --- | --- |
| When do points expire? | `TRACKING YOUR ACCOUNT; POINTS EXPIRATION` — "after 365 days of account inactivity" | yes |
| How many purchases a day earn points? | `ACCUMULATING POINTS` — "limited to three qualifying purchases per day" | yes |
| How long does a redeemed Reward last? | `REWARDS` — "expire if not used within 60 days" | yes |

**One boundary was nearly lost, and the check is what found it.** The page prefixes
`CLASS ACTION WAIVER AND JURY TRIAL WAIVER` with a middle dot and six non-breaking
spaces, outside the bold run that makes it a heading. The first version of the parser
required the *whole* paragraph to be bold and therefore folded that section into the
17,000-character one above it. Reading the rendered page beside the harvested list is
the only thing that would have caught it: both were internally consistent.

## Catering — every published price matching

Read off `catering.chipotle.com`, which renders prices its API also returns; the two
agree, which is worth confirming rather than assuming, because the page is a different
codebase from the endpoint.

| Rendered | Harvested `package_id` | Rendered price | Harvested `min_price` | Rendered minimum | Harvested `min_quantity` |
| --- | --- | ---: | ---: | ---: | ---: |
| BUILD YOUR OWN · SINGLE · "BEST VALUE" | `CMG-4105` | $8.75 /person | 8.75 | 10 people | 10 |
| BUILD YOUR OWN · DOUBLE · "FAN FAVORITE" | `CMG-4206` | $12.00 /person | 12.00 | 10 people | 10 |
| BUILD YOUR OWN · TRIPLE · "MOST VARIETY" | `CMG-4306` | $13.50 /person | 13.50 | 10 people | 10 |
| BURRITOS BY THE BOX | `CMG-4012` | $8.75 | 8.75 | 6 people | 6 |
| CHIPS & DIPS | `CMG-4027` | $40.00 | 40.00 | — | 1 |

`CMG-4027`'s "Serves 10-15 each" is rendered on the page and lands verbatim in the
`serves` column; the page states no minimum for it, and the endpoint's `1` is what the
row carries.

The tier subtitles and taglines match too: `display_sub_name` is `Single`, `Double`,
`Triple` and `tagline` is `Best Value`, `Fan Favorite`, `Most Variety`.

The page also states what a tier contains: *"The Single comes with 1 protein, 2 bases,
2 toppings, 2 salsas, and 1 type of tortilla."* `CMG-4105` carries exactly that —
`protein_count` 1, `base_count` 2, `topping_count` 2, `salsa_count` 2,
`tortilla_count` 1.

## One store, end to end

`locations.chipotle.com/ca/lakewood/5310-lakewood-blvd` — the reference restaurant of
[`decisions/menu-pricing.md`](decisions/menu-pricing.md), and therefore the one row where
issue #19's prices and issue #21's address have to meet.

| Rendered on the page | Harvested |
| --- | --- |
| CHIPOTLE MEXICAN GRILL | `store_profiles.name` = `Lakewood Mall` — see below |
| 5310 Lakewood Blvd Lakewood, CA 90712 | `5310 Lakewood Blvd` / `Lakewood` / `CA` / `90712` |
| (562) 790-8786 | `+15627908786` |
| RESTAURANT HOURS · Monday - Sunday · 10:45 AM - 11:00 PM | seven rows, each `10:45`–`23:00`, all `is_published` |
| order links to `?restaurant=679` | `store_id` = 679, matching `item_prices.restaurant_id` |

**The name is the interesting row.** The page calls it "Chipotle Mexican Grill", as every
one of the four thousand locator pages does. `Lakewood Mall` comes from
`restaurant/v3/restaurant/679`, which is why it is in `store_profiles` with its own
`source_url` rather than merged into `stores` — see
[`decisions/store-selection.md`](decisions/store-selection.md).

## The FAQ — the structure, and two answers word for word

`faq_entries` holds 136 questions across 40 category/subcategory sections. The accordion
on `/contact-us` renders them from the same endpoint, so the check here is against the
*rendered* page: whether the two-level structure the harvest kept is the one a visitor
navigates.

The sidebar lists ten categories in the harvested `category_position` order — Rewards
Program, Delivery, Catering, Company Info, Food, Gift Cards & Coupons, Ordering,
Restaurants, Build Your Own Chipotle, Chipotle Careers — plus "Top Questions" and "All
Questions", which are views rather than categories and are not in the data.

Two categories were opened and their subcategory headings and counts compared:

| Category | Rendered subcategories, in order, with question counts | Harvested |
| --- | --- | --- |
| Delivery | Catering (0) · Fees (1) · Delivery - General (5) · International (1) · Delivery - My Order (2) · Online Ordering (1) · Payment (1) · Partners (1) | identical |
| Ordering | Ordering (4) · Catering (1) · General (8) | identical |

The empty `Catering` heading under Delivery is published as an empty subcategory and lands
as one; it is a row with `entry_count` zero rather than a row that is not there.

Then the two answers issue #21 names, expanded in the browser and compared word for word:

- **"Who do I contact regarding delivery payment issues, including refund requests?"** —
  `Delivery / Payment`. Two paragraphs, matching exactly, and the newline between them is
  preserved. The words "chipotle.com" are a link on the page; the harvested `links` column
  carries `https://www.chipotle.com/`, which the answer text alone would have lost.
- **"Can I cancel my online or mobile order after I've submitted it?"** — `Ordering /
  General`, `is_top_question` true, which is how the page marks it. Harvested answer
  *"When you submit an order, it's sent directly to our restaurant crew, so we're unable
  to cancel."*, identical to the rendered one.

The earn rate appears in three published places and all three agree: the rewards page
hero ("10 points closer"), its own FAQ accordion ("10 points per $1 spent"), and the FAQ
endpoint's `Okay, so how does Chipotle Rewards work?`. **Nothing in the dataset turns
that sentence into a number**; it stays prose in `faq_entries` and `policy_sections`, for
an answer to quote.

## What was not checked

- **Forty-nine of the fifty stores.** Only restaurant 679 was compared page-by-page.
  The other forty-nine were read by the same parser from the same markup, and the parser
  raises rather than guessing on anything it does not recognise — but that is an argument,
  not a check.
- **The catering component lists**, beyond the Single's counts. `catering_package_options`
  has 93 rows; five were confirmed by reading the "POPULAR BUILDS" descriptions on the
  catering page, which name proteins, bases and salsas that all appear in the harvested
  slots.
- **The 134 FAQ answers** that were not read in full, and the eight categories whose
  subcategory headings were not compared.
