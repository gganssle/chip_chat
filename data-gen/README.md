# data-gen

The seeded synthetic account generator: five hundred customers, thirty stores,
eighteen months of orders composed only of real catalogue items.

This is the fake half of RFC-001 §04's two data planes, and the boundary is the
whole point. **The menu is real and the accounts are fake, and the pipeline keeps
them apart on purpose.** Everything Cilantro says about food comes from what
Chipotle publishes; everything it says about "you" comes from a customer minted
here.

Issue #25 calls this "the highest-leverage phase and the one everyone rushes", and
PRD §09 says why: account realism is bounded by the generator, so behaviour the
population does not exhibit cannot be demonstrated, however good the rest of the
pipeline is. That makes the archetypes below a product decision rather than a
technical one, which is why they are a config file and not a module.

## Generating one

```bash
# The harvest first. The generator composes orders from the catalogue and earns
# points at Chipotle's published rate, and it fetches nothing itself.
python -m chip_chat.harvest.sources.chipotle --landing landing --dataset all
python -m chip_chat.catalog --landing landing --offline

# Then the population.
python -m chip_chat.data_gen --landing landing
```

Run it twice and compare the `population_version` it prints. That is issue #25's
first acceptance criterion checked rather than asserted — same seed, same
population, byte for byte.

```python
from chip_chat.catalog import load_catalog
from chip_chat.data_gen import generate_population, load_config, load_rewards_terms

catalog, terms = load_catalog(blobs), load_rewards_terms(blobs)
population = generate_population(catalog, terms, load_config())
population.write(blobs)
```

Two real inputs, one tuned one. The catalogue says what may be ordered; the
published rewards terms say what an order earns and what a reward costs; the config
says how five hundred people behave.

`--seed N` gives you *a* population instead of *the* one, without editing anything.
`--config path` replaces the parameters wholesale, which is how the population is
retuned.

## Five tables come out

| Table | Rows | What it is |
| --- | --- | --- |
| `personas` | 7 | The archetypes, from the config. |
| `demo_visitors` | 500 | The synthetic customers. A public visitor is assigned one. |
| `orders` | ~19k | One order, at one store, at one instant, for one published total. |
| `order_items` | ~49k | Its lines: a real item, built from real modifiers. |
| `loyalty_ledger` | ~26k | Points earned on settled orders at Chipotle's published rate, and spent on real published rewards. |

Row counts are for the shipped config against the committed fixture catalogue; a
real catalogue has more food in it and the same number of customers.

`docs/decisions/synthetic-population.md` argues the seven columns these carry that
RFC-001 §04 does not list, and why `personas` is a table of archetypes rather than
of customers.

## The three properties everything else serves

**Same seed, same population, byte for byte.** No stream is shared. Every random
draw comes from `rng.substream`, addressed by the seed and by what is being drawn
for — `("customer", "demo-0214", "orders")` — so a customer's history is a pure
function of the seed and their identifier, not of how many customers came before
them. That is what makes the config retunable: change `toppings_max` and the
population changes because the parameter changed, not because everyone's stream
slid along by one. The wall clock appears nowhere; the window ends at a fixed
instant in the config.

**Real food only.** Every `item_id` and every `modifier_id` written into
`order_items` was read off a catalogue row handed over by `catalogue.OrderableMenu`,
which is the only source of an identifier in the package. There is no code path
that could produce a name Chipotle does not publish. `test_referential_integrity.py`
asserts it over the whole population anyway, including that a modifier is a real
modifier *of the item it is on* — a real burrito with an invented salsa is the same
failure wearing a smaller hat.

**Real points only.** The ledger runs Chipotle's published arithmetic, not this
project's. `rewards.load_rewards_terms` reads four rules off the policy harvest —
ten points per dollar, the Rewards Exchange and its point costs, expiry after 365
days of inactivity, three qualifying purchases a day — and raises rather than
supplying a default for any of them, including when two published pages disagree.
There is no earn rate in `population.toml` and no flag for one; a config that still
carries `points_per_dollar` is refused. Every accrual names the order it came from
and every redemption names the published reward it was spent on, so the whole ledger
reconciles by joining rather than by regenerating.

