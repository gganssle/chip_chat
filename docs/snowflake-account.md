# The Snowflake account

What the serving layer contains, who may touch which part of it, and how each of
those claims was checked against the live account rather than against the SQL
that was supposed to produce it. Issue
[#41](https://github.com/gganssle/chip_chat/issues/41), plus the credit ceiling
[#41] deliberately left to an operator — section 7 below, and [#88].

The account was built before it held a single row, for the same reason the
lakehouse catalogue was: ownership and grants are cheap to set on an empty
database and tedious to retrofit onto a populated one, because every object
created in between inherits whatever was true when it was made. [#42] adds the
tables, [#43] the row access policies, [#46] the stored procedures. All three
land into a boundary that already exists.

Everything below is `snowflake/sql/`. Nothing was clicked in Snowsight, which is
the fourth acceptance criterion and the reason the other three are commands
rather than screenshots.

## 1. The shape

```
CHIP_CHAT                          ← one database, owned by CHIP_CHAT_ADMIN
├── CATALOGUE     real, harvested, cited          #42 fills it
├── ACCOUNTS      synthetic, visitor-scoped       #42 fills it, #43 polices it
└── MARTS         published nightly by Databricks #39 fills it
     (PUBLIC dropped — Snowflake creates it and grants the world usage on it)

CHIP_CHAT_SERVING_WH   X-Small · 60s auto-suspend · 60s statement timeout
CHIP_CHAT_PUBLISH_WH   X-Small · 60s auto-suspend · 1h statement timeout

CHIP_CHAT_SERVING_MONITOR   4 credits/day  notify 50/80/100 · suspend 300/400
CHIP_CHAT_PUBLISH_MONITOR   2 credits/day  notify 80        · suspend 100/120
CHIP_CHAT_TRIAL_MONITOR     the whole trial, on the ACCOUNT — opt-in, no default

                     CATALOGUE      ACCOUNTS       MARTS       STAGING  warehouse
  CHIP_CHAT_READ     select         select         select      —        serving
  CHIP_CHAT_WRITE    select         select + DML   —           —        serving
  CHIP_CHAT_PUBLISH  select + DML   3 tables       select+DML  all      publish

  CHIP_CHAT_APP        → READ       the chat app and the Foundry agent
  CHIP_CHAT_OPS        → WRITE      the Azure Functions ops API
  CHIP_CHAT_PUBLISHER  → PUBLISH    the nightly Databricks job
```

One service user per role, one role per user, and no user holds
`CHIP_CHAT_ADMIN`. That last absence is what stops anything a conversation
touches from owning a table — and an owner can drop what it owns, which would
take [#43]'s row access policy with it.

The three lane schemas are the real/synthetic boundary RFC-001 §04 insists on,
made grantable. A table-name prefix could express the same distinction to a
reader and none of it to Snowflake; `CHIP_CHAT_WRITE` having no privilege of any
kind on `MARTS` is not a convention, it is four `GRANT` statements that were
never written.

`STAGING` is a fourth schema and not a fourth population. [#39]'s nightly publish
lands each incoming generation there and then makes it live with one
`INSERT OVERWRITE`; it cannot land beside its target, because `CHIP_CHAT_READ`
holds `SELECT ON FUTURE TABLES` in all three lanes and an incoming copy of
`orders` would therefore be readable, unscoped, by the identity the agent runs
as. It holds no declared table and is empty between runs.
[nightly-publish.md](nightly-publish.md) has the argument in full.

All four schemas are **managed access** schemas. In an ordinary schema the owner
of an object may grant access to it. In a managed-access schema only the schema
owner may, and object owners cannot — so when [#42] creates tables and [#46]
creates procedures here, the ability to widen access to them stays with
`CHIP_CHAT_ADMIN` rather than travelling with whoever created the object. A
boundary any later object owner can open is not a boundary.

## 2. Why the roles are siblings and not a ladder

The obvious hierarchy — write inherits read — is wrong for this account, and the
reason is worth a paragraph because it is the shape most Snowflake examples use.

`CHIP_CHAT_WRITE`'s read surface is deliberately **narrower** than
`CHIP_CHAT_READ`'s, not wider. The ops API needs the catalogue, because an order
line needs a price and a price lives on a restaurant
([menu-pricing.md](decisions/menu-pricing.md)). It has no business reading the
personalization marts. A ladder cannot express that; three disjoint grants can.

`CHIP_CHAT_PUBLISH` is the same argument from the other end. It writes the
catalogue and the marts, and reaches `ACCOUNTS` through exactly three tables —
`orders`, `order_items`, `loyalty_ledger` — granted by name, with no privilege of
any kind on the schema beyond the `USAGE` needed to reach into it. Those three
are `schema.MART_INPUTS`, the tables the marts are computed from, which [#39]
publishes on the same schedule as the marts themselves.

The three it does **not** get are the containment RFC-001 §04 describes when it
says no visitor-editable field is an input to a mart, expressed as a privilege
rather than as a code review. A reviewer confirming that property still does not
have to read the medallion pipeline; they can observe that the identity which
runs it cannot select from `demo_visitors` — where all three editable columns
live, and which is also the one account table a visitor writes to, so a nightly
overwrite would delete their edits. `personas` and `persona_fixtures` are
withheld for the duller reason that nothing publishes them.

All three are granted to `CHIP_CHAT_ADMIN`, and `CHIP_CHAT_ADMIN` to `SYSADMIN`,
so an operator can assume any lane's role to see what it can do. That is an
administrative path, not an escalation: a role can only assume roles granted *to*
it, so `CHIP_CHAT_READ` still cannot reach `CHIP_CHAT_WRITE`.

## 3. The four criteria, and what actually happened

```bash
make snowflake-apply         # create or re-assert. Safe to repeat
make snowflake-verify        # 30 checks against the live account, ~3 minutes
make snowflake-verify-fast   # 29 of them, skipping the minute of watching
make snowflake-rebuild       # drop everything, build it back, verify
```

The transcripts in this section are [#41]'s, recorded when there were 25 checks
and five SQL files. [#88] added a sixth file and five more checks — section 7 —
and they have not been run against the live account yet, so nothing below has
been rewritten to look as though they had.

### 3.1 “Warehouse auto-suspends within 60 seconds of going idle, verified”

Verified means watched. `make snowflake-verify` runs a query that cannot be
answered without compute, confirms `SHOW WAREHOUSES` reports `STARTED`, and then
polls until it reports `SUSPENDED`:

```
PASS  the serving warehouse suspends after going idle
      resumed at 2026-08-26T14:02:20-07:00, then observed SUSPENDED 63s after
      the waking query returned. The setting is 60s; this account's default
      warehouse ships at 600s.
```

Sixty-three rather than sixty because Snowflake looks for idle warehouses on its
own cadence rather than running a timer per warehouse. What would be a finding is
a number near 600 — or, as §8.2 explains, a number *below* 60.

### 3.2 “The read role cannot write, verified by an attempted write that is refused”

Seven attempts, each in its own session holding only `CHIP_CHAT_READ`: insert,
update, delete, create table, drop the table it can read, create schema — and,
first, a *successful* read, because a role with no access to anything refuses
every write too and would be a different bug wearing a passing test's output.

```
PASS  the read role can read -- otherwise the refusals below prove nothing
      returned the row for verify-visitor-mine
PASS  the read role cannot INSERT
      SQL access control error: Insufficient privileges to operate on table
      '_VERIFY_PROBE'. Your primary role CHIP_CHAT_READ must have INSERT
      granted on TABLE CHIP_CHAT.ACCOUNTS._VERIFY_PROBE.
```

### 3.3 “The ops API's write role cannot read another visitor's rows either”

This is the criterion that needs a row access policy, and the real ones are
[#43]'s. `verify` builds a throwaway policy on a throwaway table, proves the
write role is bound by it, and drops both. What is under test here is the
**role**, not the policy: [#41]'s grants are what leave `CHIP_CHAT_WRITE` without
`APPLY ROW ACCESS POLICY` and without ownership of anything, and this is where
that absence is demonstrated instead of asserted.

```
PASS  the write role sees its own visitor's row
PASS  the write role does NOT see the other visitor's row
PASS  the write role cannot UPDATE the other visitor's row by naming it
      rows updated: 0
PASS  the write role cannot detach the policy that binds it
```

The third line is the quiet one. Naming another visitor's row in a `WHERE` clause
is not an error — the policy filters the rows the `UPDATE` can see, so it changes
nothing and says so. A caller that treats “no error” as “it worked” will be wrong
in a way no exception announces, which is worth knowing before [#46] writes its
procedures.

### 3.4 “Entire account rebuildable from `snowflake/` in one run”

Checked the only way a claim like that can be: by doing it.

```
$ make snowflake-rebuild
→ reset.sql (destructive)
  the account is back to the morning of the trial
→ 00_roles.sql … 04_users.sql
5 files applied.
…
25/25 checks passed
make snowflake-rebuild  2:32.41 total
```

Two and a half minutes from an account with nothing in it to one that passes
every check above.

Since [#88] a rebuild is one command short of green, on purpose:
`optional/reset.sql` drops the account-wide credit cap, the numbered files do not
put it back, and the run ends `29/30` with the failing check naming
`make snowflake-cap QUOTA=<credits>`. Section 7 says why the number cannot live
in a file.

## 4. What an apply may and may not do

Re-running `make snowflake-apply` is safe, and the asymmetry that makes it safe is
deliberate: **an apply may create and may tighten, and may not destroy.**

Nothing uses `CREATE OR REPLACE` on an object holding data or a credential.
Roles keep their grants, warehouses keep their `USAGE` grants, the database keeps
its tables, and the service users keep whatever key pairs an operator attached.
What re-running *does* do is re-assert every warehouse property — so a setting
somebody widened in Snowsight is narrowed again by the next apply.

`snowflake/tests/test_account_layout.py::test_an_apply_never_destroys` holds that
invariant in CI by refusing any `DROP` or `CREATE OR REPLACE` in the numbered
files. The single exception is `DROP SCHEMA CHIP_CHAT.PUBLIC`, which removes
something Snowflake created and nothing here writes to.

Destroying is `make snowflake-rebuild`, which says what it drops before it drops
it. Snowflake's `DROP` is a soft delete — `UNDROP DATABASE CHIP_CHAT` works for
the retention period, one day on this account.

## 5. Credentials

**The three service users are created with no credential at all** and therefore
cannot connect until an operator attaches a key pair. That is not an oversight
being tracked; it is the design. A credential in a checked-in file is a
credential in every clone, and an apply that set one would revoke whatever the
operator had rotated to.

Snowflake blocks single-factor password authentication for programmatic users, so
these are `TYPE = SERVICE` and key pair or nothing:

```bash
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out chip_chat_app.p8 -nocrypt
openssl rsa -in chip_chat_app.p8 -pubout -out chip_chat_app.pub

# the public half, in Snowflake:
snow sql -c chipchat -q "ALTER USER CHIP_CHAT_APP SET RSA_PUBLIC_KEY='$(sed -e '1d' -e '$d' chip_chat_app.pub | tr -d '\n')'"
```

The private half goes to Key Vault, like every other non-Azure credential
([local-setup.md](local-setup.md)), and never to `.env`.

## 6. The network policy, and why it is opt-in

Issue #41 asks for one “if the trial edition supports it”. It does — this is an
Enterprise trial and `CREATE NETWORK POLICY` works. What it cannot support is a
policy written before anybody knows the egress addresses, so
`snowflake/sql/optional/network_policy.sql` is a template with no defaults and
`snowflake-apply` does not run anything in `optional/`.

The policies attach to **users**, not to the account. An account-level policy is
the standard way to lock a Snowflake account down and also the standard way to
lock yourself out of it; per-user gets the property the issue wants — the ops
API's credentials are useless from anywhere but the ops API — and leaves the
operator's own login alone.

One warning that belongs before the fact rather than after it. Snowflake refuses
to activate an *account* policy that would block the session activating it. That
protection does not exist here: nothing about the operator's address is checked
when a policy is attached to a service user. Get the addresses wrong and the
failure surfaces as the app losing its database.

## 7. The credit ceiling, and the one number that is not in this repository

Section 1's warehouse settings bound what **one query** costs: X-Small, sixty
seconds of idle, no query acceleration, a statement timeout per lane. None of
them bounds the **total**. A nightly publish that loops, a Snowsight worksheet
left open on a Friday, or a Cortex Analyst query a language model wrote badly can
each spend a 30-day trial in a weekend without violating a single setting above.

Three resource monitors close that, and they are not interchangeable.

| | counts | quota | notifies | suspends |
| --- | --- | --- | --- | --- |
| `CHIP_CHAT_SERVING_MONITOR` | `CHIP_CHAT_SERVING_WH` | 4 credits, **daily** | 50 / 80 / 100% | 300% · 400% immediate |
| `CHIP_CHAT_PUBLISH_MONITOR` | `CHIP_CHAT_PUBLISH_WH` | 2 credits, **daily** | 80% | 100% · 120% immediate |
| `CHIP_CHAT_TRIAL_MONITOR` | the whole **account** | your number, **never resets** | 50 / 75 / 90% | 100% · 110% immediate |

The first two are `snowflake/sql/05_resource_monitors.sql` and every apply
creates them. The third is `snowflake/sql/optional/trial_credit_cap.sql`, which
no apply runs.

**Why the suspend thresholds are asymmetric.** A suspended publish costs a stale
mart until tomorrow. A suspended serving warehouse costs the demo,
mid-conversation, in front of whoever was being shown it. So the publish
warehouse is suspended *at* its quota and the serving warehouse only at three
times its — twelve credits in a day, on a warehouse where every statement times
out after sixty seconds and the compute goes idle sixty seconds after the last
one. There is no demo on the other side of that line, only something that has
come loose. Between the quota and the ceiling the serving monitor notifies and
does nothing else, which is the honest action for a number a genuinely busy day
can reach. Both quotas reset **daily**, so a suspension is over by tomorrow
rather than for the rest of the trial.

**Where the daily numbers come from.** Not from the balance. $400 of credits at
Enterprise's roughly $3 each is about 130 credits over 30 days, which is 4.4 a
day — so 4 credits of serving in one day is a thirtieth of the trial spent on
conversations, and 2 credits of publish is two hours of X-Small for a job that
takes minutes. Both are arithmetic a reader can redo, which is what makes them
safe to check in.

**Where the third number comes from, and why it is not here.** The cap on the
whole trial has to be read off the remaining balance, and that is the one number
a checked-in file cannot know: too low suspends the demo mid-conversation, too
high does nothing at all while looking handled. [#41] left it out for exactly
that reason. So it is a variable in an `optional/` file, next to the network
policy, and it is applied deliberately:

```bash
make snowflake-cap QUOTA=60
```

which refuses a quota the monitor has already counted past — the one wrong number
that suspends every warehouse in the account the instant you press return. The
quota counts from the moment the monitor is first created, not from the start of
the trial, so what to pass is what you are prepared to spend **from now**.
Snowsight → Admin → Cost Management has the remaining balance in dollars;
`SELECT ROUND(SUM(credits_used), 1) FROM
SNOWFLAKE.ACCOUNT_USAGE.METERING_DAILY_HISTORY` has what has gone, lagging by up
to two hours.

**Only the account-level monitor sees `COMPUTE_WH`** — the warehouse Snowflake
created at signup, which still auto-suspends after 600 seconds, still has query
acceleration on, and is still the default in the `snow` connection (§8.7). The
two daily monitors are attached to the two warehouses this repository built, and
a worksheet left open does not run on either of them.

**What no resource monitor counts:** serverless. Snowpipe, tasks, materialized
view maintenance, search optimization, Cortex and query acceleration all bill to
pools a monitor does not watch. That is the second reason
`01_warehouses.sql` sets `ENABLE_QUERY_ACCELERATION = FALSE` rather than trusting
a cap to catch it — a serverless pool is exactly the spend that does not appear
where you go looking for it, and a resource monitor is where you would go
looking.

**A rebuild leaves the account uncapped.** `optional/reset.sql` drops all three
monitors, the numbered apply puts two of them back, and nothing puts back the
third. `make snowflake-verify` fails on that by name rather than letting it be
quiet:

```
#88
  FAIL  the trial has a total credit cap, not just a daily one
        no account-level resource monitor with a SUSPEND trigger. ...
        Choose a quota against the remaining balance and run
        make snowflake-cap QUOTA=<credits>
```

## 8. Nine things that surprised the first person to do this

**1. `USE ROLE X` does not give a session the privileges of X.** It gives it
those *plus every other role its user holds*, as secondary roles, because
`DEFAULT_SECONDARY_ROLES = ('ALL')` is the default and an operator's user holds
`ACCOUNTADMIN`. Run without `USE SECONDARY ROLES NONE`, the read-role check
inserted a row and passed:

```
use role CHIP_CHAT_READ;
insert into CHIP_CHAT.ACCOUNTS.PROBE values ('mallory','SHOULD NOT HAPPEN');
→ number of rows inserted: 1

use secondary roles none;
use role CHIP_CHAT_READ;
insert into CHIP_CHAT.ACCOUNTS.PROBE values ('mallory','SHOULD NOT HAPPEN');
→ 003001 (42501): Insufficient privileges to operate on table 'PROBE'.
```

A security check that passes while proving nothing is worse than no check. It is
also why `04_users.sql` pins every service user to `DEFAULT_SECONDARY_ROLES = ()`
and why `test_account_layout.py` fails if one of them stops being pinned.

**2. Waking a warehouse on purpose takes two tricks, and getting it wrong
reports a number that looks *better* than the truth.** The first version of the
auto-suspend check woke the warehouse with `SELECT 1` — which Snowflake answers
without resuming anything — so it timed a warehouse that had been idle since the
previous check and reported a confident **24 seconds**.

Switching to a query that needs compute fixed it once. On the next run it
reported **27 seconds**, because the second identical query came back from the
**result cache**, which also resumes nothing:

```
before: SUSPENDED  resumed_on 13:52:29
query ok: True
after : SUSPENDED  resumed_on 13:52:29     ← never woke
```

The check now suspends the warehouse first, wakes it with `USE_CACHED_RESULT =
FALSE`, and requires the warehouse's own `resumed_on` to have moved before it
starts its clock. It also fails a measurement that comes in *under* the setting,
because there is no legitimate way for that to happen and it is the exact shape
both bugs took. Three consecutive runs now give 63, 66 and 68 seconds.

Neither wrong version ever failed. A check that measures the wrong thing does not
announce it; it just quietly reports a healthier number than the truth.

**3. A refusal has three shapes, and two of them claim the object does not
exist.** Missing a privilege on an object you can see is `003001 (42501)`.
Missing usage on the schema is `002003 (02000)`, *"Schema does not exist or not
authorized"*. Missing usage on a warehouse is `002043 (02000)`, *"Object does not
exist, or operation cannot be performed"*. The last two are the **stronger**
answer — Snowflake declining to confirm existence — not a weaker one. Any check
that only recognises "insufficient privileges" will report the tightest grants in
the account as failures.

**4. Row access policies have no owner exemption, and `ACCOUNTADMIN` is not
special.** With a policy keyed to a session variable and the variable unset:

```
accountadmin, no DEMO_ID set → rows_visible: 0
```

This is what makes criterion 3.3 a property of the *grants* rather than of
[#43]'s policy text — nothing needs to be excluded from the policy, because
nothing is.

**5. Query acceleration ships on.** Every warehouse this account created at
signup has `ENABLE_QUERY_ACCELERATION = true`, which bills to a separate
serverless pool — exactly the kind of spend that does not appear where you look
for it. Both warehouses here set it false. An X-Small serving one conversation
has nothing to accelerate.

**6. The account has two identifiers and both are correct.** `CURRENT_ACCOUNT()`
returns the locator `HQ72718`; `CURRENT_ACCOUNT_NAME()` returns `GS74649`, in
organisation `LLMPCWE`. Records written at different moments name different ones
and look like they contradict each other. `snow connection add` wants the locator
form, `hq72718.us-east-2.aws`; Snowflake's documentation calls
`LLMPCWE-GS74649` the account identifier. Both are in `.env.example`, labelled.

**7. The default warehouse in the connection is still `COMPUTE_WH`.** Which
auto-suspends after **600 seconds**. Ad-hoc SQL run through `snow sql` without a
`USE WAREHOUSE` therefore wakes the most expensive idle warehouse in the account.
Nothing in `snowflake/sql/` touches it — it is not ours to rebuild — so this is a
finding rather than a fix. There is no `snow connection` subcommand that edits an
existing entry, so point it at the serving warehouse by hand:

```toml
# ~/.snowflake/config.toml, mode 600
[connections.chipchat]
warehouse = "CHIP_CHAT_SERVING_WH"
```

**8. A resource monitor's notifications go nowhere by default, and nothing tells
you.** `DO NOTIFY` mails the users in `NOTIFY_USERS`, plus account administrators
who have *both* a verified email address and notifications switched on. A fresh
trial account has neither, so every NOTIFY trigger in section 7 fires into
nothing until somebody fixes it once, by hand:

```sql
-- verify the address first: Snowsight → your name → Profile → email
ALTER USER <you> SET EMAIL = 'you@example.com';
ALTER RESOURCE MONITOR CHIP_CHAT_SERVING_MONITOR SET NOTIFY_USERS = ('<you>');
```

`snowflake/sql/` deliberately does not do this: a checked-in file cannot know the
operator's user name, and re-asserting `NOTIFY_USERS` on every apply would revoke
whoever had been added since. It is the same argument that keeps `RSA_PUBLIC_KEY`
out of `04_users.sql`. What `make snowflake-verify` does instead is print the
recipients as evidence on every monitor check, so an empty list is visible rather
than assumed — and the design does not rest on it, because the SUSPEND triggers
work whether or not anybody is reading email.

**9. A resource monitor cannot be made idempotent the way a warehouse can.**
`01_warehouses.sql` re-asserts every property on every apply, so a setting
somebody widened in the UI is narrowed again. Doing the same to a monitor is a
trap with no error message: `FREQUENCY` may only be set together with
`START_TIMESTAMP`, and setting `START_TIMESTAMP` **restarts the counting period**
— zeroing `used_credits`. An apply that re-asserted the frequency would hand a
runaway a fresh quota every time anybody ran `make`, and the account would look
perfectly healthy the whole time.

So `CREATE RESOURCE MONITOR IF NOT EXISTS` owns the frequency and the start, and
the `ALTER` re-asserts only the quota and the triggers. `CREATE OR REPLACE` is
worse still for the same reason, and is refused in CI by
`test_an_apply_never_destroys` alongside the objects that hold data. The one
property an apply cannot narrow back is the frequency, and
`test_the_frequency_is_set_once_and_never_re_asserted` is what stops somebody
adding it to the `ALTER` in good faith.

## 9. What this deliberately does not do

- **~~No tables.~~** [#42] added them — fourteen, into the schemas and the
  grants that already existed, and [#46] added three more for the same reason
  it added the procedures. `SELECT` on `FUTURE TABLES` was granted in every
  schema, so neither also meant re-running a grants file nobody remembers is
  required, and neither did. See
  [docs/snowflake-schema.md](snowflake-schema.md).
- **No row access policies on real tables.** [#43]. The throwaway one in
  `verify` proves a fact about the roles and is dropped in the same run.
- **~~No stored procedures.~~** [#46] added four, in
  `CHIP_CHAT.ACCOUNTS`, all `EXECUTE AS CALLER`. `USAGE ON FUTURE PROCEDURES IN
  SCHEMA CHIP_CHAT.ACCOUNTS` had already been granted to `CHIP_CHAT_WRITE`
  ahead of them existing, which is why landing them did not also mean
  re-running the grants. What it *did* need was the one grant nobody had
  anticipated — `USAGE ON SEQUENCES`, because caller's rights means the caller
  needs every privilege the body uses, and an owner's-rights procedure would
  have hidden that by needing none of them.
- **No `SNOWFLAKE.CORTEX_USER` grant.** The Cortex Analyst semantic view is
  [#45]'s, and the read role's grant list is the security artefact of this issue
  — every line in it should be one that something already built needs.
- **No cap on the whole trial in the numbered apply.** Section 7. The two daily
  monitors are there and every apply creates them; the cap on the account needs a
  quota read off the remaining balance, so it is `make snowflake-cap
  QUOTA=<credits>` and a failing check until somebody runs it.
- **No `NOTIFY_USERS` on any monitor.** Section 8 item 8. The suspensions do not
  depend on it; the notifications do.
- **No network policy in force.** Section 6.
- **No `make ci` integration.** These targets need a `snow` connection and a live
  trial, and a gate that needs a credential and a credit balance is not a gate.
  What *is* in CI is `snowflake/tests/`, which holds the SQL to
  `chip_chat.snowflake.account` and `chip_chat.snowflake.schema` for free and
  offline: that no `GRANT` contradicts the access table, that no lane role can
  apply a policy or own anything, that each lane holds exactly one warehouse,
  that an apply cannot destroy, that no monitor re-asserts the property that
  would zero its own counter, and — from [#42] — that every visitor-scoped table
  carries `demo_id` and every column carries a comment.

[#39]: https://github.com/gganssle/chip_chat/issues/39
[#41]: https://github.com/gganssle/chip_chat/issues/41
[#42]: https://github.com/gganssle/chip_chat/issues/42
[#43]: https://github.com/gganssle/chip_chat/issues/43
[#45]: https://github.com/gganssle/chip_chat/issues/45
[#46]: https://github.com/gganssle/chip_chat/issues/46
[#88]: https://github.com/gganssle/chip_chat/issues/88
