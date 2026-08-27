# `snowflake/` — the serving layer

The governed database the agent hits on **every single turn**. Databricks does
the expensive overnight thinking; Snowflake answers in the conversation
(RFC-001 §03). This package holds the account itself as checked-in SQL, plus the
code that applies it and the code that checks it is still true.

The account is live: a 30-day Enterprise trial started **2026-08-25**, capped at
roughly $400 of credits, whichever comes first. That deadline is the reason
almost every design choice here is about idleness rather than about speed.

## What is here

```
sql/00_roles.sql          four roles, and why they are siblings not a ladder
sql/01_warehouses.sql     two X-Small warehouses, both suspending after 60s
sql/02_database.sql       CHIP_CHAT, four managed-access schemas, PUBLIC dropped
sql/03_grants.sql         the security boundary. Read this one first
sql/04_users.sql          three service users, no credentials
sql/05_resource_monitors  a daily credit ceiling per warehouse, and why they differ
sql/06_catalogue.sql      the real half: menu_items, item_prices, modifiers,
                          stores, and the published rewards #46 reads
sql/07_accounts.sql       the synthetic half. Every table here carries demo_id,
                          #47's demo_visitor_baseline included
sql/08_marts.sql          the four marts Databricks publishes overnight
sql/09_audit.sql          the demo_id rule, as a view that must return no rows
sql/10_policies.sql       the isolation mechanism. Two row access policies and
                          the ten tables they are attached to
sql/11_semantic_view.sql  the account lane, five tables out of eighteen
sql/12_procedures.sql     the write path: place_order, redeem_points,
                          update_preferences
sql/13_cancel_order.sql   cancel_order, alone, and why it is alone
sql/14_demo_reset.sql     #47's nightly reset: age a session out, put that
                          visitor back, and the task that runs it at 09:00
sql/optional/             never run by an apply: reset.sql, network_policy.sql,
                          trial_credit_cap.sql

src/chip_chat/snowflake/account.py     the layout as data. Creates nothing
src/chip_chat/snowflake/schema.py      the tables as data. Also creates nothing
src/chip_chat/snowflake/semantic.py    the semantic view as data, and the tables
                                       it deliberately does not model
src/chip_chat/snowflake/procedures.py  the write path as data. Same bargain
src/chip_chat/snowflake/analyst.py     answer, or say so. No network call
src/chip_chat/snowflake/cortex.py      the Cortex Analyst call itself (#61)
src/chip_chat/snowflake/reads.py       the three visitor-scoped read queries,
                                       none of which names a visitor
src/chip_chat/snowflake/lane.py        the account and personalization lanes:
                                       four tools, and four ways to decline
src/chip_chat/snowflake/testing.py     doubles for both seams, so the declines
                                       are asserted for free
src/chip_chat/snowflake/snow.py        the `snow` CLI, wrapped
src/chip_chat/snowflake/apply.py       `make snowflake-apply`
src/chip_chat/snowflake/load.py        `make snowflake-load-sample`
src/chip_chat/snowflake/reset.py       #47's reset as data, and
                                       `make snowflake-demo-reset`
src/chip_chat/snowflake/verify.py      `make snowflake-verify`
```

```bash
make snowflake-apply         # create or re-assert every object. Safe to repeat
make snowflake-cap QUOTA=60  # cap the whole trial. The one number nothing here knows
make snowflake-load-sample   # the committed catalogue fixture, 60 rows
make snowflake-verify        # 99 checks against the live account, ~5 minutes
make snowflake-verify-fast   # 98 of them, skipping the minute of watching
make snowflake-demo-reset    # age demo sessions out now. Plan it first
make snowflake-rebuild       # drop it all, build it back, verify
```