The ledger is **append-only and the balance is derived**: there is no balance column
anywhere in this package, and `test_ledger_population.py` asserts over all five
hundred customers that the sum of a customer's entries is their balance, that it is
never negative at any point in their history, that every accrual is worth exactly
`floor(total × 10)` of a settled order of theirs, and that the set of rewards
redeemed is exactly the harvested Rewards Exchange.

| Archetype | Redeems at | Ends up with |
| --- | --- | --- |
| The Lapsed Regular | 0.04 | ~8,900 points, unredeemed — 59 of 60 above the price of the most expensive published reward. |
| The Office Manager | 0.18 | Tens of thousands: a floor's lunch on the company card, and nobody thinking about the points. |
| The Weekly Regular | 0.55 | A few hundred. They spend what they earn. |

That spread is why `redemption_probability` is on the archetype rather than on the
programme: issue #27 asks that the Lapsed Regular carry a balance "worth surfacing
unprompted", and one population-wide rate cannot describe both them and the regular.

## The archetypes

Seven, in `population.toml`. Adding "the customer who only ever orders catering" is
an edit to that file.

| Archetype | Share | What they demonstrate |
| --- | --- | --- |
| The Weekly Regular | 16% | The same bowl, the same store, the same day nearly every week. Which day is drawn *per customer* — pinning eighty of them to Tuesday would put a third of the population's orders on Tuesday, which reads as a generator rather than as a regular. |
| The Lapsed Regular | 12% | Ordered steadily, then stopped about four months ago. |
| The Explorer | 18% | Rarely the same thing twice, rarely the same store. |
| The Office Manager | 8% | A midweek lunch order for the whole floor, on delivery. |
| The Weekend Family | 16% | Three or four entrees and chips for the table, most weekends. |
| The Newcomer | 10% | First order about five months ago; still working out the usual. |
| The Occasional Visitor | 20% | Every couple of months, usually somewhere new. |

Shares are apportioned by largest remainder, so eighteen per cent of five hundred is
ninety customers every run and not ninety-one on a seed that rolled well.

Two customers of one archetype are still two people. Each is minted a **palate** — a
Dirichlet draw over everything orderable, concentration in the config — so one is the
guacamole one and another never orders a drink. Their *usual* falls out of the same
draw, which is why the regular's usual is theirs and not their archetype's.

## Retuning it

Every number is in `src/chip_chat/data_gen/population.toml` and nowhere else, which
is issue #25's fourth acceptance criterion. The reader validates all of them and
**refuses** rather than clamping: shares that do not sum to one, a cadence of an
hour, a timezone that does not exist. A file whose numbers are silently corrected is
no longer the file that was tuned.

The knobs worth knowing about:

- `[population]` — size, seed, the fixed end of the window, how unevenly traffic
  spreads over stores, how sharply a customer prefers some items over others.
- `[timing]` — the shape of a week, a day and a year. `month_weights` is the seasonal
  drift, applied as a change to how often people order rather than as a multiplier
  bolted onto a count. `store_timezones` maps a published state abbreviation to an
  IANA zone, because the locator publishes opening hours without one.
- `[catalogue]` — which published category names mean entree, side, drink and
  not-food. A catalogue whose category names change is a config edit.
- `[orders]` — the statuses an order may end in, and which of them settle.
- `[loyalty]` — **no arithmetic.** What the `reason` column calls each movement, and
  how a customer chooses among the rewards they can afford. The earn rate, the expiry
  window, the daily earning cap and every reward's price are read from Chipotle's
  published terms by `chip_chat.data_gen.rewards`; a config still carrying
  `points_per_dollar` or `redemption_threshold` is refused rather than ignored.
- `[[personas]]` — the archetypes, including `redemption_probability`: how readily a
  customer of this kind spends. Per archetype, because the Lapsed Regular's
  accumulated balance is the point of them.

## What this package does not do

- **Prove the population is not thin.** That is issue #28, and it needs a real
  harvest: the committed fixture catalogue has two entrees in it, so variety of
  *food* is not assertable here. Variety of *behaviour* is, and `test_texture.py`
  asserts it.
- **Write to ADLS.** `write()` takes a `BlobStore`, the same interface the harvest
  and the catalogue land through; an ADLS Gen2 implementation is `cc-b15`.
- **Read the population back.** Deliberately. Issue #33 ingests these tables with
  Auto Loader, and a test that wants them calls the generator again — which is free,
  because the same seed produces the same population. A loader would be a second
  definition of the schema to keep true.
