# Databricks notebook source
# MAGIC %md
# MAGIC # The nightly publish — lakehouse to Snowflake
# MAGIC
# MAGIC Issue [#39](https://github.com/gganssle/chip_chat/issues/39), and the
# MAGIC whole of the `chip-chat-publish` job. Eleven tables cross: the four
# MAGIC harvested catalogue tables, the three synthetic account tables the marts
# MAGIC are computed from, and the four gold marts. `publish.py` declares what
# MAGIC crosses and by what statement; this notebook is the loop.
# MAGIC
# MAGIC **Two writes per table, and only the second one is visible.** The
# MAGIC connector writes an incoming generation into `CHIP_CHAT.STAGING`, which
# MAGIC no serving role can reach, and then one `INSERT OVERWRITE` makes it live.
# MAGIC Truncate and insert inside a single transaction, so a conversation
# MAGIC querying mid-publish sees last night's generation or tonight's and never
# MAGIC half of either. The target table is never dropped or renamed, so #43's
# MAGIC row access policy, #45's column comments and the declared keys stay
# MAGIC attached to it. `publish.py`'s header argues both at length.
# MAGIC
# MAGIC **What a failure leaves behind.** The previous generation, in every table
# MAGIC that had not swapped yet, and the staging table of the one that failed.
# MAGIC Nothing to reconcile and nothing to roll back by hand. RFC-001 §10 then
# MAGIC asks the serving layer to say so rather than to serve the stale rows as
# MAGIC fresh, which is what `derived_at` is for — and this job carries that
# MAGIC column across unchanged rather than restamping it, so a mart republished
# MAGIC from an unchanged gold table still reports the night it was *computed*.
# MAGIC
# MAGIC **The alert is the job's, not the notebook's.** A notebook that fails
# MAGIC cannot email anybody; `infra/terraform/databricks_publish.tf` declares
# MAGIC `email_notifications.on_failure`, which fires on a run that died before
# MAGIC reaching any line of this file. Failing loudly here is what turns that
# MAGIC into a useful message.
# MAGIC
# MAGIC **Parameters**, all supplied by the job: `catalog`, `lib_path`,
# MAGIC `snowflake_url`, `snowflake_user`, `secret_scope`.

# COMMAND ----------

import json
import sys
import time

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog")
dbutils.widgets.text("lib_path", "", "Workspace directory holding publish.py")
dbutils.widgets.text("snowflake_url", "", "Snowflake account URL")
dbutils.widgets.text("snowflake_user", "CHIP_CHAT_PUBLISHER", "Snowflake user")
dbutils.widgets.text("secret_scope", "chip-chat-snowflake", "Databricks secret scope")

LIB_PATH = dbutils.widgets.get("lib_path")
if LIB_PATH not in sys.path:
    sys.path.insert(0, LIB_PATH)

import catalog  # noqa: E402
import publish  # noqa: E402

CATALOG = dbutils.widgets.get("catalog")
URL = dbutils.widgets.get("snowflake_url")
USER = dbutils.widgets.get("snowflake_user")
SCOPE = dbutils.widgets.get("secret_scope")

