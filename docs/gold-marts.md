# Gold marts

The four personalization marts of RFC-001 §04, what every number in them means,
and how `usual_order.confidence` is defined so that a low value produces an
honest hedge rather than a confident wrong answer. Issue
[#36](https://github.com/gganssle/chip_chat/issues/36).

Everything below is `infra/terraform/databricks_gold.tf`,
`databricks/notebooks/gold_marts.py`, `databricks/notebooks/gold_verify.py` and
`databricks/src/chip_chat/databricks/gold.py`. Nothing was made by hand in the
workspace UI.

> **Status.** Run. `chip-chat-gold-marts` completed against `dbw-chip-chat` on
> 2026-08-27 and `chip-chat-gold-verify` returned SUCCESS over all five of
> §7's criteria. §7.1 has the numbers it asserted on. `make ci` is green over
> the rest — including the confidence metric, which is *run* against the bounds
> `population.toml` admits a Regular and an Explorer on rather than described.

## 1. The shape

One Lakeflow Spark Declarative Pipeline, `chip-chat-gold-marts`, reading silver
and writing four tables into `chip_chat.gold_synthetic`.

```
chip_chat.silver_synthetic                   chip_chat.gold_synthetic
├── orders ────────────────┬── aggregate ──► ├── customer_360   one row per visitor
│                          │                 │
├── order_items ───────────┼── basket ─────► ├── usual_order    one row per visitor
│                          │                 │                 with a usual
│                          ├── co-order ───► ├── item_affinity  one row per ordered
│                          │                 │                 pair of items
│                          └── by month ───► └── spend_summary  one row per visitor
chip_chat.silver_harvested                                      per calendar month
└── menu_items ──── which item ids are entrees ──┘

                    demo_visitors ── NOT READ, and that is the mechanism (§3)
```

**This is the lane that earns Databricks its place in the architecture.**
Snowflake is the governed low-latency store the agent hits every turn;
Databricks is the batch engine that computes overnight what would be far too
slow to compute mid-conversation. `item_affinity` is a self-join over every
order in the population, and nobody is waiting on it inside a chat turn.

**Gold reads silver and never bronze**, for the reason silver reads bronze and
never the landing zone. A mart computed from what arrived rather than from what
is true would be a mart that quietly disagrees with the layer a human reads.

**A third pipeline, not a third section of the second.** The same argument
`databricks_silver.tf` makes, one layer up: a pipeline named for one layer would
own two, and re-deriving a mart — which is the thing that changes here, because
a confidence definition or an affinity threshold is a product decision — would
mean re-conforming the corpus first. The cost is one extra single-node cluster
start on a manual trigger, and `continuous = false` closes the always-on trap
exactly as it does in the two pipelines above.

**The SQL is in the module, not the notebook**, and that is the one place this
layer's shape differs from silver's. Silver's tables are all built the same four
ways, so a declaration plus a loop was the whole pipeline. Each of these four is
a genuinely different aggregation, so what `gold.py` declares is the query
itself, with every table name and every threshold left as a placeholder
`gold.query` fills. Two things follow:

1. A threshold cannot drift from the SQL that applies it, because there is
   exactly one of each and it is a constant with a docstring.
2. `gold_verify.py` can run **the same query the pipeline ran**, against the
   same silver input, and compare. That is how the fifth acceptance criterion —
   *marts rebuild deterministically from the same silver input* — becomes an
   assertion instead of a hope. It is the whole reason for the indirection.

## 2. The schema, matched exactly

RFC-001 §04 fixes these columns and the issue's brief is that they are matched
exactly, because the agent's read tools query them by name. §04 is transcribed
into `gold.RFC_COLUMNS` as data, and `test_gold.py` holds the declarations to
it — so a rename that a reviewer would have to notice fails `make ci` instead.

| Mart | Columns | One row is |
| --- | --- | --- |
| `customer_360` | `demo_id, order_count, lifetime_spend, last_order_at, favourite_store, cadence_days, lapsed_flag` | a visitor who has ordered |
| `usual_order` | `demo_id, item_id, modifiers[], confidence, derived_at` | a visitor with a usual |
| `item_affinity` | `item_id, related_item_id, lift` | an ordered pair of items |
| `spend_summary` | `demo_id, period, total, order_count` | a visitor, a calendar month |

**`derived_at` is the one addition**, and only to the three marts §04 did not
already name it on. RFC-001 §10 requires a failed nightly job to serve stale
marts *with their timestamp*, never silently as fresh — and a mart with nowhere
to put one cannot be served stale honestly. It is a required column, which makes
it a fatal expectation, which is what turns the issue's fourth criterion
("`derived_at` populated on every row") into something enforced rather than
intended. There is no fifth column on any mart, and `test_gold.py` asserts that
too, so that adding one is a decision somebody makes on purpose.

**`item_affinity` carries no `demo_id`** and it is the only one that does not.
It is a fact about the population rather than about a visitor, so
[#43](https://github.com/gganssle/chip_chat/issues/43) has nothing to attach a
row access policy to — which is the honest way for a table to say it needs none.
The test asserts the biconditional: every other mart carries `demo_id`, and this
one does not.

## 3. What a mart is allowed to read

Three tables. `silver_synthetic.orders`, `silver_synthetic.order_items`, and
`silver_harvested.menu_items` for one column — which item ids are entrees.

**Never `demo_visitors`.** RFC-001 §04 answers PRD Q2 — *may a visitor edit
their persona?* — by containment rather than by asking nicely: the three fields
a visitor may edit (`display_name`, `home_store_override`,
`stated_preferences`) are all columns of that table, no editable field is an
input to a mart, and so no edit can invalidate one. The RFC says a reviewer
checks the property by confirming nothing under the medallion pipeline selects
from it. `gold.FORBIDDEN_SOURCES` is that check, run by `test_gold.py` over
every rendered query and over both notebooks — and it also asserts that
`demo_visitors` is a table silver really has, because a guard against a name
nothing recognises guards nothing.

`customer_360.favourite_store` is derived from `orders.store_id` and may
therefore legitimately disagree with a visitor's `home_store_override`: an
override changes where the next order would be *priced*, not where past orders
*happened*. §04 is explicit that the serving layer says so rather than
reconciling silently, and this layer is not the serving layer.

**The ledger is not read either.** None of §04's four marts carries a points
balance, so none of them joins the thing that would produce one. Stored value
reaches a visitor through `persona_fixtures.points_balance` and issue #27's
reconciliation.

## 4. One set of orders, so that every count agrees

An order that was cancelled never happened; an order that was refunded had its
money returned. `gold.SETTLED_STATUSES` is one rule — `COMPLETED` — and it is
applied to **all four marts**, not to the money ones only.

That is deliberate, and the alternative is worse than it looks. Two columns both
called `order_count` that count different orders is exactly the sort of thing
that produces a confident wrong answer in conversation: *you have placed 47
orders*, and the monthly figures beside it sum to 45. Because there is one rule,
an identity holds for every visitor —

```
sum(spend_summary.order_count) = customer_360.order_count
sum(spend_summary.total)       = customer_360.lifetime_spend
```

— and `gold_verify.py` asserts it rather than hoping for it.

The statuses are a copy of `chip_chat.data_gen`'s `settled_statuses`, which is
the set that already earns loyalty points, and `test_gold.py` asserts the two
are equal. It also asserts that the *ignored* statuses are exactly
`{CANCELLED, REFUNDED}` — named rather than left as "everything else", so that a
generator growing a fourth status forces somebody to decide which of the two it
is.

**The consequence to know about.** `persona_fixtures` measures the same
customers over *all* their orders, because `chip_chat.data_gen.fixtures` selects
its exemplars long before any of this exists. About four orders in a hundred are
cancelled or refunded, so a fixture's `usual_share` and this layer's
`confidence` are computed over slightly different denominators. They are already
two different definitions on purpose (§5), and this is one more reason not to
read them as one number measured twice.

## 5. `usual_order.confidence`

The issue is blunt that this field carries the most weight. The Explorer persona
genuinely does not have a usual order, and the product requirement is that
Cilantro can state a visitor's usual **and briefly how it worked that out** —
which means low confidence has to produce an honest hedge rather than a
confident wrong answer.

### The definition

> **`confidence` is the lower bound of the 95% Wilson score interval for the
> proportion of a visitor's settled orders that are exactly their commonest
> basket.**

In words: *the share we could still defend if this customer's history were an
unlucky sample.*

```
                 p + z²/2n − z·√( p(1−p)/n + z²/4n² )
confidence  =    ───────────────────────────────────       p = repeats/n, z = 1.96
                            1 + z²/n
```

It is **not** the raw share, and the gap is the entire point. Two orders out of
three is a share of 67% and evidence of nothing; forty out of sixty is the same
share and a habit. A metric that cannot tell those apart is one that will state
an Explorer's accident as their usual.

`chip_chat.data_gen.records.PersonaFixture.usual_share` is the raw share, and
its docstring already says it is *deliberately* not called confidence, because
"the `usual_order` gold mart computes a confidence its own way". This is that
way.

The metric exists twice — `gold.confidence` in Python and
`gold.confidence_expression` in SQL — because only one of them can run in CI.
`test_gold.py` translates the SQL to Python arithmetic and runs the two against
each other over a grid, which proves the halves are the same arithmetic. Spark's
own reading of the SQL is `gold_verify.py`'s job, and neither pretends to be the
other.

### What a value means, in words

The issue asks for the value to be documented in words, so the words are data:
`gold.CONFIDENCE_BANDS`, which the serving layer renders and `test_gold.py`
asserts are non-empty.

| Band | Range | What it means | What the assistant may say |
| --- | --- | --- | --- |
| `stated` | ≥ 0.60 | The same basket, often enough and over enough orders that the pattern would survive an unlucky sample. | Name it plainly, and say how it was worked out. |
| `hedged` | 0.25 – 0.60 | There is a favourite, but it is varied often enough that stating it flatly would overclaim. | Offer it — *"you often go for"* — and keep the alternative easy to reach. |
| `no_usual` | < 0.25 | No usual order. The commonest basket is the commonest of many rather than a habit. | *"I am not sure what your usual is."* The row is still worth having: what is in it is what they had last. |

### Why the boundary is where it is

The bands are not chosen to look reasonable. They are chosen against the
population's own admission bounds, in `population.toml`:

| | Admitted at | Worst case for the band | Lands in |
| --- | --- | --- | --- |
| Regular fixture | `usual_share ≥ 0.85` over `order_count ≥ 30` | 0.7032 | `stated`, floor 0.60 |
| Explorer fixture | `usual_share ≤ 0.15` | < 0.15 at any *n* | `no_usual`, ceiling 0.25 |

The Regular clears the floor with room *at their worst permitted case*, and the
Explorer cannot reach even the middle band at any sample size — because a Wilson
lower bound is never above the share it is computed from, which `test_gold.py`
asserts over a grid rather than argues. `gold.CALIBRATION` names those two
archetypes and where each must land; the unit test proves the arithmetic puts
them there, and `gold_verify.py` proves the *published mart* does. A retune of
the generator that broke the separation fails `make ci` rather than surfacing as
a wrong answer in a demo.

The other five archetypes are deliberately not calibrated. They land wherever
their own histories put them, which is the point of a measured confidence rather
than a lookup table: an Office Manager who happens to order the same thing every
Wednesday should read as a Regular does, because they behave like one.

### What a row is, and when there is none

The usual order is the **commonest complete basket** — the mart is called
`usual_order`, not `usual_item` — and `item_id` and `modifiers` name the entree
of that basket, lowest item id first where a basket carries several. That is the
same definition `chip_chat.data_gen.fixtures` uses, which is what makes §7's
comparison between them meaningful.

A visitor whose settled orders carry no entree at all gets **no row**. "Your
usual is a bag of chips" is a worse answer than the honest absence, and the
serving layer already handles an empty result. A visitor who *has* a usual but a
weak one does get a row, with a low confidence — because the hedge path needs
something to hedge about.

## 6. The other three definitions

**`customer_360.cadence_days`** is the mean gap between consecutive settled
orders: `(last − first) / (order_count − 1)`. Null for a visitor with one order,
because one order is not a cadence and zero would read as *every day*.

**`customer_360.lapsed_flag`** is more than `gold.LAPSED_AFTER_DAYS` (90) of
silence — a copy of `texture.lapsed_after_days`, asserted equal in the tests,
because a mart that called somebody lapsed at sixty would disagree with the
archetype the population put them in.

**Measured against what, though.** `current_timestamp()` is the wrong clock: it
would make every customer lapse further every day, so a mart rebuilt from
unchanged silver would not equal the mart it replaced, and §7's fifth criterion
would be false by construction. So the instant is read out of the data —
`gold.AS_OF`, the latest settled order in the population. That is the same call
`chip_chat.data_gen` makes when it measures `days_since_order` against its
window's fixed end rather than against the wall clock. **`derived_at` is the
only wall-clock value in this layer**, and `test_gold.py` asserts that each
query calls `current_timestamp()` exactly once.

**`item_affinity.lift`** is `P(both) / (P(a) · P(b))` over settled orders: one
means independent, above one means they go together, below one means they are
ordered *instead of* each other. It is symmetric, and both directions of a pair
are published because the read tool queries by `item_id`.

Two details are load-bearing. It is computed as **four integers and one
division** — `both · N / (a · b)` — rather than three float ratios multiplied,
because a float product's last digits depend on the order Spark evaluated it in
and this layer has to reproduce its rows. And a pair needs
`gold.MINIMUM_CO_ORDERS` (25) co-orders to appear at all: a lift computed from
three co-occurrences is noise with a confident face, and #37's recommender
trains on this table. There is no support column to filter on downstream —
§04 gives this mart exactly three columns — so the threshold is applied here,
where it can be read, and `gold_verify.py` prints how many pairs it kept, so
that "we dropped the rare ones" is a number rather than a claim.

**`spend_summary.period`** is a calendar month in UTC, `YYYY-MM`. Months rather
than weeks because the question a visitor asks is *how much did I spend last
month*, and a week has no name a person recognises. A month with no settled
order has no row.

## 7. Checking it, rather than believing it

`make ci` runs everything that does not need a cluster, and for this layer that
is more than usual, because the confidence metric is an algorithm rather than a
threshold. `test_gold.py` runs it against the population's own bounds, checks it
against the SQL, and holds the four queries to the properties that make a mart
trustworthy: exactly one wall clock each, no tie broken on arrival order (no
`first()`, no `any_value()` — every pick is a `max()` over a struct whose lower
fields are the identity of the thing chosen), every threshold reaching the SQL
it governs, and nothing anywhere naming `demo_visitors`.

What is left is the live system, and #36's five criteria are claims about one.
`chip-chat-gold-verify` is those claims, as assertions:

| # | Criterion | How it is checked |
| --- | --- | --- |
| 1 | All four marts built, matching the schema exactly | Each mart exists, holds rows, and publishes exactly the declared columns in order with the declared types |
| 2 | A known customer's usual order comes back right | Joined to `persona_fixtures` — measured independently, in Python, from the same history — and required to name the same entree, built the same way |
| 3 | `confidence` calibrated, boundary documented | Every Regular fixture in `gold.STATED`, every Explorer fixture in `gold.NO_USUAL`, with the band table printed and the distribution beside it |
| 4 | `derived_at` populated on every row | It is a required column, so it is a fatal expectation, re-run here as a filter that must match nothing |
| 5 | Marts rebuild deterministically | The pipeline's own query re-run twice and compared to the published mart on every column but `derived_at` |

Criterion 2 is the strongest check in the notebook, because the answer it
compares against was not computed here. `persona_fixtures` is issue #26's table:
`chip_chat.data_gen.fixtures` measured each exemplar in Python, from the same
eighteen months of history, and has never seen this SQL. Two derivations that
never met have to name the same entree.

Criterion 5 runs the query **twice** on purpose, because two failures are
different findings. A rebuild that disagrees with the *published* mart means
silver moved under it or the query changed. A rebuild that disagrees with
*itself* means the query is not a function of its input at all — a tie broken on
arrival order, or a float sum that depends on partitioning.

Run them in order, after the silver pipeline:

```bash
databricks pipelines start-update $(terraform output -raw databricks_gold_pipeline_id)
databricks jobs run-now $(terraform output -raw databricks_gold_verify_job_id)
```

The verify job is read-only and safe to run at any time. It exits with a
machine-readable verdict, so the numbers it asserted on are quotable without
opening the workspace.

### 7.1 What it returned

Run 2026-08-27 against `dbw-chip-chat`, over the silver layer #34 built.

| Mart | Rows | Rows on the rebuild |
| --- | --- | --- |
| `customer_360` | 500 | 500 |
| `usual_order` | 500 | 500 |
| `item_affinity` | 12 | 12 |
| `spend_summary` | 7,176 | 7,176 |

Two of those columns being the same column is criterion 5, and it is worth
being explicit that the check is not the count. The job re-runs the pipeline's
own query and compares it to the published mart **on every column but
`derived_at`**, then re-runs it a second time and compares the two rebuilds to
each other; the counts above are what it reports, not what it asserts on. Both
comparisons were empty. §6's argument for reading the as-of instant out of the
data rather than off the wall clock is what makes that possible, and it is the
one design decision here that would have been invisible until this run.

Five hundred visitors and five hundred usual orders is not a coincidence and is
worth noticing: every visitor in the population has at least one settled order
carrying an entree, so the "no row" case §5 argues for did not arise. It is
still the right behaviour — the Explorer's honest absence is a *low confidence*,
not a missing row, and the missing-row case is a visitor whose whole history is
chips and a drink — but nothing in this population exercises it, so that path is
tested and not yet observed.

`item_affinity` at twelve pairs is the `MINIMUM_CO_ORDERS` threshold doing its
job against a ten-item catalogue: ninety ordered pairs are possible, twelve
clear twenty-five co-orders, and the rest are the noise §6 declines to publish.
The count is printed by the job for exactly this reason — "we dropped the rare
ones" is a number rather than a claim.

Criteria 2 and 3 assert rather than report and so have no row in the table
above. The usual order the mart computes matched `persona_fixtures` — #26's
independent measurement, in Python, over the same eighteen months, which has
never seen this SQL — for every exemplar. Every Regular landed in `stated` and
every Explorer in `no_usual`, which is the calibration in §5 holding against a
real population rather than against the bounds `population.toml` admits.

## 8. What this does not do

- **Nothing exercises the no-row case.** §7.1: every visitor in this population
  has a usual order to compute, so the honest absence §5 argues for is tested
  and not yet observed. The hedge path *is* observed — that is what a low
  confidence is — but "this visitor has no entree in their history" is not.
- **No publish to Snowflake.**
  [#39](https://github.com/gganssle/chip_chat/issues/39) owns the nightly
  hand-off. This pipeline's job ends when the mart is correct in Unity Catalog;
  §10's stale-serving behaviour is implemented where the serving happens, and
  `derived_at` is what it reads.
- **No recommender.** [#37](https://github.com/gganssle/chip_chat/issues/37)
  trains and registers the item-affinity model in MLflow — see
  [recommender.md](recommender.md). `item_affinity` is its reference rather than
  its input in the end: the model's full-history refit has to *reproduce* this
  mart, which is how #37's "produce the mart from the model" was satisfied
  without moving a table out from under §7's fifth criterion. The support
  threshold in §6 is still set with that model in mind, and the two are asserted
  equal. What #37 publishes is a fifth table, `recommendations`, because
  RFC-001 §06 returns ranked items *with rationale* and `item_affinity` has
  three columns and no `demo_id`.
- **No row access policies.**
  [#43](https://github.com/gganssle/chip_chat/issues/43) applies them. Every
  visitor-scoped mart here carries `demo_id` so that it can.
- **No points balance.** §04 gives none of these four marts a column for one,
  and this layer does not invent columns.
- **No schedule.** [#38](https://github.com/gganssle/chip_chat/issues/38) argues
  the weekly re-harvest. Nothing in this workspace should be able to start
  spending on its own.
