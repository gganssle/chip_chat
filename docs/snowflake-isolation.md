# The isolation mechanism

Two row access policies, nine tables, and one session variable. Issue
[#43](https://github.com/gganssle/chip_chat/issues/43), which is one of the two
launch gates and the piece the rest of the system is only safe because of.

[docs/snowflake-schema.md](snowflake-schema.md) is the schema this attaches to —
seventeen tables, and `demo_id` on every one of the nine that belongs to a
visitor. That document ends with "no row access policies, [#43], and it is the
launch gate". This is that ticket.

RFC-001 §05 is the argument and it is short enough to restate. The agent is a
program that takes untrusted input — visitor text, harvested web documents,
uploaded photographs — and produces output that is itself untrusted. Any design
where that program is also responsible for asserting **whose** data to return is
one prompt away from a disclosure. So it isn't. Identity originates in the app's
server-side session, is applied to the connection as a session variable, and is
enforced underneath every query the system runs. No tool signature accepts a
visitor identifier; that absence is the enforcement, and this file is what makes
the absence safe.

The gate is **zero** cross-visitor disclosures, and the reason it is zero rather
than few is that the mechanism is structural. Anything above zero is a broken
mechanism rather than a bad day.

## 1. The shape

```
CHIP_CHAT.ACCOUNTS.visitor_isolation      default deny
    demo_visitors      demo_id = the bound visitor, and nothing otherwise
    orders
    order_items
    loyalty_ledger
    MARTS.customer_360
    MARTS.usual_order
    MARTS.spend_summary

CHIP_CHAT.ACCOUNTS.entry_roster           open while nothing is bound
    persona_fixtures   the roster entry chooses a visitor's customer FROM

not visitor-scoped, and therefore not guarded
    CATALOGUE.*        the real half. A menu is the same for everybody
    ACCOUNTS.personas  a kind of person rather than a person
    MARTS.item_affinity  a fact about two items and about nobody
```

`snowflake/sql/10_policies.sql` is all of it — both policies, both bodies, and
the eight attachments — and it is the file to read. This document is the
argument behind it.

## 2. Why a policy, and not middleware that injects the identifier

Three options were on the table, and RFC-001 D4 picks the third: identity as a
tool argument, identity injected into arguments by middleware, or identity bound
at the connection with no argument at all.

Middleware injection still leaves a *field in the schema*. A field is a code
path where the model's value could be used, and a reviewer has to prove it never
is — every release, forever. Removing the parameter removes the class of bug
rather than an instance of it.

And the account lane settles it on its own. It hands natural language to Cortex
Analyst, a text-to-SQL system, and **executes what comes back**. `SELECT * FROM
orders` is not a query somebody has to be tricked into writing; it is what a
text-to-SQL system produces for "show me my orders" often enough. A row access
policy applies regardless of what that system generates, which is the entire
reason enforcement lives under the query rather than in front of it.

## 3. Default deny is written out rather than inherited

```sql
(GETVARIABLE('DEMO_ID') IS NOT NULL AND row_demo_id = GETVARIABLE('DEMO_ID'))
```

The first half is redundant. `row_demo_id = GETVARIABLE('DEMO_ID')` already
denies an unbound session, because an unset variable is `NULL`, a comparison
against `NULL` is `NULL` rather than `TRUE`, and a row access policy keeps only
the rows its body returns `TRUE` for.

It is written out anyway, and the redundancy is the point. The most important
property of this mechanism — **an unset session variable returns zero rows,
never all rows** — should be legible in the policy body rather than be a
consequence of three-valued logic that a reviewer has to already know. It is the
failure mode that turns a bug into a breach: a connection the pool handed out
without binding is not a connection that errors, it is a connection whose
`WHERE` clause quietly matched everything.

`test_row_access_policies.py` fails if that clause disappears, and
`make snowflake-verify` asks the live account the same question two ways — once
against a fixture, and once by counting the rows an unbound read lane sees in
each of the seven real tables.

## 4. The one inversion, and why it is a second policy

`persona_fixtures` is the roster the entry flow (#67) chooses a visitor's
synthetic customer *from*. That read happens **before** any visitor exists, on a
connection that has bound nobody — and "switch persona", which
`07_accounts.sql` describes as minting a new `demo_id` on a clean connection,
happens the same way. Under `visitor_isolation` both reads return nothing and
entry has no roster to choose from.

There were two ways out and one of them is worse than it looks.

**Leave the table unprotected.** The table carries `demo_id`, so
`visitor_scoped_tables` names it and the coverage check would have to carry a
written exemption. It also means every *bound* conversation, for the life of the
demo, can read every fixture's `demo_id`, `points_balance` and `lifetime_spend`
— and a bound conversation is the state Cortex Analyst runs in.

**Invert the unbound case, and only the unbound case.**

```sql
GETVARIABLE('DEMO_ID') IS NULL OR row_demo_id = GETVARIABLE('DEMO_ID')
```

A session that has bound nobody sees the roster. A session that has bound
somebody sees that visitor's fixture and no other. The state that can read the
whole roster is now the lane that has no visitor to leak it *to*, rather than
every conversation in the demo. That is strictly narrower, so that is what is
there.

It is a **separate policy with its own name** rather than a clause on
`visitor_isolation`, and that is not tidiness. A clause bolted onto the shared
policy widens all seven other tables the day somebody edits the wrong line. Two
policies cannot be widened by accident; one policy with an exception in it can.
`test_row_access_policies.py` requires that exactly one policy is open when
unbound, that it guards exactly one table, and that the reason is written down
in `schema.POLICIES` rather than remembered.

## 5. The maintenance escape, and why it hands over nothing

`visitor_isolation` has a second clause:

```sql
OR (CURRENT_ROLE() = 'CHIP_CHAT_ADMIN' AND GETVARIABLE('ALL_VISITORS') IS NOT NULL)
```

Some questions are legitimately about every visitor at once. `snowflake-load`
counts the rows it has just landed. `snowflake-verify` joins `orders` to
`order_items` to show the schema answers. Issue [#47]'s nightly reset ages
visitors out. None of those can be asked by a session bound to one visitor, and
Snowflake has no owner exemption to fall back on — a row access policy filters
the table for whoever reads it, ownership included.

So an escape has to exist. What matters is what it costs to reach, and it takes
two things the demo cannot produce.

**The role, which gives nothing away.** `CHIP_CHAT_ADMIN` already holds `APPLY
ROW ACCESS POLICY`, which no other role in the account does: it can *detach*
this policy outright. A role that can remove the lock is not further empowered
by being handed a key. No service user holds it — `04_users.sql` grants each of
the three exactly one lane role — and `DEFAULT_SECONDARY_ROLES = ()` means no
session picks it up implicitly.

**The variable, which is what keeps default deny universal.** The escape is the
*presence* of `ALL_VISITORS`, not the *absence* of `DEMO_ID`. An owner session
that simply forgot to bind a visitor therefore reads zero rows exactly as a lane
role does, and a cross-visitor read has to be asked for by name in a session
somebody wrote. "An unset session variable returns zero rows" stays true of
every role in the account, including the one that runs every load and every
rebuild.

`CURRENT_ROLE()`, not `IS_ROLE_IN_SESSION()`. The latter is true for secondary
roles and for roles reached through the grant hierarchy, which is a wider door
than this needs. Only a session whose **primary** role is `CHIP_CHAT_ADMIN`
qualifies.

`make snowflake-verify` checks the escape from both sides, and the second one is
what makes the first mean anything: a lane role that sets `ALL_VISITORS` still
sees nothing, and the owner role with it set sees both visitors. Without the
second check, an escape that had stopped working entirely would look like
excellent security until the next load reported zero rows.

## 6. Why the attachments are in a scripting block

`ALTER TABLE … ADD ROW ACCESS POLICY` fails if the table already has one.
`ALTER TABLE … DROP ROW ACCESS POLICY` fails if it does not. Neither statement
alone is re-runnable, and every file under `snowflake/sql/` has to be — the
whole account is `make snowflake-apply` twice in a row.

`CREATE OR ALTER TABLE` does not help and was checked rather than assumed:
Snowflake's own usage notes say setting a policy on a table with the
`CREATE OR ALTER` variant is not supported, and that existing policies are left
unchanged by one. That second half is what makes a routine apply safe — the DDL
converges the table and steps over the policy — and it is also why the
attachment cannot live in the `CREATE` at all.

So `10_policies.sql` asks `POLICY_REFERENCES` which tables already carry the
policy and emits, per table, either a plain `ADD` or the documented
single-statement swap:

```sql
ALTER TABLE … DROP ROW ACCESS POLICY p, ADD ROW ACCESS POLICY p ON (demo_id)
```

The swap is one statement on purpose. The two-statement version — detach, then
attach — leaves a window in which the table is unprotected, and a window is a
thing somebody eventually queries through.

A table carrying some *other* row access policy matches neither branch, so the
`ADD` fails and the apply stops. That is the intended direction: a policy this
file did not put there is a finding, not something to overwrite quietly.

The policies themselves are `CREATE … IF NOT EXISTS` with a body of `FALSE`,
and the real body arrives by `ALTER … SET BODY`. Snowflake refuses
`CREATE OR REPLACE ROW ACCESS POLICY` on a policy that is attached to anything,
so a file that re-asserted the body that way would work exactly once. The
created body denies everything for a second reason: an apply that dies between
the two statements leaves the account **closed** rather than open.

## 7. How the guarantee is kept from decaying

The failure mode of this design is not a wrong policy. It is a table that
quietly never got one, eighteen months from now, added by somebody who had no
idea this document existed. So the coverage question is asked three times, in
three places that fail for different reasons.

**`make ci`, over the checked-in SQL.**
`snowflake/tests/test_row_access_policies.py` compares the attachment list in
`10_policies.sql` against `chip_chat.snowflake.schema.visitor_scoped()` **in
both directions**. A visitor-scoped table with no attachment fails the build. An
attachment for a table nobody declared visitor-scoped fails it too — that means
the two descriptions of what a visitor owns have come apart, and only one of
them is the one Snowflake enforces.

**`make snowflake-verify`, against the account.** The list of what must be
protected is `09_audit.sql`'s `visitor_scoped_tables` rather than Python's,
because that view reads `INFORMATION_SCHEMA` and defaults to deny: a table
somebody typed into Snowsight at four in the afternoon is on it, and a table
`make ci` has never heard of is exactly the table with no policy on it. A second
check requires the account and the DDL to agree about *which* policy guards
what, which is how the roster's inversion is held to the one table it was argued
for.

**And a canary, because a coverage check that has stopped looking passes
forever.** `verify` creates a table with a `demo_id` and no policy and requires
the check above to name it. If it does not, every clean run of that check meant
nothing — the same failure mode as `09_audit.sql`'s demo_id audit, which is
checked the same way and for the same reason.

## 8. Four things worth knowing before extending this

**A row access policy does not filter `INSERT`.** It filters `SELECT`, and the
rows an `UPDATE`, `DELETE` or `MERGE` can see. Nothing in these policies stops
the ops API writing a row that carries another visitor's `demo_id` — isolation
covers reading somebody else's history and covers editing it, and does not cover
fabricating it.

> **Closed by [#46].** The four write procedures are the only path that writes,
> none of them takes a visitor identifier, and each reads `demo_id` from
> `GETVARIABLE('DEMO_ID')` into one local variable that every `INSERT`, `UPDATE`
> and `MERGE` in the body then uses.
> `snowflake/tests/test_procedure_layout.py` walks every write statement in
> every body to assert it, so the caller cannot express the wrong thing rather
> than being trusted not to. `action_receipts`, the ninth table on the list
> above, arrived with the same ticket and carries `visitor_isolation` for the
> ordinary reason: a spent retry key is a fact about one visitor's attempt.

**`CREATE OR REPLACE TABLE` takes the policy with it.** `CHIP_CHAT_PUBLISH`
holds `CREATE TABLE` on `MARTS`, and a publish that replaces a mart wholesale
would drop the attachment silently and leave three visitor-scoped marts readable
by everybody. `make snowflake-verify` names it afterwards; the fix is that
[#39]'s publish replaces *contents* rather than tables — `TRUNCATE` and `COPY`
in one transaction, which is what `chip_chat.snowflake.load` already does and
for the same reason.

**`USE ROLE X` does not restrict a session to X.** It adds every other role the
user holds as a secondary role, so any check of a policy that omits
`USE SECONDARY ROLES NONE` passes while proving nothing — and an operator's user
holds `ACCOUNTADMIN`. This is finding 1 of
[docs/snowflake-account.md](snowflake-account.md) §8 and it applies to every
check in this document.

**`INFORMATION_SCHEMA` is filtered by the querying role, even through a view.**
`POLICY_REFERENCES` is too. The coverage check runs as `CHIP_CHAT_ADMIN` for
that reason: the write role cannot see `MARTS` at all, so the same query run as
a lane role reports three unprotected marts as protected by not looking at them.

## 9. What this deliberately does not do

- **It does not manage the connection.** Session variables and pooled
  connections are the classic combination for cross-tenant bleed: a connection
  returned to the pool with `DEMO_ID` still set, then handed to another
  visitor's request, defeats every policy here. [#44] owns setting the variable
  on checkout and clearing it on return, and it has landed:
  `api/src/chip_chat/api/pool.py`, with its argument in `api/README.md`. RFC-001
  §05 names this as the risk of the whole design, and nothing in this file can
  catch it — which is why the check that binds lives on the **checkout** rather
  than on the return. Clearing on the way back fails open; a connection whose
  `DEMO_ID` does not read back as `NULL` before a bind is destroyed instead of
  handed out.
- **It does not red-team itself.** [#82] is the adversarial suite pointed at a
  deployment. The concurrency test that would actually catch a pool failure —
  sequential tests pass regardless — is `api/tests/test_pool_concurrency.py`,
  which landed with [#44] and runs on every pull request: 32 visitors through a
  pool of 4, and the same assertions run first against a deliberately broken
  pool so that a green result means something. `verify`'s checks here are the mechanism proving it works;
  that ticket is somebody trying to break it.
- **It does not touch tool signatures.** [#61] implements the six read tools,
  none of which takes a visitor identifier. That absence is the other half of
  RFC-001 §05, and it is a property of those signatures rather than of this
  file.
- **It does not protect the catalogue.** A menu is the same for everybody.

## 10. Running it

```bash
make snowflake-apply         # create both policies and attach all nine tables
make snowflake-verify-fast   # #41, #42, #43 and #88, without the minute of watching
make ci                      # the coverage test, free and offline
```

An apply is safe to repeat: the policies are created if absent, their bodies are
re-asserted every run, and each attachment is either added or swapped in one
statement depending on what the account already has.

[#39]: https://github.com/gganssle/chip_chat/issues/39
[#43]: https://github.com/gganssle/chip_chat/issues/43
[#44]: https://github.com/gganssle/chip_chat/issues/44
[#46]: https://github.com/gganssle/chip_chat/issues/46
[#47]: https://github.com/gganssle/chip_chat/issues/47
[#61]: https://github.com/gganssle/chip_chat/issues/61
[#67]: https://github.com/gganssle/chip_chat/issues/67
[#82]: https://github.com/gganssle/chip_chat/issues/82