The tables are `CREATE OR ALTER TABLE`, which converges an existing table to the
declaration and **keeps its rows** — and keeps a row access policy attached to
it, which is what makes a routine apply safe now that [#43] has landed. Changing
that one word to `OR REPLACE` would empty every table on the next apply *and*
detach its policy, so `test_account_layout.py` fails on it.

`snowflake-cap` is the odd one out. Everything else here is a file an apply runs;
that target sets a credit quota on the whole account, and the quota is the one
number in this package that has to come from an operator looking at the bill
rather than from arithmetic. Guess it low and the demo suspends mid-conversation,
guess it high and it looks handled while doing nothing —
[docs/snowflake-account.md](../docs/snowflake-account.md) §7 has the whole
argument, and `make snowflake-verify` fails by name while it is unset.

## The boundary, in one table

|  | `CATALOGUE` | `ACCOUNTS` | `MARTS` | `STAGING` | warehouse |
| --- | --- | --- | --- | --- | --- |
| `CHIP_CHAT_READ` | select | select | select | — | serving |
| `CHIP_CHAT_WRITE` | select | select + DML | — | — | serving |
| `CHIP_CHAT_PUBLISH` | select + DML | three tables | select + DML | all | publish |

That table is `account.GRANTS`, `sql/03_grants.sql` is the same table spelled as
privileges, and `tests/test_account_layout.py` fails in `make ci` if a `GRANT`
appears that the table does not permit. Widening the ops API's reach to the
personalization marts is a failing test, not a line nobody re-reads.

**The one exception in that grid is `CHIP_CHAT_PUBLISH` on `ACCOUNTS`.** [#39]
publishes the synthetic account tables on the same schedule as the marts, so the
publisher holds `orders`, `order_items` and `loyalty_ledger` — granted by name,
which is `schema.MART_INPUTS`, the tables the marts are computed from — and
nothing at all on `demo_visitors`, where every visitor-editable column lives.
`Access.tables` is where that exception is declared and where its argument is
written down; a table-level grant the access table does not name is a failing
test.

`STAGING` is the loading dock the nightly publish stages a generation in before
one `INSERT OVERWRITE` makes it live. It holds no declared table, is empty
between runs, and is the one schema in the database that no identity a
conversation touches can read.
[docs/nightly-publish.md](../docs/nightly-publish.md) is the write-up.

## One column, and the view that will not let it slip

Every visitor-scoped table carries `demo_id`. That is a schema requirement
rather than a convention: a row access policy filters **one** table against a
session variable and cannot follow a join, so a visitor-scoped table without the
column is one [#43] cannot protect at all.

```sql
SELECT * FROM CHIP_CHAT.ACCOUNTS.tables_missing_demo_id;   -- empty is a pass
```

It defaults to deny — a new table in `ACCOUNTS` or `MARTS` is presumed
visitor-scoped until somebody exempts it by name in `09_audit.sql` and writes
down why. Two are: `personas`, which is a kind of person rather than a person,
and `item_affinity`, which is about two items and nobody. `verify` also creates
a table without the column and requires the audit to notice, because an audit
that has quietly stopped looking reports a clean account forever.

`visitor_scoped_tables`, beside it, is the list the coverage check reads: every
table on it must carry a row access policy, and `make snowflake-verify` names
any that does not.

## Two policies, and one of them is open on purpose

[#43] is the launch gate and `sql/10_policies.sql` is it. Identity originates in
the app's server-side session, reaches Snowflake as the `DEMO_ID` session
variable, and is enforced under every query the system runs — so that no tool
signature has to carry a visitor identifier and no injected instruction has a
field to populate.

```sql
visitor_isolation   demo_id = the bound visitor. Nine tables. DEFAULT DENY:
                    an unset variable returns zero rows, never all of them
entry_roster        persona_fixtures only, and open while nothing is bound —
                    entry chooses a visitor's customer from it before there
                    is a visitor to bind
```

The inversion is one table, one policy of its own, and an argument written down
in three places rather than remembered. Bind a visitor and the roster narrows to
that visitor's fixture like everything else does; the state that can read the
whole roster is the lane with no visitor to leak it to.

Neither body names a lane role. Snowflake has no owner exemption, so the ops
API's `CHIP_CHAT_WRITE` is bound by exactly the same policy the read lane is,
and `test_row_access_policies.py` fails if a role name ever appears in one — a
single `OR` clause is all it would take, and it would read like a convenience.
The one escape needs both `CHIP_CHAT_ADMIN`, which no service user holds and
which can detach the policy anyway, *and* an `ALL_VISITORS` variable set on
purpose: default deny therefore survives for every role in the account,
including the one that runs every load.

[docs/snowflake-isolation.md](../docs/snowflake-isolation.md) is the whole
argument — why a policy rather than middleware, why the redundant `IS NOT NULL`
is the most important half of the body, why the attachments are a scripting
block rather than eight `ALTER TABLE` statements, and the four things to know
before extending it.

## The account lane, and the nine tables it cannot see

`sql/11_semantic_view.sql` is [#45]: the native `CREATE SEMANTIC VIEW` Cortex
Analyst answers account questions from. Five logical tables out of fourteen,
with a synonym list on every field, and seven verified queries so the frequent
questions are not re-derived on every turn.

Almost all of it is subtraction, and the subtraction is where the argument is.
`menu_items` is in the model for its name and its category and for **nothing
else** — no calories, no allergens — because "how many calories have I eaten
this year" is a question the golden set requires the account lane to REFUSE, and
a refusal that rests on an absent column is one no prompt can talk its way past.
All four gold marts are out for a different reason: RFC-001 §10 wants a stale
mart served with its `derived_at`, and a generated query will not carry one.

`demo_id` appears in no dimension, no fact, no relationship and no verified
query. Isolation is [#43]'s row access policy on the base table, so every query
here is written as though the visitor were the only person in the database.

```sql
SELECT * FROM SEMANTIC_VIEW(CHIP_CHAT.ACCOUNTS.ACCOUNT_LANE
    METRICS orders.total_spend DIMENSIONS restaurants.store_name);
```

Three things about this file are easy to lose and expensive to lose:

* **A semantic view is not a view.** `GRANT SELECT ON ALL VIEWS` does not reach
  it. `03_grants.sql` carries `ON ALL` and `ON FUTURE SEMANTIC VIEWS` too.
* **`COPY GRANTS` is load-bearing.** `CREATE OR REPLACE` drops the object's
  grants, so without it every routine apply silently revokes the read role and
  the lane goes dark with nothing in any log naming a privilege.
* **A verified query is written against the LOGICAL model** — `__orders`, and
  the element names. Physical SQL is accepted, silently rewritten into something
  that does not compile, and then dropped, while the request still succeeds.

`analyst.py` is the other half and it makes no network call: it takes a Cortex
Analyst response and returns either SQL worth running or the reason it will not,
which is RFC-001 §10's *"I can't answer that reliably"* as a function.

`cortex.py` is the HTTP and `lane.py` is the span, both #61's. `AccountLane.ask`
opens one `db.cortex_analyst` around all three steps — the Analyst call, the
decision, and the execution of whatever it admitted — because that span's two
attributes are *the SQL* and *how many rows it returned*, and those come from
opposite ends of the sequence.

Authentication is a key-pair JWT minted by `snow connection generate-jwt`, from
the same `~/.snowflake/config.toml` connection `snow.py` shells out to. Same
argument as that module's: a second code path that knew how to sign a JWT is a
second thing to fix when the key rotates. A deployment that cannot ship the CLI
supplies a different `TokenSource`; that is what the protocol is for.

Measured against the live trial on 2026-08-27: seven answerable questions
answered and executed, ten deliberately unanswerable ones refused with no SQL at
all, and an Analyst round trip with a **3.65s median** — which is past the PRD's
whole-turn target on its own, because Cortex Analyst is not native in this
region. [docs/snowflake-semantic-view.md](../docs/snowflake-semantic-view.md)
has the numbers, the six findings behind the file, and what was not measured.

## The reads that name nobody

`reads.py` is `get_points_balance`, `get_usual_order` and `get_recommendations`,
and **not one of the four statements in it carries a predicate on `demo_id`**.
`sql/11_semantic_view.sql` already gives the reason for the generated path and
it is the same reason here: every query is written as though the visitor were
the only person in the database, because [#43]'s row access policies mean the
session cannot see another visitor's rows. A `WHERE demo_id = ...` would need a
visitor identifier to put in it, and RFC-001 §06's whole design is that no
signature has one. `test_reads.py` asserts the string's absence from every
statement.

Identity arrives through `SessionCheckout`, which is
`chip_chat.api.pool.VisitorPool.for_session` — a *session* id in, a connection
with one visitor already bound out. The lanes hold that callable and never a
`demo_id`.

Two absences are reported rather than papered over. `CHIP_CHAT.MARTS` has no
`recommendations` table yet (bead `cc-afo5`), so `get_recommendations` declines
and says so; nothing publishes `rewards` yet (`cc-99cn`), so the balance comes
back real with a note saying the catalogue it would be spent against is not
loaded. Both are visible in a trace as declining lanes, which is what they are.

## The write path, and the one word holding it up

Four procedures in `CHIP_CHAT.ACCOUNTS`, one per write tool, and the ops API
reaches the database through nothing else. The word is **`EXECUTE AS CALLER`**,
on all four.

Snowflake's default is owner's rights, and an owner's-rights procedure executes
as `CHIP_CHAT_ADMIN`: `GETVARIABLE('DEMO_ID')` reads the *owner's* session rather
than the caller's, and [#43]'s row access policies are evaluated against the
owner. A write path built that way would make every visitor look like the same
one and would pass every test that opens a single session.
`test_procedure_layout.py` fails on the missing word, and `snowflake-verify`
asks the live account with `DESC PROCEDURE`.

The second thing to know is that **row access policies do not filter `INSERT`**.
They filter `SELECT`, `UPDATE` and `DELETE`, so isolation looks correct in every
read path and in review, and a procedure that accepted a visitor identifier
could still attribute a row to somebody else. None of them does: `demo_id` is
read from the session into one local variable and every write uses that
variable, which a test asserts statement by statement. The caller cannot express
the wrong thing rather than being trusted not to.

| | takes | writes | invented |
| --- | --- | --- | --- |
| `place_order` | retry key, store, channel, lines | `orders`, `order_items`, `loyalty_ledger` | — |
| `redeem_points` | retry key, reward, quoted cost | `loyalty_ledger` | the reward ids |
| `update_preferences` | retry key, a partial object | `demo_visitors` | the two ceilings |
| `cancel_order` | retry key, order | `orders`, `loyalty_ledger` | **the action itself** |

All four also write `action_receipts`, which is what makes them idempotent: each
claims its retry key with a `MERGE` as the first statement inside its own
transaction, so a simultaneous retry waits rather than racing past, and a call
that fails rolls the claim back with everything else. A `SELECT` then `INSERT`
would read identically in review and would not serialise — Snowflake's `INSERT`
does not conflict with a concurrent `INSERT` and its `SELECT` takes no lock — and
the failure it produces is a second real order.

`cancel_order` is in a file of its own because it models an affordance the
published record **refuses**: *"When you submit an order, it's sent directly to
our restaurant crew, so we're unable to cancel"*, and a delivery order can only
be cancelled through Customer Service, possibly for a fee. Both sentences are on
its receipt. PRD T1 requires the action and PRD T5 says every action is
simulated, so it exists — and [docs/action-surface.md](../docs/action-surface.md)
§10 records the exit, which is a PRD change. Keeping it in one file keeps that
exit a deletion: three deletions, one `DROP PROCEDURE`, and no migration. The
header of `sql/13_cancel_order.sql` is that list.

**What this tier does not validate** is `procedures.ENFORCED_ELSEWHERE`, and it
is written down rather than left as an absence. Required modifier slots,
per-pair portion permissions and the six per-item caps are about columns the
serving projection of the catalogue does not carry — `CATALOGUE.modifiers` is
five columns and there is no `portion_options` table — so they are enforced at
proposal time in `api/drafts.py` against `chip_chat.catalog`. What #46 asks the
database to make structural is the other thing, and that is here: **no SKU in
any response that does not exist in the catalogue**, checked at the row that
would have to exist rather than at the matcher.

## The reset, and the two things it is careful about

`sql/14_demo_reset.sql` is [#47]: one procedure, one task at 09:00 UTC, and a
manual trigger that runs the same procedure with the same arguments.

It **ages sessions out** rather than truncating, because [#9] decided a
visitor's state persists between visits — so emptying the tables nightly would
empty the account of somebody who is coming back tomorrow, which is the
cold-start failure the PRD is most afraid of, on a schedule. What it deletes is
only what a visitor added, which is identifiable without a diff: every row above
the `ord-9000001` / `loy-9000001` band, plus every `action_receipts` row. What
it restores is only what a visitor could edit, out of `demo_visitor_baseline` —
the eighth table in `07_accounts.sql`, filled from the generator's own
`demo_visitors.jsonl` in the same run as `demo_visitors` itself, which is what
makes "restores generated state exactly" checkable rather than assertable.

```bash
make snowflake-demo-reset-plan   # who would be aged out. Changes nothing
make snowflake-demo-reset        # do it
```

The two failures worth knowing about both look like success:

* **A reset with no maintenance escape deletes nothing.** #43's policies filter
  `DELETE` and `UPDATE`, so an admin session that has bound no visitor changes
  no rows and reports a clean run. The procedure sets `ALL_VISITORS` — the
  escape's second caller, as `10_policies.sql` says — and then *checks that it
  took*, refusing outright if it did not.
* **A `DELETE` that lost its band predicate empties a persona** and leaves a
  perfectly plausible row count behind it. `tests/test_demo_reset.py` reads
  every `DELETE` in the file and fails on the missing predicate.

A visitor with no dated activity at all — a `thread_id` and nothing else — is
**held rather than guessed about**, and the count comes back as `held_no_clock`.
That number is the app tier's bill: only `update_preferences` writes `last_seen`
today, and whatever writes `thread_id` when a session binds is the thing that
should write `last_seen` beside it.

[docs/demo-reset.md](../docs/demo-reset.md) is the write-up — the four clocks,
why the two-day TTL is derived from the session cookie rather than chosen, and
§6, which is the decision `docs/nightly-publish.md` §7 routed here: a visitor's
live rows survive until that visitor ages out, so the nightly publish is what
has to stop replacing three account tables wholesale.

## Two files, two different questions

`tests/` asks whether the **SQL** still says what `account.py` and `schema.py`
say. Free, offline, in `make ci`, and it is what catches a renamed warehouse, a
widened grant, a dropped column or a credit quota that stopped matching its
monitor at the moment somebody writes it. `test_credit_cap.py` is the exception
that proves the rule: it is the only test here that exercises Python rather than
SQL, because the guard that
refuses a quota the account has already spent past is the one check the SQL
cannot make about itself.

`verify.py` asks whether the **account** is what the SQL says. That needs a live
trial and a credential, so it is a `make` target rather than a gate — a UI click,
a hand-made grant or an expired trial can all change the answer without anybody
editing a file.

## Before you add anything here

**`USE ROLE X` does not restrict a session to X.** It adds every other role the
user holds as a secondary role, so any check of a role boundary that omits
`USE SECONDARY ROLES NONE` passes while proving nothing. That mistake, and eight
others of the same kind, are written up in
[docs/snowflake-account.md](../docs/snowflake-account.md) §8 — which is the file
to read before extending this package.

[docs/snowflake-schema.md](../docs/snowflake-schema.md) is the same thing for
the tables: the ten columns RFC-001 §04 does not print and why each is
unavoidable, why nothing is clustered (the whole database is 193 KB, and a
micro-partition holds 16 MB), and six more findings of the same kind — including
that `INFORMATION_SCHEMA` is filtered by the querying role *even through a
view*, so an audit run as the wrong role passes by not looking.

[docs/snowflake-semantic-view.md](../docs/snowflake-semantic-view.md) is the
same thing for [#45]'s account lane: what the view models, the nine tables it
does not, six findings measured against the live trial — a semantic view is not
a view, `COPY GRANTS` is load-bearing, a verified query that names a physical
table is silently dropped — and the latency numbers the PRD's turn targets are
being re-baselined against.

[#39]: https://github.com/gganssle/chip_chat/issues/39
[#43]: https://github.com/gganssle/chip_chat/issues/43
[#45]: https://github.com/gganssle/chip_chat/issues/45
[#46]: https://github.com/gganssle/chip_chat/issues/46
[#47]: https://github.com/gganssle/chip_chat/issues/47
[#9]: https://github.com/gganssle/chip_chat/issues/9
