# The Snowflake schema

Seventeen tables, two views, four write procedures, and the column the whole
isolation argument hangs on. Issue
[#42](https://github.com/gganssle/chip_chat/issues/42) built fourteen of the
tables and [#46](https://github.com/gganssle/chip_chat/issues/46) added the
other three along with the procedures — a write path that cannot be written
without somewhere to keep a published reward catalogue, a published earn rate,
and the knowledge that a retry key has been spent.

[docs/snowflake-account.md](snowflake-account.md) is the account this lands
into — the roles, the grants and the two warehouses, built before there was a
single row to protect. This is what went into it: the real catalogue, the
synthetic accounts, and the four marts Databricks publishes overnight.

RFC-001 §04 fixes the schema and the ticket says to match it exactly, so most
of the interesting decisions here are not about *what* the columns are. They
are about the ten columns §04 does not print and why each is unavoidable, about
`demo_id` being a schema requirement rather than a convention, and about two
choices that look like omissions until you price them.

## 1. The shape

```
CHIP_CHAT.CATALOGUE          real, harvested, cited
  menu_items        item_id · name · category · description · calories
                    allergens[] · allergen_disclosure · source_url · harvested_at
  item_prices       restaurant_id · item_id · unit_price · unit_delivery_price
                    is_available · source_url · harvested_at
  modifiers         modifier_id · item_id · modifier_item_id · name · delta_calories
  stores            store_id · name · city · region · hours[]
  rewards           reward_id · position · name · point_cost · image_path
                    source_url · harvested_at                          (#46)
  rewards_terms     rule · value · source_url · harvested_at           (#46)

CHIP_CHAT.ACCOUNTS           synthetic, visitor-scoped
  personas          persona_id · label · home_store · seed_points · narrative
  demo_visitors     demo_id · display_name · persona_id · thread_id
                    home_store_override · stated_preferences · created_at · last_seen
  persona_fixtures  demo_id · persona_id · label · rank · home_store · … · narrative
  orders            order_id · demo_id · store_id · placed_at · status · total
                    channel · priced_restaurant_id
  order_items       order_id · line_number · demo_id · item_id · qty
                    modifiers[] · unit_price · line_total
  loyalty_ledger    entry_id · demo_id · delta · reason · order_id
                    reward_name · created_at
  action_receipts   demo_id · retry_key · action · subject_id
                    receipt · created_at                               (#46)

CHIP_CHAT.MARTS              published nightly from Databricks (#39)
  customer_360      demo_id · order_count · lifetime_spend · last_order_at
                    favourite_store · cadence_days · lapsed_flag · derived_at
  usual_order       demo_id · item_id · modifiers[] · confidence · derived_at
  item_affinity     item_id · related_item_id · lift · derived_at
  spend_summary     demo_id · period · total · order_count · derived_at

CHIP_CHAT.ACCOUNTS           the audit, as two views
  visitor_scoped_tables    every table the demo_id rule applies to
  tables_missing_demo_id   the ones that break it. Must be empty

CHIP_CHAT.ACCOUNTS           the write path, as four procedures (#46)
  place_order · redeem_points · update_preferences   sql/12_procedures.sql
  cancel_order                                       sql/13_cancel_order.sql
  live_order_seq · live_ledger_seq   the ord-9000001 and loy-9000001 bands
```

Ten of the eighteen carry `demo_id`. `personas` and `item_affinity` are the
two tables in ACCOUNTS and MARTS that do not, and both are exempted by name in
`sql/09_audit.sql` with the reason beside the exemption. The six catalogue
tables are exempt wholesale: the catalogue is the real half and has no visitor
in it at all.

Three of the eighteen are not RFC-001 §04's and arrived with [#46], which could
not be written without them: `rewards` and `rewards_terms` in CATALOGUE, because
`redeem_points` validates a redemption against a published catalogue and
`place_order` accrues at a published rate, and `action_receipts` in ACCOUNTS,
because idempotency on a retry key needs somewhere durable to remember that a
key has been spent. Their `Table.rfc` is empty in `schema.py`, which is the loud
case rather than the quiet one: every single column of such a table has to carry
its own written argument, because none of them can point at §04.

[#46]: https://github.com/gganssle/chip_chat/issues/46

## 2. `demo_id` is a schema requirement, and the audit is a query

RFC-001 §05 puts visitor isolation below the model: identity is bound to the
Snowflake session and enforced by row access policies, so no tool signature
accepts a visitor identifier and there is nothing for an injected instruction
to populate. [#43] writes those policies. This ticket's job is to make sure
there is somewhere to attach them.

A row access policy filters **one table** against a session variable. It cannot
follow a join. So a visitor-scoped table without its own `demo_id` is not a
table missing a convenience — it is a table no policy can be written for, and
the guarantee has a hole shaped exactly like it.

The criterion asks for that to be "asserted by a query that fails if one is
added without it", and the query form is right, because a `CREATE TABLE` is
something a person can type into Snowsight and a test over checked-in SQL will
never see it:

```sql
SELECT * FROM CHIP_CHAT.ACCOUNTS.tables_missing_demo_id;
```

Empty is a pass. **It defaults to deny.** A table appearing in ACCOUNTS or
MARTS is presumed visitor-scoped until somebody exempts it by name and writes
down why, so forgetting fails loudly. An allow-list of tables to check would
fail silently every time it was not updated, which is every time it matters.

`visitor_scoped_tables` is the other half and is what [#43] needs: every table
on that list must carry a policy, and one that appears there without one is the
same guarantee developing the same hole from the other direction.

Three things check this, and none of them replaces another:

| | asks | costs | catches |
| --- | --- | --- | --- |
| `test_schema_layout.py` | does the DDL declare it? | free, in `make ci` | the table somebody wrote and did not think about |
| `tables_missing_demo_id` | does the account have it? | a query | the table nobody wrote down at all |
| `verify`'s canary | does the audit still see anything? | a table created and dropped | an audit that has quietly stopped looking |

The third is the one worth arguing for. `verify` creates a table with no
`demo_id`, asks the audit again, and requires it to be named. Without that, an
audit that had stopped working — a view rewritten, a schema renamed, the query
run as a role that cannot see MARTS — would report a clean account forever.
That is the same failure mode as a security check that passes because it is
broken, which [docs/snowflake-account.md §7](snowflake-account.md) already has
one worked example of.

## 3. The ten columns RFC-001 §04 does not print

§04 is fixed and the ticket says to match it exactly, so an addition needs an
argument. Each of these is already produced by `chip_chat.data_gen` and
conformed by silver, so the publish is a copy rather than a cast nobody wrote
down — and a serving column the generator does not produce is a column that
arrives null forever.

| Column | Why a serving layer without it cannot work |
| --- | --- |
| `order_items.demo_id` | §04's own sentence requires it and §04's printed list omits it. [#43] names `order_items` among the tables that get a policy, and a policy cannot reach the visitor through a join. |
| `order_items.line_number` | §04 keys a line by `(order_id, item_id)`, which cannot hold two burritos built differently — and a group order that cannot is not a group order. |
| `order_items.unit_price`, `line_total` | Without them `orders.total` is a number nobody can audit. With them a reviewer re-derives it from `item_prices` and finds it. |
| `orders.channel` | Two published prices per item, counter and delivery. A total is unexplainable until the row says which list priced it. |
| `orders.priced_restaurant_id` | And thirty restaurants publish different numbers, so the row also says whose. |
| `loyalty_ledger.order_id` | Reconciling the ledger against the orders that earned it should be a join, not a regeneration. |
| `loyalty_ledger.reward_name` | [#27] requires every redemption to trace to a published reward, and a cost is not an identity: two rewards can be priced the same. |
| `modifiers.modifier_item_id` | The thing added is itself a priced item, so this is the join into `item_prices`. `modifier_id` already contains it, spelled `<item_id>:<this>` — without the column, pricing a modifier means splitting a string inside a generated query. |
| `menu_items.allergen_disclosure` | See below. |
| `derived_at` on three marts | RFC-001 §10 requires a stale mart to be served *with* its timestamp and never silently as fresh. `usual_order` already had one. |

`persona_fixtures` carries ten more, and §04 says of that table that "some
columns above are elided". The elided ones are the measurements the narrative
was written from, and
[docs/decisions/persona-fixtures.md](decisions/persona-fixtures.md) sets the
standard they are here to meet: a reviewer who doubts the sentence re-derives
it from the row rather than trusting it. A row carrying the sentence and none
of its evidence cannot be doubted usefully.

None of this is on trust. `chip_chat.snowflake.schema` holds §04's columns and
the additions as data, each addition with its reason, and
`test_schema_layout.py` asserts that a table's columns are **exactly** §04's
plus the declared additions. A column added without an argument fails
`make ci`; so does a §04 column that disappears.

### `allergen_disclosure`, and why an empty array is not an answer

`menu_items.allergens` is §04's, and on its own it is the boolean-shaped
mistake that
[docs/decisions/allergen-absence.md](decisions/allergen-absence.md) exists to
prevent. An empty array has two meanings — Chipotle publishes allergen data for
this item and marks none of it, or Chipotle publishes nothing about this item
at all — and only one of those is a statement somebody made. Silence read as
reassurance is the specific failure that decision is about, and *"does this
contain dairy?"* is going to be asked by strangers on the open internet.

So the column is here, `NOT NULL`, holding `PUBLISHED` or `NOT_PUBLISHED`.

What is deliberately **not** here is `item_allergens`, the dense three-valued
table where every item is crossed with every published allergen. Allergen
answers are composed in the knowledge lane, out of the harvested chart, with a
citation attached. A second copy in the serving layer would be a second source
of truth for the one question in this system where being confidently wrong is
worst. [#45] curates what Cortex Analyst can reach, and if an allergen question
should be answerable from here at all, that is the ticket to argue it in.

## 4. `CREATE OR ALTER TABLE`, and the two things it does not break

Every table is `CREATE OR ALTER TABLE`. The alternative, `CREATE TABLE IF NOT
EXISTS`, is silent about a table that already exists — so a reworded column
comment would land in git and never reach the account. That matters more here
than it looks: the comments are what [#45]'s semantic view retrieves against,
and a comment that has drifted still answers.

Two properties were checked against this account rather than assumed, because
both would be quiet failures.

**It keeps the rows.**

```
CREATE OR ALTER TABLE _T_TEST (a NUMBER(10,0) NOT NULL COMMENT 'first', b VARCHAR ...)
INSERT INTO _T_TEST VALUES (1,'x');
CREATE OR ALTER TABLE _T_TEST (a ... COMMENT 'first, reworded', b ..., c BOOLEAN ...)

SELECT * FROM _T_TEST;          →  A=1  B=x  C=None
COLUMN_NAME  COMMENT
A            first, reworded    ← the comment was re-asserted
C            third              ← the column was added
```

**A row access policy survives it.** This is the load-bearing one, because [#43]
attaches the policies that keep visitors apart and a routine apply that silently
detached one would be a breach nobody sees:

```
ALTER TABLE _T_TEST ADD ROW ACCESS POLICY _P_TEST ON (demo_id);
CREATE OR ALTER TABLE _T_TEST ( ... one more column ... );

SELECT policy_name FROM TABLE(INFORMATION_SCHEMA.POLICY_REFERENCES(...));
→ _P_TEST
```

**The sharp edge.** Removing a column from a definition *drops* it on the next
apply. That is a deliberate act by whoever edits the file rather than something
a re-run does on its own, but it is the one way an apply can destroy, and no
test can see it coming. `test_an_apply_never_destroys` now also fails on
`CREATE OR REPLACE TABLE`, which is a one-word edit away from `CREATE OR ALTER
TABLE`, reads almost identically in a diff, and would empty every table on the
next apply.

## 5. Constraints and clustering: one is declared unenforced, the other is refused

**Constraints.** Snowflake enforces `NOT NULL` and nothing else — `PRIMARY
KEY`, `UNIQUE` and `FOREIGN KEY` are metadata. They are declared anyway, and
not as decoration: [#45]'s semantic view reads them to know which joins exist,
and a text-to-SQL system that has to guess a join key guesses. So `NOT NULL`
carries every invariant that must actually hold — `demo_id` on all eight
visitor-scoped tables, every key column, `allergen_disclosure` — and the keys
carry the shape. None is declared `RELY`, which would let the optimizer
eliminate a join on the strength of a promise nothing checks.

**Clustering: none, and the arithmetic is the argument.** The ticket asks for
clustering chosen for the actual query patterns. The measurement, against this
account with the fixture catalogue and a sixty-customer population loaded:

```
SELECT SUM(bytes), SUM(row_count) FROM INFORMATION_SCHEMA.TABLES
→ 197,632 bytes    11,984 rows
```

A Snowflake micro-partition holds up to 16 MB compressed. The entire database
is a little over one percent of one partition; the largest table in it,
`order_items`, is 71 KB. The five-hundred-customer population is roughly ten
times that and still one partition. There is nothing to prune — a clustering
key would be a recurring serverless bill against a trial capped at $400 of
credits, buying a scan that was already one partition wide. Snowflake's own
guidance puts the payoff on tables in the multi-terabyte range.

The lookup this schema is actually shaped for — `WHERE demo_id = ?` — is
answered by [#43]'s row access policy filtering a table that fits in one
partition. If the demo ever grows to where this is wrong, the numbers above are
the ones to re-run.

## 6. Six things worth knowing before extending this

**1. `INFORMATION_SCHEMA` is filtered by the querying role, even through a
view.** The audit view is owned by `CHIP_CHAT_ADMIN`, and a lesser role still
gets a shorter answer out of it:

```
CHIP_CHAT_ADMIN  → SELECT * FROM visitor_scoped_tables → 8 rows
CHIP_CHAT_WRITE  → SELECT * FROM visitor_scoped_tables → 5 rows
                   (the three MARTS tables are invisible to it, correctly)
```

Five of eight is not a smaller pass, it is a check that did not look at three
tables. Run the audit as `CHIP_CHAT_ADMIN`; `verify` does.

**2. A bulk load does not fit on the serving warehouse, and that is the split
working.** `CHIP_CHAT_SERVING_WH` has a sixty-second statement timeout, because
a turn that has not answered in a minute has already failed as a conversation.
The first version of the loader ran there and had a `TRUNCATE` cancelled by it:

```
000630 (57014): Statement reached its statement or warehouse timeout of
60 second(s) and was canceled.
```

`chip_chat.snowflake.load` runs on `CHIP_CHAT_PUBLISH_WH`, whose timeout is an
hour, which is what that warehouse is for.

**3. The generator's `order_items` cannot fill the serving `order_items`.**
`chip_chat.data_gen` writes an order line without a `demo_id` — the generator
keys a line by its order — and the silver layer carries the visitor down onto
the line. The serving table requires it, so a load of the raw generator output
is refused by name before anything is uploaded:

```
landing/accounts/synthetic/order_items.jsonl cannot fill CHIP_CHAT.ACCOUNTS.order_items
it carries no demo_id, and the table requires them.
```

That refusal is the schema doing its job. Load the conformed tables.

**4. `COPY INTO … MATCH_BY_COLUMN_NAME` ignores columns the table does not
have, which is the whole reason the load works.** `CHIP_CHAT.CATALOGUE` is a
*projection* of silver rather than a copy — `menu_items` carries fifteen
columns in the lakehouse and nine here — and the extra six land nowhere without
complaint. The flip side is that a file with the wrong column *names* also
loads successfully and lands nothing, so `load` counts rows afterwards and
fails on a table that came out empty.

**5. Table and column names come back upper-cased.** Unquoted identifiers are
folded, so `orders` is `ORDERS` in `INFORMATION_SCHEMA` and in every result
header. The DDL is written in RFC-001 §04's lower case because that is how the
schema is discussed; every comparison against a live account is
case-insensitive, and quoting the identifiers to preserve the case would mean
quoting them forever, in every generated query.

**6. `VARCHAR` is `TEXT` and every fixed-point type is `NUMBER`.**
`INFORMATION_SCHEMA.COLUMNS` reports the storage type, with precision and scale
in separate columns, so a check that compares the live schema to the DDL has to
put them back together. `verify._live_type` is that reassembly, and it is the
reason the type comparison is worth having at all rather than being a string
match that never matches.

## 7. What this deliberately does not do

- **No row access policies.** [#43] landed them in `sql/10_policies.sql` and
  they are documented in
  [docs/snowflake-isolation.md](snowflake-isolation.md). Everything here is
  what that ticket attached to: `demo_id` on all nine visitor-scoped tables,
  `NOT NULL` so no row is undecidable, and `visitor_scoped_tables` as the list
  its coverage check reads.
- **No stored procedures.** [#46]. `USAGE ON FUTURE PROCEDURES IN SCHEMA
  CHIP_CHAT.ACCOUNTS` was already granted to `CHIP_CHAT_WRITE` by [#41].
- **No semantic view.** [#45]. The comments on every table and every column are
  its input, which is why they are checked as strictly as the columns are.
- **No nightly publish.** [#39] owns landing the marts and the catalogue out of
  Databricks, atomically, with an alert on a failed run; it has since landed, in
  `databricks/` and `CHIP_CHAT.STAGING`, and
  [nightly-publish.md](nightly-publish.md) is its write-up. It also added the one
  table-level grant in `03_grants.sql`, because it publishes `orders`,
  `order_items` and `loyalty_ledger` too. `chip_chat.snowflake.load` remains the
  developer path: JSONL in a directory, one transaction per table, `TRUNCATE` and
  `COPY` together so a reader sees one generation or the other and never half of
  either — and it is still the only way `demo_visitors`, `personas` and
  `persona_fixtures` reach the account, because the publisher cannot write them.
- **No marts data.** The four mart tables exist and are empty until [#39] runs.
  Nothing in this repository computes them anywhere else.
- **No `item_allergens`.** Section 3.
- **No clustering keys.** Section 5, with the numbers.

## 8. Running it

```bash
make snowflake-apply         # create or re-assert every table, keeping the rows
make snowflake-load-sample   # the committed catalogue fixture, 60 rows
make snowflake-verify-fast   # #41 and #42, without the minute of watching
```

`make snowflake-load LANDING=landing` loads a landing zone that has been
harvested and generated — the catalogue from `landing/catalog`, the account
tables from `landing/accounts/synthetic`, conformed.

`make snowflake-load-roster` loads `data-gen/roster/`, which is the committed
copy of the three tables no landing zone is needed for. Section 9 is why it
exists.

## 9. Two loaders fill `ACCOUNTS`, and for a day they disagreed

Section 7 records, almost in passing, that `chip_chat.snowflake.load` "is still
the only way `demo_visitors`, `personas` and `persona_fixtures` reach the
account, because the publisher cannot write them". The other half of that
sentence is the interesting half: `orders`, `order_items` and `loyalty_ledger`
reach the account the *other* way, from [#39]'s nightly publish out of
Databricks silver, on a schedule, as a role that cannot see the roster at all.

Six tables of one generated population, two loaders, and — until 2026-08-27 —
nothing anywhere that made them name the same generation.

### What it looked like

Measured as `CHIP_CHAT_ADMIN` with `ALL_VISITORS` set, across all twenty-eight
rows of `ACCOUNTS.persona_fixtures` against the tables they claim to describe:

```
FIXTURES 28 · POINTS_AGREE 4 · ORDERS_AGREE 4 · SPEND_AGREE 4
```

`demo-0048`'s fixture said eighty orders, $1,345.75 of lifetime spend and 397
points. The tables said thirty-one orders, $2,430.70 and 1,363 points. The
account was holding a history generated for five hundred customers underneath a
roster generated for sixty: `ACCOUNTS.orders` and `ACCOUNTS.loyalty_ledger`
carried five hundred distinct `demo_id`s, `ACCOUNTS.demo_visitors` carried
`demo-0001` through `demo-0060`, and the twenty-eight fixtures were drawn from
that sixty.

**It is a load defect and it is not a generator defect.** `chip_chat.data_gen`
composes all six tables from one walk and asserts they agree by construction:
`data-gen/tests/test_referential_integrity.py` holds
`sum(entry.delta) == fixture.points_balance` for every fixture in the
population. No coherent generation can look like the account looked. Only
loading two of them can.

**It cost nothing until the account lane was wired.** While `get_points_balance`
returned a hardcoded fixture, the fixture was the only source and there was
nothing in the system for it to contradict; `docs/public-demo.md` §9 was that
defect and closing it is what exposed this one. Afterwards the opening message
is composed from `persona_fixtures.narrative` and the tool sums `delta` from the
ledger, so a visitor was introduced as having 397 points and then told, in the
same conversation, that they had 1,363.

### Why the narrative made this more than a column repair

Two of the seven archetypes quote the balance in prose. The Weekly Regular's
narrative reads *"a regular at IL Town 1 Mall, 590 points on the card"* and the
Lapsed Regular's reads *"15,445 points still unredeemed from 48 orders"* —
eight of the twenty-eight rows say a number of points in words. Correcting
`points_balance` and `order_count` while leaving the sentence beside them alone
would have moved the contradiction from a place a visitor can see it in one turn
to a place they can see it in two, which is worse rather than better. The
generator renders the sentence and the columns from the same measured facts, so
the only repair that holds is one that replaces the row.

### What was done instead of a re-generation

The obvious fix — regenerate the population, load all six tables together — was
the wrong one, and for a reason worth writing down. The four gold marts were
computed in Databricks *from the `orders` the account already held*. Replacing
those orders makes `MARTS.customer_360` and `MARTS.usual_order` describe
customers who no longer exist, which is not an error; it is four tables of
plausible numbers about nobody, and it would have moved the lie from the account
lane to the personalization lane.

So the history was left alone and the roster was reconciled to it. That was
possible because the live history turned out to be **exactly reproducible from
this repository**: the population the account holds is
`generate_population(catalogue fixture, policy fixture, population.toml)` at the
shipped seed 20260826, and the check is not a resemblance —

```
live visitors: 500 generated visitors: 500
mismatched visitors: 0
```

— comparing order count, lifetime spend, ledger sum, ledger entry count and last
order timestamp for all five hundred. The landing zone `docs/snowflake-account.md`
§3.4 records as missing was never lost. It was never written down, which is a
different problem with a different fix.

The three roster tables of that generation are now committed at
`data-gen/roster/`, 140 kilobytes, and `make snowflake-load-roster` loads them.
`manifest.json` beside them records the seed, both input digests and a SHA-256
per table for all six, so "is this the generation the marts were computed
against?" is a question with an answer.

### The two things that make it stay fixed

**`data-gen/tests/test_roster.py`, in `make ci`, free.** It regenerates the
shipped population and holds the committed bytes to it table by table, holds the
manifest to the fresh one, holds every committed fixture to the committed
history, and holds every narrative that mentions points to the balance in the
column beside it. A retune of `population.toml` that moves the population now
fails a test rather than quietly desynchronising the account the next time
somebody publishes.

**`make snowflake-verify`, against the live account.** A new check under [#42]
asks the account itself, in one statement, whether the roster describes the
history it sits beside — points, order count, lifetime spend, first and last
order, usual item, and whether a narrative that says a number of points says the
right one:

```
PASS  persona_fixtures describes the orders and the ledger it sits beside
      28 fixtures, all agreeing on points 28 · orders 28 · spend 28 · dates 28
      · usual 28 · narrative 28
```

It reports a profile rather than a verdict because the profile is the diagnosis.
Twenty-seven of twenty-eight is one bad row; four of twenty-eight is two
generations in one database, and those want different repairs.

The usual-item column is deliberately weaker than the generator's own rule.
`chip_chat.data_gen.fixtures._commonest` breaks a tie between two equally
frequent baskets on the `repr` of a Python tuple, which no SQL expression
reproduces, so a check that tried to rebuild that rule would raise a false alarm
the first time a fixture had two joint-commonest baskets. What is asked instead
is that the item the fixture calls the usual appears in *some* basket the
customer repeated as often as any other — which a fixture from another
generation fails essentially always, and a tie never does.

The check was confirmed to bite by moving one number: `points_balance + 1` on
one row of one fixture, and it named the row.

```
FAIL  persona_fixtures describes the orders and the ledger it sits beside
      28 fixtures; agreeing: POINTS 27 · ORDERS 28 · SPEND 28 · DATES 28 ·
      USUAL 28 · NARRATIVE 27. demo-0033 says 434 points and the ledger sums
      to 433. …
```

Note that `NARRATIVE` fell with `POINTS`. That is the column that exists because
the prose quotes the number.

### Verified on the live app

Not on a test double. Entry, then the one question the numbers can disagree
about, against
`https://ca-chip-chat-web.whitesea-eea6e4c0.eastus2.azurecontainerapps.io`, on
an archetype whose narrative quotes the balance:

```
POST /api/entry   {"name": "Graham"}
  persona_id      regular  ("The Weekly Regular", demo-0272)
  points_balance  590
  narrative       a regular at IL Town 1 Mall, 590 points on the card, and 99%
                  of 79 orders the same Chicken Bowl with guacamole, white
                  rice, black beans and cheese.
  opening         Hi Graham. You're a regular at IL Town 1 Mall, 590 points on
                  the card, and 99% of 79 orders the same Chicken Bowl with
                  guacamole, white rice, black beans and cheese. …

POST /api/chat    {"message": "How many points do I have?"}
  reply           You have 590 points. That's enough right now to redeem a
                  Side Tortilla (85 pts), Chips (350 pts), a Fountain Drink
                  (400 pts), or Guac (500 pts). Want to redeem one?
```

The sentence and the tool say 590. Before the reload they would not have, and
the same run on the Lapsed Regular reads 15,445 in the opening and 15,445 back
from `get_points_balance`.

`/healthz/lanes` still reports `personalization: up` with
`derived_at: 2026-08-27T18:54:51Z` and `stale: false` — the nightly publish was
deliberately **not** re-run, because the orders it computes from were not
touched.

### What this did not fix

`api/src/chip_chat/api/fixtures/persona_fixtures.json` is a committed export of
this table, read only when the API has no Snowflake connection factory
(`docs/decisions/shipped-persona-roster.md`). It is still an export of the old
generation. The deployed app reads Snowflake — `/healthz/lanes` reports the
account lane up, and the transcript above came off the live URL — so the file is
not being consulted today, but it is a third copy of a table that now has two
authoritative ones and it should be re-exported from `data-gen/roster/`.

[#27]: https://github.com/gganssle/chip_chat/issues/27
[#39]: https://github.com/gganssle/chip_chat/issues/39
[#41]: https://github.com/gganssle/chip_chat/issues/41
[#42]: https://github.com/gganssle/chip_chat/issues/42
[#43]: https://github.com/gganssle/chip_chat/issues/43
[#45]: https://github.com/gganssle/chip_chat/issues/45
[#46]: https://github.com/gganssle/chip_chat/issues/46