if CATALOG != catalog.CATALOG:
    raise ValueError(
        f"the job is configured for catalog {CATALOG!r} but the layout module "
        f"names {catalog.CATALOG!r}; one of the two has drifted"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## The clock, before anything moves
# MAGIC
# MAGIC Every timestamp in the serving layer is UTC and carries no zone. The
# MAGIC `UTC_TIMESTAMP` transport formats an instant to a string in Spark and
# MAGIC reads it back with an explicit format in Snowflake, so neither engine's
# MAGIC timezone setting decides anything — *provided* Spark is rendering UTC.
# MAGIC
# MAGIC Asserted rather than set. Databricks defaults to UTC, and a workspace
# MAGIC where it is not is one where the timestamps already in silver were parsed
# MAGIC against a different clock. Quietly correcting it here would publish rows
# MAGIC that disagree with the lakehouse they came from.

# COMMAND ----------

session_timezone = spark.conf.get("spark.sql.session.timeZone")
if not publish.is_utc(session_timezone):
    raise AssertionError(
        f"spark.sql.session.timeZone is {session_timezone!r} and this publish "
        f"requires {publish.SPARK_TIMEZONE!r}, spelled any of "
        f"{publish.UTC_SPELLINGS}. Every timestamp in CHIP_CHAT is UTC and "
        "carries no zone, and the silver tables underneath were parsed against "
        "this setting too -- so this is a workspace to look at rather than a "
        "value to override here."
    )

# The secret holds the .p8 file whole, which is what an operator can check
# against the file it came from. The connector wants the base64 body alone; see
# `publish.pem_body`, which is where the second live publish died.
PRIVATE_KEY = publish.pem_body(
    dbutils.secrets.get(scope=SCOPE, key=publish.PRIVATE_KEY_SECRET)
)

# Staging is the only schema this job writes with the connector. The serving
# tables are reached by fully qualified name inside `publish.swap`, never by a
# session default -- a default that is correct today is a default somebody
# relies on tomorrow.
OPTIONS = publish.options(URL, USER, publish.STAGING_SCHEMA)
CONNECT = dict(OPTIONS, pem_private_key=PRIVATE_KEY)

print(f"catalog        {CATALOG}")
print(f"account        {URL}")
print(f"user           {USER}")
print(f"role           {publish.PUBLISH_ROLE}")
print(f"warehouse      {publish.PUBLISH_WAREHOUSE}")
print(f"staging        {publish.DATABASE}.{publish.STAGING_SCHEMA}")
print(f"publishes      {len(publish.TARGETS)} tables")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Running a statement on the other side
# MAGIC
# MAGIC `Utils.runQuery` is the connector's own escape hatch: it opens a JDBC
# MAGIC connection with the same options the writes use and runs one statement.
# MAGIC One statement is also its limit, which is exactly why the swap is a
# MAGIC single `INSERT OVERWRITE` rather than the `BEGIN; TRUNCATE; INSERT;
# MAGIC COMMIT;` that `chip_chat.snowflake.load` uses on the developer path — a
# MAGIC transaction opened in one call is not the session the next call lands in.
# MAGIC
# MAGIC Reads come back through the data source rather than through `runQuery`,
# MAGIC which returns a JDBC result set nothing in Python can hold.

# COMMAND ----------


def run(statement):
    """Run one statement on Snowflake as CHIP_CHAT_PUBLISH."""
    sc._jvm.net.snowflake.spark.snowflake.Utils.runQuery(CONNECT, statement)


def scalar(query):
    """Return the single value of a one-row, one-column query."""
    frame = spark.read.format(publish.SOURCE).options(**CONNECT).option("query", query)
    return frame.load().first()[0]


def landed(target):
    """Return how many rows `target` holds in Snowflake, counted off the metadata.

    NOT `SELECT COUNT(*)`. Three of the eleven tables this job publishes carry
    `demo_id` and so wear #43's `visitor_isolation` row access policy, and that
    policy is default-deny: a session that has bound no visitor reads zero rows.
    The publisher binds no visitor -- it replaces these tables wholesale, for
    every visitor at once -- so a `COUNT(*)` here comes back 0 against a staging
    count of 18,898 and aborts the run after the swap has already happened.

    The obvious fix is an OR clause in the policy naming CHIP_CHAT_PUBLISH, and
    it is the wrong one. `tests/test_row_access_policies.py` refuses exactly
    that, on the grounds that a lane role appearing in a policy body is a lane
    role the policy has stopped applying to; and #43's acceptance criterion is
    that an unset session variable returns zero rows from every visitor-scoped
    table, for every role, not for every role but one.

    `INFORMATION_SCHEMA.TABLES.ROW_COUNT` is metadata about the table rather
    than a read of its rows, so no row access policy filters it. Measured on
    the live account: as CHIP_CHAT_READ with nothing bound, `COUNT(*)` on
    `ACCOUNTS.ORDERS` returns 0 and this returns 18,898. That is the whole
    argument -- the publisher gets its number, and the isolation guarantee is
    not touched to give it.

    Coerced to `int` for the same reason the `COUNT(*)` was: the connector maps
    a Snowflake `NUMBER(18,0)` to a `Decimal`, which compares correctly against
    the staging count and then refuses to be serialised. The first publish that
    reached the end of the loop died on `TypeError: Object of type Decimal is
    not JSON serializable` in `dbutils.notebook.exit`, after all eleven tables
    had swapped -- every row where it should be, and the run marked FAILED,
    which is the worst way for this job to be wrong.
    """
    return int(scalar(publish.row_count(target)))


# COMMAND ----------

# MAGIC %md
# MAGIC ## One table
# MAGIC
# MAGIC Read, check, stage, swap, count, drop. The order of the last three is the
# MAGIC whole design: the staging table is dropped only after its swap succeeded,
# MAGIC so a staging table still sitting in `CHIP_CHAT.STAGING` is a run that did
# MAGIC not finish and is worth looking inside.
# MAGIC
# MAGIC The null check runs in Spark, before anything is written. A `NULL result
# MAGIC in a non-nullable column` from Snowflake is a correct refusal that names
# MAGIC neither the column nor the table, arriving after the upload has been paid
# MAGIC for.
# MAGIC
# MAGIC The count is compared against the staging table and against the source,
# MAGIC and both comparisons matter. Staging against source catches a connector
# MAGIC write that dropped rows. Target against staging catches a swap that did
# MAGIC not replace what it was supposed to — which is also the check that would
# MAGIC fail first if a row access policy attached to the target ever filtered
# MAGIC this role. It is meant to fail loudly if it does: a publisher that cannot
# MAGIC see what it just wrote cannot verify a publish at all.

# COMMAND ----------


def publish_one(target):
    """Publish one table and return what it did."""
    started = time.monotonic()
    source = catalog.table(target.layer, target.stream, target.table)
    frame = spark.sql(publish.select(target, catalog.table))
    rows = frame.count()

    print(f"→ {target.qualified}")
    print(f"  from {source}: {rows} rows")

    if rows == 0:
        raise AssertionError(
            f"{source} is empty, and publishing it would empty "
            f"{target.qualified} for every conversation until tomorrow night. "
            "Run the pipeline that fills it before the publish, not after."
        )

    missing = {
        column: frame.where(f"{column} IS NULL").count()
        for column in publish.required_columns(target)
    }
    broken = {column: count for column, count in missing.items() if count}
    if broken:
        raise AssertionError(
            f"{source} cannot fill {target.qualified}: "
            + ", ".join(f"{column} is null on {n} rows" for column, n in broken.items())
            + ". The target declares those columns NOT NULL."
        )

    staging = publish.staging(target)
    (
        frame.write.format(publish.SOURCE)
        .options(**CONNECT)
        .option("dbtable", staging)
        .mode("overwrite")
        .save()
    )
    staged = int(scalar(f"SELECT COUNT(*) FROM {staging}"))
    if staged != rows:
        raise AssertionError(
            f"{staging} holds {staged} rows and {source} produced {rows}. "
            "Nothing has been swapped, so the serving table is untouched."
        )

    run(publish.swap(target))
    live = landed(target)
    if live != rows:
        raise AssertionError(
            f"{target.qualified} holds {live} rows after the swap and staging "
            f"held {rows}. If the count is HIGHER, the truncate half of INSERT "
            "OVERWRITE did not remove every row -- check whether a row access "
            f"policy on the table filters {publish.PUBLISH_ROLE}, which it must "
            "not. If it is LOWER, the swap did not land."
        )

    run(publish.drop_staging(target))
    elapsed = time.monotonic() - started
    print(f"  {live} rows live in {elapsed:.1f}s")
    return {"table": target.qualified, "rows": live, "seconds": round(elapsed, 1)}


# COMMAND ----------

# MAGIC %md
# MAGIC ## Every table, in foreign key order
# MAGIC
# MAGIC `publish.TARGETS` is ordered the way `snowflake/sql/` declares the
# MAGIC tables: `menu_items` before the two that reference it, `orders` before
# MAGIC `order_items`. Snowflake enforces none of those keys, so the order buys
# MAGIC nothing at write time. What it buys is that a run killed halfway leaves a
# MAGIC consistent *set* of generations — a catalogue no published order line
# MAGIC points outside of — rather than lines naming items that have not landed.
# MAGIC
# MAGIC It stops at the first failure, unlike the verification notebooks, and for
# MAGIC the opposite reason: those are asking questions and this one is changing
# MAGIC what a conversation reads. Carrying on past a table that would not land
# MAGIC would publish the rest of the set against a generation that is not there.

# COMMAND ----------

started = time.monotonic()
published = []
for target in publish.TARGETS:
    published.append(publish_one(target))
active_seconds = time.monotonic() - started

# COMMAND ----------

# MAGIC %md
# MAGIC ## What it cost
# MAGIC
# MAGIC Issue #39's fifth acceptance criterion. Two halves, and this notebook can
# MAGIC only measure one of them honestly.
# MAGIC
# MAGIC **Snowflake** is estimated from the warehouse's own published rate:
# MAGIC X-Small, one credit an hour, with the sixty-second minimum Snowflake
# MAGIC bills per resume applied — an eleven-table publish finishes well inside
# MAGIC that minimum, so an estimate without the floor would report less than the
# MAGIC account is charged. The billed figure lands in `ACCOUNT_USAGE` within
# MAGIC about three hours and `CHIP_CHAT_PUBLISH` deliberately cannot read it;
# MAGIC `docs/nightly-publish.md` has the query to run as `CHIP_CHAT_ADMIN`.
# MAGIC
# MAGIC **Databricks** is the run's own wall clock on a single-node job cluster.
# MAGIC The DBU figure is that duration times the node type's rate, and the
# MAGIC billable usage system table is what settles it. Printed here as seconds
# MAGIC rather than multiplied by a rate this file would have to hardcode and
# MAGIC nobody would re-check.

# COMMAND ----------

estimated_credits = publish.warehouse_credits(active_seconds)
rows = sum(item["rows"] for item in published)

print(f"  {len(published)} tables, {rows} rows")
print(f"  {active_seconds:.1f}s of publish warehouse time")
print(f"  ~{estimated_credits:.4f} Snowflake credits at the X-Small rate")
print("  Databricks: this run's cluster duration times the node type's DBU rate")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The verdict

# COMMAND ----------

dbutils.notebook.exit(
    json.dumps(
        {
            "tables": published,
            "rows": rows,
            "seconds": round(active_seconds, 1),
            "estimated_snowflake_credits": round(estimated_credits, 4),
        },
        sort_keys=True,
    )
)
