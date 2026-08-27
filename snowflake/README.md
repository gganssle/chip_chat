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
sql/06_catalogue.sql      the real half: menu_items, item_prices, modifiers, stores
sql/07_accounts.sql       the synthetic half. Every table here carries demo_id
sql/08_marts.sql          the four marts Databricks publishes overnight
sql/09_audit.sql          the demo_id rule, as a view that must return no rows
sql/10_policies.sql       the isolation mechanism. Two row access policies and
                          the eight tables they are attached to
sql/11_semantic_view.sql  the account lane, five tables out of fourteen
sql/optional/             never run by an apply: reset.sql, network_policy.sql,
                          trial_credit_cap.sql

src/chip_chat/snowflake/account.py   the layout as data. Creates nothing
src/chip_chat/snowflake/schema.py    the tables as data. Also creates nothing
src/chip_chat/snowflake/semantic.py  the semantic view as data, and the nine
                                     tables it deliberately does not model
src/chip_chat/snowflake/analyst.py   answer, or say so. No network call
src/chip_chat/snowflake/snow.py      the `snow` CLI, wrapped
src/chip_chat/snowflake/apply.py     `make snowflake-apply`
src/chip_chat/snowflake/load.py      `make snowflake-load-sample`
src/chip_chat/snowflake/verify.py    `make snowflake-verify`
```

```bash
make snowflake-apply         # create or re-assert every object. Safe to repeat
make snowflake-cap QUOTA=60  # cap the whole trial. The one number nothing here knows
make snowflake-load-sample   # the committed catalogue fixture, 60 rows
make snowflake-verify        # 80 checks against the live account, ~3 minutes
make snowflake-verify-fast   # 79 of them, skipping the minute of watching
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
visitor_isolation   demo_id = the bound visitor. Seven tables. DEFAULT DENY:
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
which is RFC-001 §10's *"I can't answer that reliably"* as a function. #61 owns
the HTTP and the span.

Measured against the live trial on 2026-08-27: seven answerable questions
answered and executed, ten deliberately unanswerable ones refused with no SQL at
all, and an Analyst round trip with a **3.65s median** — which is past the PRD's
whole-turn target on its own, because Cortex Analyst is not native in this
region. [docs/snowflake-semantic-view.md](../docs/snowflake-semantic-view.md)
has the numbers, the six findings behind the file, and what was not measured.

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
