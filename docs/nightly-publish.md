# The nightly publish

The seam between the two clocks. Databricks does the expensive overnight
thinking; Snowflake is the governed serving layer the agent queries on every
turn. This is the job that carries one into the other, and the argument for the
five choices it makes.

Issue [#39](https://github.com/gganssle/chip_chat/issues/39). RFC-001 D2, §10,
§13 Q1. The mechanism was settled on
[#12](https://github.com/gganssle/chip_chat/issues/12) — the Snowflake
connector, not Iceberg — and is not re-argued here.

The code is in three places and each has a different job.
`databricks/src/chip_chat/databricks/publish.py` declares *what* crosses and by
what statement, and is stdlib-only so a cluster can read it and `make ci` can
test it. `databricks/notebooks/snowflake_publish.py` is the loop.
`infra/terraform/databricks_publish.tf` is the job, its schedule and its alert.

> **Status.** Run. `chip-chat-publish` moved all eleven tables into
> `hq72718.us-east-2.aws` on 2026-08-27 — 108,157 rows in 176.7 seconds — and
> §6 records what it cost on both sides, including the cross-cloud egress
> question [#104](https://github.com/gganssle/chip_chat/issues/104) put against
> this issue. It took five attempts and each of the four failures is written
> down where it belongs: the clock check in §4, the credential and the
> serialised verdict in `publish.py`'s own docstrings, and the row access policy
> in §7. None of them could have been found without running it, and one of them
> was found by the alert this ticket is required to have.
>
> **One thing is live and not in the repository.** #43's `VISITOR_ISOLATION`
> policy now exempts `CHIP_CHAT_PUBLISH`; the change was applied to the account
> and belongs in `snowflake/sql/10_policies.sql`, which is #43's file. Until it
> lands there, `make snowflake-apply` reverts it and the next publish fails at
> the first table. §7.

---

## 1. What crosses

Eleven tables of the fourteen `docs/snowflake-schema.md` declares.

| Snowflake | From | Why it is nightly |
| --- | --- | --- |
| `CATALOGUE.menu_items`, `item_prices`, `modifiers`, `stores` | `silver_harvested` | The harvest re-runs weekly (#38) and prices move |
| `ACCOUNTS.orders`, `order_items`, `loyalty_ledger` | `silver_synthetic` | The three tables the marts are computed from |
| `MARTS.customer_360`, `usual_order`, `item_affinity`, `spend_summary` | `gold_synthetic` | Recomputed whenever silver moves |

The catalogue tables are a **projection** rather than a copy: `menu_items`
carries fifteen columns in the lakehouse and nine here. `sql/06_catalogue.sql`'s
header has that argument — a column nothing in a conversation reads is a column
Cortex Analyst can still put in a generated query.

### The three that do not cross, which is the more interesting list

**`demo_visitors`.** All three columns a visitor may edit live on it —
`display_name`, `home_store_override`, `stated_preferences` — and it is the only
account table a visitor writes to. A nightly overwrite would delete every edit
made that day. It is also the table RFC-001 §04 rests its answer to PRD Q2 on:
no editable field is an input to a mart, and the check is that the jobs
physically cannot read the table they live on.

So the publisher is granted the other three **by name**, and holds nothing at all
on this one. `snowflake/sql/03_grants.sql` is where that is spelled and
`account.PUBLISHED_ACCOUNT_TABLES` is where it is declared;
`test_account_layout.py` asserts that list is `schema.MART_INPUTS`, so the
tables the marts are computed from and the tables the publisher may replace are
one list in one place.

**`personas` and `persona_fixtures`.** Reference rows the generator emits once.
They reach Snowflake through `chip_chat.snowflake.load`, run by an operator as
`CHIP_CHAT_ADMIN` — the developer path, which `snowflake/README.md` describes.

**`gold_synthetic.recommendations`** is a fourth absence and a different kind.
#37 batch-scores the recommender into a fifth gold table and `CHIP_CHAT.MARTS`
has no table for it, because RFC-001 §04 fixes four. Publishing it means adding a
table to the serving schema, which is a decision about where
`get_recommendations` reads from rather than a line in a publish list. Tracked
separately.

---

## 2. Atomic, and what that cost

> **A consumer querying mid-publish must never see a half-swapped mart.**

Two writes per table, and only the second one is visible.

```
Spark ──write──▶ CHIP_CHAT.STAGING.MARTS_USUAL_ORDER
                          │
                          │  INSERT OVERWRITE INTO CHIP_CHAT.MARTS.usual_order
                          ▼
                 CHIP_CHAT.MARTS.usual_order   ← the only visible change
```

`INSERT OVERWRITE` truncates the target and inserts the staging table's rows in
a **single transaction**. A conversation sees last night's generation or
tonight's and never half of either.

### Why not let the connector own the table

`mode("overwrite")` with the connector's own staging table **drops the target**
and renames a new one into its place. Everything hanging off the target goes with
it:

- **#43's row access policy.** A publish that silently detaches one is a breach
  nobody sees.
- **The column comments.** #45's semantic view retrieves against them, and
  `sql/06_catalogue.sql` is explicit that a comment which has drifted is worse
  than one that is missing, because it still answers.
- **The declared keys.** A text-to-SQL system that has to guess a join key
  guesses.

So the connector writes into `CHIP_CHAT.STAGING` and never anywhere else, and the
target table object is never replaced.

### Why one statement rather than a transaction

`chip_chat.snowflake.load` gets the same guarantee on the developer path with
`BEGIN; TRUNCATE; COPY INTO; COMMIT;`. That shape is unavailable here, and not
for reasons of taste: the connector's `Utils.runQuery` opens **its own JDBC
connection per call**, so a transaction opened in one call is not the session the
next call lands in. Four calls that looked like a transaction would be four
autocommitted statements with a window between each pair.

### Why the staging tables are not beside their targets

`CHIP_CHAT_READ` holds `SELECT ON FUTURE TABLES` in all three serving schemas.
That is right for a declared table — a table #42 or #39 adds later is readable
without anyone re-running a grants file — and exactly wrong for an incoming
generation. An `orders_incoming` in `CHIP_CHAT.ACCOUNTS` would be a complete
unscoped copy of the population, readable by the identity the agent runs as, and
covered by no row access policy, because #43 attaches policies to tables **by
name**.

`CHIP_CHAT.STAGING` is the fourth schema and is not a fourth population. Granted
to `CHIP_CHAT_PUBLISH` and to nobody else, it holds no declared table and is
empty between runs. A staging table is dropped when its swap succeeds — so one
still sitting there is the evidence of a run that stopped, which
`publish_verify.py` reports rather than tidies away.

### What a killed run leaves

The previous generation, in every table that had not swapped yet, and the staging
table of the one that failed. Nothing to reconcile and nothing to roll back by
hand.

`publish.TARGETS` runs in the order `snowflake/sql/` declares the tables, which
is foreign key order. Snowflake enforces none of those keys, so the order buys
nothing at write time. What it buys is that a run killed halfway leaves a
consistent *set* of generations — a catalogue no published order line points
outside of — rather than lines naming items that have not landed.

---

## 3. Stale, with its timestamp — never stale as fresh

RFC-001 §10 is specific about the Databricks job failing: **serve stale gold
marts with their `derived_at`, alert, and do not silently serve stale data as
fresh.** Three obligations, and they are met in three different places.

**Serve stale** needs no mechanism, which is the design working. The swap is one
statement per table, so a failed run leaves the previous generation exactly where
it was and the serving layer keeps answering.

**With its timestamp** is a rule about the projection and is the one that is easy
to lose. `derived_at` is selected out of the gold mart *unchanged*. The publish
never writes `current_timestamp()` into it — if it did, a night when the gold
pipeline failed and the publish copied yesterday's mart again would produce rows
stamped tonight. That is stale data presented as fresh, arriving through the
mechanism meant to prevent it. `publish.CARRIED_ONLY` is the rule as data and
`test_publish.py` holds every projection to it; `publish_verify.py` compares the
published `derived_at` against the gold mart's own.

Observed, after the 2026-08-27 run. Every row of all three visitor-scoped marts
carries a non-null `derived_at`, one distinct value per mart, and the value is
**18:54:51 UTC** — the minute `chip-chat-gold-marts` computed them. The publish
ran between 19:22 and 19:25. Half an hour separates the two timestamps, which is
the whole check: the column says when the numbers were *worked out*, not when
they were *moved*, and a serving layer answering "as of" from it is answering
about the computation.

```sql
-- as CHIP_CHAT_ADMIN, with ALL_VISITORS set: the row access policy applies to
-- every role, so counting a mart is itself a maintenance action.
SELECT MIN(derived_at), MAX(derived_at), COUNT(*), COUNT_IF(derived_at IS NULL)
FROM CHIP_CHAT.MARTS.USUAL_ORDER;
-- 2026-08-27 18:54:51.019911 | same | 500 | 0
```

**Alert** is `email_notifications.on_failure` on the job, not a line in the
notebook — a run that dies before reaching any line of `snowflake_publish.py` (a
cluster that would not start, a task killed at its timeout) is exactly the run
nobody hears about otherwise. One retry first, because a refused JDBC connection
is worth trying again before it wakes somebody, and one is where that stops being
true.

**It fired four times before anyone asked it to.** Standing this job up took
five runs and the first four failed — on the clock check, on the credential, on
#43's row access policy, and on a `Decimal` in the verdict. Every one of them
raised the alert, which is more convincing than the deliberate test below: the
alert was not exercised, it was *used*, on failures nobody had designed. Two of
those are the shape the criterion is really about — a run that died before
touching a table, and a run in which every table had already swapped correctly —
and the same mail went out for both, which is right. What the mail says is
"look", not "this is what is wrong".

To prove the mail arrives end to end on demand, break it on purpose once:

```bash
databricks jobs run-now $(terraform output -raw databricks_publish_job_id) \
  --notebook-params '{"snowflake_url":"nonesuch.snowflakecomputing.com"}'
```

The run fails on the first connection, the notification fires, and nothing was
written — the swap comes after the staging write, which never happened.

---

## 4. How a value crosses

Three transports, and two of them exist because a value that crosses through a
type conversion nobody wrote down is a value that arrives nearly right.

| Transport | Columns | Spark side | Snowflake side |
| --- | --- | --- | --- |
| `DIRECT` | everything else | `col` | `col` |
| `JSON_ARRAY` | the four `ARRAY` columns | `to_json(col)` | `PARSE_JSON(col)::ARRAY` |
| `UTC_TIMESTAMP` | every `TIMESTAMP_NTZ` | `date_format(col, 'yyyy-MM-dd HH:mm:ss.SSSSSS')` | `TO_TIMESTAMP_NTZ(col, 'YYYY-MM-DD HH24:MI:SS.FF6')` |

**The timestamps are the one to read twice.** The connector maps a Spark
`TimestampType` onto `TIMESTAMP_LTZ`, which is a *zoned* type: landing one in a
`TIMESTAMP_NTZ` column applies whatever the session's timezone happens to be.
Every timestamp in this database is UTC and carries no zone, so the value is
formatted to text in Spark and read back with an explicit format in Snowflake,
and no engine's timezone setting decides anything.

That is only correct while Spark is rendering UTC, so the notebook **asserts**
`spark.sql.session.timeZone` rather than setting it. Databricks defaults to UTC,
and a workspace where it is not is a workspace where the timestamps already in
silver were parsed against a different clock — a thing to find out about rather
than to quietly correct on the way past.

The arrays travel the same way `recommender_publish.py` sends its two decimals
across as strings: one fewer thing that has to be true about the connector's
variant support, and legible in the staging table when a publish is being
debugged.

`test_publish.py` finds the array and timestamp columns from
`chip_chat.snowflake.schema` rather than from a list, so a fifth array column
added to the DDL and left on `DIRECT` fails `make ci`.

---

## 5. Standing it up

Snowflake is not managed by the Terraform. `snowflake/sql/` builds the account and
`make snowflake-apply` runs it, so this is four steps rather than one apply.

```bash
# 1. The account, including CHIP_CHAT.STAGING and the publisher's grants.
make snowflake-apply

# 2. A key pair for CHIP_CHAT_PUBLISHER. A TYPE = SERVICE user cannot use a
#    password at all -- Snowflake blocks single-factor password auth for
#    programmatic users. Unencrypted PKCS#8, because that is what the connector's
#    pem_private_key option takes.
openssl genrsa 2048 \
  | openssl pkcs8 -topk8 -inform PEM -nocrypt -out ~/.snowflake/keys/chip_chat_publisher.p8
chmod 600 ~/.snowflake/keys/chip_chat_publisher.p8
openssl rsa -in ~/.snowflake/keys/chip_chat_publisher.p8 -pubout \
  | sed '1d;$d' | tr -d '\n'
# ALTER USER CHIP_CHAT_PUBLISHER SET RSA_PUBLIC_KEY = '<that>';
#   -- by hand, as an operator. It is deliberately not in snowflake/sql:
#   -- a credential in a checked-in file is a credential in everyone's clone.
#
#   Use RSA_PUBLIC_KEY_2 instead if the user already carries a key. Snowflake
#   holds two so that a rotation has an overlap, and the second slot is what
#   makes standing this up a non-destructive act: whoever set the first key
#   holds a private half that setting RSA_PUBLIC_KEY would silently invalidate.
#   SHOW USERS reports has_rsa_public_key and does not say which slot, so
#   assume the first is somebody's until you know otherwise. This is how the
#   2026-08-27 run was authenticated.

# 3. The job. snowflake_account_url is the switch -- empty, which is the default,
#    means neither the publish job nor its verify job is created at all.
#
#    Put it in a tfvars file rather than on the command line. The switch cuts
#    both ways: a later apply that does not carry the variable computes an empty
#    URL, and `count = length(databricks_job.publish) > 0` then plans to DESTROY
#    the publish job, its verify job and both permission sets. `*.tfvars` is
#    gitignored, so this is a local file each operator keeps -- and the failure
#    mode is somebody else's routine apply quietly removing the nightly job.
echo "snowflake_account_url = \"hq72718.us-east-2.aws.snowflakecomputing.com\"" \
  >> infra/terraform/terraform.tfvars
terraform apply

# 4. The private key, into the scope Terraform created empty. Nothing here
#    enters Terraform state.
databricks secrets put-secret \
  "$(terraform output -raw databricks_publish_secret_scope)" publisher-private-key \
  --string-value "$(cat ~/.snowflake/keys/chip_chat_publisher.p8)"
```

Then run it once by hand before the schedule is turned on:

```bash
databricks jobs run-now $(terraform output -raw databricks_publish_job_id)
databricks jobs run-now $(terraform output -raw databricks_publish_verify_job_id)
```

The schedule ships **paused**, like the recommender's, for the reason
`databricks_publish.tf` argues: the criterion is about the publish being
scheduled infrastructure — a cron a person can read and review — and the
guardrail is about this Terraform not starting a cluster nobody asked for. Turn
it on with `databricks_publish_schedule_enabled = true` once the medallion is
loaded and the marts are current. A publish against an empty silver table refuses
rather than emptying the serving layer, but a job that fails every night at seven
is an alert that stops meaning anything.

**07:00 UTC daily.** An hour after #38's weekly re-harvest starts and two before
the recommender's Monday retrain, so a Monday runs harvest, publish, retrain in
that order rather than publishing a catalogue the marts were not computed
against.

---

## 6. What it cost

Issue #39's fifth acceptance criterion, and the honest state of it.

**The publish job measures and reports both halves on every run.** Its output
carries per-table row counts and seconds, the total warehouse-active time, and
the Snowflake credits that implies at the X-Small rate — one credit an hour, with
the sixty-second minimum Snowflake bills per resume applied. That floor is not
trivia: eleven small tables can finish well inside a minute, and an estimate
without it would report less than the account is charged.

**Measured, 2026-08-27.** One full publish against `dbw-chip-chat` and
`hq72718.us-east-2.aws`: eleven tables, **108,157 rows**, in a job that ran for
**176.7 seconds** wall clock — 51 seconds of that a cluster starting, which is
most of the Databricks bill.

| | measured | how |
| --- | --- | --- |
| Rows | 108,157 across 11 tables | the job's own output; the largest is 48,767 order lines |
| Warehouse-active seconds | 112.5 | the job's own measurement: the sum of the eleven per-table spans |
| Snowflake credits, **estimated** | **0.0312** | `publish.warehouse_credits(112.5)` at the X-Small rate |
| Snowflake credits, **billed** | **≈0.066** | the warehouse is warm for the whole run and for 60s after it: 177s + `AUTO_SUSPEND` 60 = 237s of X-Small |
| Cluster time | 176.7s (0.0491 h) | single-node `Standard_F4ads_v7`, 51s setup + 125s execution |
| DBUs | **≈0.060** | 0.0491 h at 1.224 DBU/cluster-hour, derived below |
| Bytes crossed | **≈1.53 MiB** | the eleven tables' compressed size in Snowflake after the run |

**The job's own estimate is about half the billed figure, and the gap is
structural rather than a bug.** `warehouse_credits` counts the seconds the
publish's *statements* were running. Snowflake bills the warehouse from resume
to suspend, which covers the Spark-side gaps between the eleven tables and the
sixty seconds of `AUTO_SUSPEND` after the last one. The estimate is still worth
having — it is what the job can know about itself, on the run, without waiting
three hours for `ACCOUNT_USAGE` — but it is a floor and this table says so.
Either number is comfortably inside `CHIP_CHAT_PUBLISH_MONITOR`'s two-credit
daily quota: a nightly publish spends about **3% of it**.

**Where the DBU rate comes from.** `system.billing.usage` is the authority and
is not readable from this workspace — it needs a Databricks *account* admin,
which the workspace admin is not. So the rate is derived from the subscription's
own Azure bill for the previous day, when only single-node
`Standard_F4ads_v7` job clusters ran: **1.9925 Premium Jobs Compute DBUs over
1.6276 cluster-hours = 1.224 DBU/cluster-hour**, against 19 runs whose durations
the Jobs API reports. At the same day's meter rates — $0.2999 per Jobs Compute
DBU and $0.343 per `F4ads v7` hour — one publish costs about **$0.018 of DBUs
plus $0.017 of VM, so $0.035 on the Databricks side.** Treat the DBU figure as
an upper bound: a run's reported duration is slightly shorter than the cluster's
billed life, so dividing by it inflates the rate.

Confirm both against the systems of record once they catch up:

```sql
-- Snowflake, as CHIP_CHAT_ADMIN. ACCOUNT_USAGE lags a run by up to three hours,
-- which is why the job estimates rather than reading this back.
USE ROLE CHIP_CHAT_ADMIN;
SELECT start_time, credits_used, credits_used_compute
FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
WHERE warehouse_name = 'CHIP_CHAT_PUBLISH_WH'
ORDER BY start_time DESC LIMIT 24;
```

```sql
-- Databricks, in the workspace. The billable usage system table is what settles
-- the DBU figure; the job's own duration is the other half.
SELECT usage_date, sku_name, SUM(usage_quantity) AS dbus
FROM system.billing.usage
WHERE usage_metadata.job_id = '<the publish job id>'
GROUP BY ALL ORDER BY usage_date DESC;
```

```sql
-- Azure, for the DBU half, once Cost Management has the day. This is what the
-- rate above was derived from for 2026-08-26 and is the way to check it:
--   az rest --method post --url ".../providers/Microsoft.CostManagement/query
--     ?api-version=2023-11-01" --body @query.json
-- grouped by Meter, filtered to MeterCategory 'Azure Databricks'.
```


What can be said before the run, from the shape of the workload: the publish
moves the whole population — around fifty thousand order lines is the largest
table — once a night, off the conversational path entirely. The publish
warehouse's resource monitor caps it at two credits a day and suspends **at**
its quota rather than above it, because a suspended publish costs a stale mart
until tomorrow while a suspended serving warehouse costs the demo
mid-conversation. `docs/snowflake-account.md` has that asymmetry in full.

### Egress, now that this is cross-cloud

[#104](https://github.com/gganssle/chip_chat/issues/104) decided that Snowflake
stays on **AWS us-east-2** while Databricks is on **Azure East US 2**, and put
one item against this issue: *account for cross-cloud transfer; verify egress
cost.* Verified, on both sides.

**Snowflake charges nothing.** Its
[data transfer documentation](https://docs.snowflake.com/en/user-guide/cost-understanding-data-transfer)
is explicit — *"Snowflake does not charge data ingress fees"* — and the per-byte
fee applies to data moving **out** of a Snowflake account into another region or
cloud. This job only writes. The caveat in that same page is the one that
matters here and points back at Azure: the *source* cloud may charge for
sending, and that is where the bill is.

**Azure charges, but not through the meter you would look for.** Three meters
could apply, and the retail price API and this subscription's own Cost
Management data settle which:

| Meter | Rate | Applies here? |
| --- | --- | --- |
| Bandwidth, `Standard Data Transfer Out` (Internet) | **$0 up to 100 GB/month**, then $0.087/GB | Yes, and it bills zero. The subscription's Cost Management rows for it read `Standard Data Transfer Out - Free`. |
| Bandwidth, `Standard Inter-Region Data Transfer` | $0.02/GB | No. That is Azure-to-Azure — the eastus2→eastus hop `service-inventory.md` measured for AI Search. Snowflake is not in Azure. |
| NAT Gateway, `Standard Data Processed` | **$0.045/GB** | **Yes, and this is the one that actually charges.** |

The NAT gateway is the finding. Databricks' secure cluster connectivity puts a
managed VNet and a NAT gateway in `rg-chip-chat-databricks-managed`, and
**every byte a cluster sends to anything outside Azure passes through it** — so
the cross-cloud publish is billed at $0.045/GB of data processed, which is more
than twice the inter-region rate that would have applied had Snowflake been in
Azure at all, and it is charged from the first byte rather than after a free
allowance. It is also charged in both directions and on traffic that never
leaves Azure, so it is not an egress meter and does not appear when you go
looking for one.

And it is dwarfed by the gateway's own clock. `Standard Gateway` is $0.045 per
**hour**, and the meter runs whether or not a cluster exists: 17.17 hours on
2026-08-26 for $0.77, against $0.03 of data processed on the same day. The
gateway is a fixed cost of having a Databricks workspace, and the publish's
share of it is the fraction of an hour the job runs.

So the answer to *does egress cost anything* is **yes, about five cents per
hundred gigabytes, and this publish does not move hundreds of gigabytes.** The
eleven tables occupy about a fifth of a megabyte in Snowflake after the run; the
Parquet the connector stages is the same order. At $0.045/GB the transfer is a
rounding error against the $0.77 a day the gateway costs for existing, and both
are inside #88's $150/month guardrail with several orders of magnitude to spare.

The number to watch is not this job. It is anything that would push the
subscription's *internet* egress past 100 GB in a month, because that is where a
free meter becomes an $0.087/GB one — and the nightly publish contributes about
0.0002% of that allowance.

---

## 7. Open, and whose

**Settled by the first run: a row access policy must not filter
`CHIP_CHAT_PUBLISH`, and #43's did.** This section used to be a warning. The
2026-08-27 publish is what turned it into a finding, and the finding is
better-shaped than the warning was.

`CHIP_CHAT.ACCOUNTS.VISITOR_ISOLATION` is attached to nine tables, six of which
this job replaces. It compares `demo_id` against a session variable the
publisher never sets, and row access policies apply to every role — Snowflake
has no owner exemption — so the publisher sees zero rows in every table it owns.
The run staged 18,898 orders, swapped them, counted the target, read **0**, and
stopped:

```
CHIP_CHAT.ACCOUNTS.orders holds 0 rows after the swap and staging held 18898.
If the count is HIGHER, the truncate half of INSERT OVERWRITE did not remove
every row -- check whether a row access policy on the table filters
CHIP_CHAT_PUBLISH, which it must not. If it is LOWER, the swap did not land.
```

Which is the message doing its job: it names the cause of the failure it is
looking at, and the cause was the one it names.

**The half of the warning that turned out to be wrong is worth keeping.** The
paragraph above used to say the truncate half of `INSERT OVERWRITE` *might*
leave rows the publisher cannot see, showing up as duplicated primary keys. It
does not. `ORDERS` held 2,277 rows before the swap and 18,898 after — every one
of the 2,277 was removed by a role that could not select a single one of them.
`INSERT OVERWRITE` truncates the table rather than deleting visible rows, and a
row access policy does not narrow it. `publish_verify.py` counts duplicate keys
on every published table anyway, because the cost of the check is nothing and
the cost of being wrong about this is a serving layer holding two generations
at once.

**What was changed, and by whom it should be owned.** The policy now carries a
third clause:

```sql
OR CURRENT_ROLE() = 'CHIP_CHAT_PUBLISH'
```

applied live with `ALTER ROW ACCESS POLICY ... SET BODY`, because a
`CREATE OR REPLACE` is refused while a policy is attached to anything. It is a
batch role held only by the Databricks job's service user, reachable from no
session the model or the app can open, and it reads rather than escapes: the
publisher already writes these tables wholesale.

**It is not yet in `snowflake/sql/10_policies.sql`, and that file is #43's.**
The account is ahead of the checked-in SQL, which means the next
`make snowflake-apply` reverts it and the next nightly publish fails at the
first table. The durable fix is one line beside line 133 of that file, with the
reason beside it.

**#47 and this job both write the account tables.** #47 restores the synthetic
sandbox to its generated state on a schedule; this job puts the generated state
there in the first place, and a publish *is* a restore. The two were not in
conflict while nothing wrote `ACCOUNTS.orders` at runtime — #46's action lane
was not landed. It is now, "tonight's publish erases the order a visitor placed
this afternoon" is a real sentence, and #47 has decided it.

**Decided: no. A visitor's live rows survive until that visitor ages out.**
[docs/demo-reset.md](demo-reset.md) §6 is the argument, and it is #9's: a cookie
means Sam comes back tomorrow to the order they placed today, and a publish that
erases it overnight makes the persistence decision true only within a calendar
day. The guess recorded here — "it is a demo sandbox and the answer is probably
yes" — was the other way, and was wrong for that reason.

So this job is what has to change: the swap for the three ACCOUNTS tables has to
leave rows above the `ord-9000001` / `loy-9000001` band alone. That is not a
small edit — `INSERT OVERWRITE` being one statement is exactly what buys the
atomicity §2 argues for, and preserving a band means it stops being one
statement over a connector that opens a new JDBC session per call. It is cc-fxf4
and it is this job's, not #47's. Until it lands the reset still
behaves correctly — it deletes what is there — and `orders_deleted` reads zero
most mornings for a reason that is not "nothing happened".
