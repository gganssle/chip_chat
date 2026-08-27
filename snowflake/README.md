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
sql/optional/             never run by an apply: reset.sql, network_policy.sql,
                          trial_credit_cap.sql

src/chip_chat/snowflake/account.py   the layout as data. Creates nothing
src/chip_chat/snowflake/schema.py    the tables as data. Also creates nothing
src/chip_chat/snowflake/snow.py      the `snow` CLI, wrapped
src/chip_chat/snowflake/apply.py     `make snowflake-apply`
src/chip_chat/snowflake/load.py      `make snowflake-load-sample`
src/chip_chat/snowflake/verify.py    `make snowflake-verify`
```

```bash
make snowflake-apply         # create or re-assert every object. Safe to repeat
make snowflake-cap QUOTA=60  # cap the whole trial. The one number nothing here knows
make snowflake-load-sample   # the committed catalogue fixture, 60 rows
make snowflake-verify        # 62 checks against the live account, ~3 minutes
make snowflake-verify-fast   # 61 of them, skipping the minute of watching
make snowflake-rebuild       # drop it all, build it back, verify
```

The tables are `CREATE OR ALTER TABLE`, which converges an existing table to the
declaration and **keeps its rows** — and keeps a row access policy attached to
it, which is what makes a routine apply safe once [#43] lands. Changing that one
word to `OR REPLACE` would empty every table on the next apply, so
`test_account_layout.py` fails on it.

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

`visitor_scoped_tables`, beside it, is the list [#43]'s coverage test needs:
every table on it must carry a policy.

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

[#39]: https://github.com/gganssle/chip_chat/issues/39
[#43]: https://github.com/gganssle/chip_chat/issues/43
