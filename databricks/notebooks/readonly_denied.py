# Databricks notebook source
# MAGIC %md
# MAGIC # The read-only principal is read-only
# MAGIC
# MAGIC Issue #32's third acceptance criterion: "Permissions verified by attempting
# MAGIC a write with the read-only principal and being refused."
# MAGIC
# MAGIC A refusal is only interesting if the same principal can read, so this
# MAGIC checks both directions. It runs as `chip-chat-readonly` — the identity
# MAGIC created in `infra/terraform/databricks_catalog.tf` with `USE_CATALOG`,
# MAGIC `USE_SCHEMA` and `SELECT`, and deliberately without `MODIFY`,
# MAGIC `CREATE_TABLE` or `WRITE_FILES`.
# MAGIC
# MAGIC Every write below is expected to fail. The notebook fails if one succeeds,
# MAGIC which is the only outcome worth alerting on: a grant that quietly widened.
# MAGIC
# MAGIC Run `lineage_probe` first — it leaves the tables this reads.

# COMMAND ----------

from collections.abc import Callable

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog name")
dbutils.widgets.text("raw_uri", "", "abfss:// URI of the raw container")
dbutils.widgets.text("run_id", "", "Job run id, supplied by the job as {{job.run_id}}")

catalog = dbutils.widgets.get("catalog")
raw_uri = dbutils.widgets.get("raw_uri").rstrip("/")
run_id = dbutils.widgets.get("run_id") or "manual"

GOLD = f"{catalog}.gold_harvested.lineage_probe"

# The two failures Unity Catalog produces for "you may not do that". Anything
# else -- a syntax error, a missing table, a cluster problem -- would also raise,
# and counting it as a refusal would turn this notebook into one that passes for
# the wrong reason.
DENIAL_MARKERS = (
    "PERMISSION_DENIED",
    "INSUFFICIENT_PERMISSIONS",
    "does not have",
    "Unauthorized",
    "403",
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Reading works
# MAGIC
# MAGIC If this fails the rest proves nothing: a principal with no access at all
# MAGIC would refuse every write below too, and would be a different bug.

# COMMAND ----------

# `row[0]` rather than `row.databaseName`: the column SHOW SCHEMAS returns has
# been called both `databaseName` and `namespace` across runtimes, and the
# position has not moved.
schemas = [row[0] for row in spark.sql(f"SHOW SCHEMAS IN {catalog}").collect()]
print(f"schemas visible in {catalog}: {sorted(schemas)}")

expected = {
    f"{layer}_{stream}"
    for layer in ("bronze", "silver", "gold")
    for stream in ("harvested", "synthetic")
}
missing = expected - set(schemas)
if missing:
    raise AssertionError(f"the reader cannot see {sorted(missing)} -- check the grants")

rows = spark.table(GOLD).collect()
print(f"read {len(rows)} rows from {GOLD}")
if not rows:
    raise AssertionError(
        f"{GOLD} is empty or absent. Run the chip-chat-uc-lineage job first: "
        "this notebook reads what that one writes."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Writing does not

# COMMAND ----------


def expect_refusal(what: str, attempt: Callable[[], object]) -> str:
    """Run ``attempt``, require it to be refused, and return the refusal.

    Raises:
        AssertionError: If ``attempt`` succeeds, or fails for a reason that is
            not a permission refusal.
    """
    try:
        attempt()
    # A bare `Exception`: the class Spark raises for a refusal is the platform's
    # business and has changed before. The text is what Unity Catalog promises.
    except Exception as error:
        message = str(error)
        if not any(marker in message for marker in DENIAL_MARKERS):
            raise AssertionError(
                f"{what} failed, but not with a permission error. "
                f"That is a different bug:\n{message}"
            ) from error
        first_line = message.strip().splitlines()[0]
        print(f"refused: {what}\n    {first_line}")
        return first_line
    raise AssertionError(
        f"{what} SUCCEEDED. The read-only principal can write, which means a "
        "grant in infra/terraform/databricks_catalog.tf has widened."
    )


refusals = {
    "INSERT into a gold table": expect_refusal(
        "INSERT into a gold table",
        lambda: spark.sql(
            f"INSERT INTO {GOLD} VALUES ('Refused', 0, 0.0, current_timestamp())"
        ),
    ),
    "CREATE TABLE in gold_synthetic": expect_refusal(
        "CREATE TABLE in gold_synthetic",
        lambda: spark.sql(
            f"CREATE TABLE {catalog}.gold_synthetic.readonly_probe_{run_id} (x int)"
        ),
    ),
    "DROP TABLE in gold_harvested": expect_refusal(
        "DROP TABLE in gold_harvested",
        lambda: spark.sql(f"DROP TABLE {GOLD}"),
    ),
    "CREATE SCHEMA in the catalog": expect_refusal(
        "CREATE SCHEMA in the catalog",
        lambda: spark.sql(f"CREATE SCHEMA {catalog}.readonly_probe_{run_id}"),
    ),
}

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. And neither does writing to the landing zone
# MAGIC
# MAGIC The catalog is not the only way out. The reader holds `READ_FILES` on the
# MAGIC `chip-chat-raw` external location and not `WRITE_FILES`, because a
# MAGIC principal that could still write files would not be read-only in any sense
# MAGIC worth verifying.

# COMMAND ----------

if raw_uri:
    refusals["write a file to the raw landing zone"] = expect_refusal(
        "write a file to the raw landing zone",
        lambda: dbutils.fs.put(f"{raw_uri}/_readonly_probe/{run_id}.txt", "nope", True),
    )
else:
    print("raw_uri not supplied; skipping the external-location check")

# COMMAND ----------

print(f"ok: {len(refusals)} write attempts, {len(refusals)} refusals, 0 successes")
for what in refusals:
    print(f"  - {what}")
