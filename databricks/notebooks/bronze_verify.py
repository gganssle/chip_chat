# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze ingestion — the acceptance criteria, as assertions
# MAGIC
# MAGIC Issue [#33](https://github.com/gganssle/chip_chat/issues/33) has four
# MAGIC acceptance criteria and every one of them is a claim about a live system.
# MAGIC This notebook is those claims, run as the `chip-chat-bronze-verify` job,
# MAGIC so SUCCESS means the assertions passed rather than the notebook merely
# MAGIC finishing. Same reasoning as the two Unity Catalog probes gh-32 left
# MAGIC behind, and the same shape.
# MAGIC
# MAGIC 1. **Both streams land from a cold start.** Every declared table
# MAGIC    exists and holds rows, in both schemas.
# MAGIC 2. **Re-running is idempotent.** `COUNT(*)` equals
# MAGIC    `COUNT(DISTINCT identity)` on every table, where the identity is the
# MAGIC    one the record's own documentation gives it. A duplicated identity is
# MAGIC    a file that was read twice.
# MAGIC 3. **A malformed input is quarantined and the update still completes.**
# MAGIC    The quarantine views resolve, and with `expect_quarantined=true` they
# MAGIC    are required to be non-empty.
# MAGIC
# MAGIC The fourth criterion — that the ingestion finishes inside the cluster's
# MAGIC auto-terminate window — is the pipeline's own business rather than this
# MAGIC notebook's: pipeline compute has no way to outlive its update. Read the
# MAGIC duration off the update itself.
# MAGIC
# MAGIC What this notebook adds beyond the criteria is the ingestion metadata the
# MAGIC issue asks for in its scope: `source_url` and `harvested_at` survive onto
# MAGIC every corpus row, and every row everywhere knows which file it came from.
# MAGIC
# MAGIC Run it after the pipeline, from the `chip-chat-bronze-verify` job. It
# MAGIC reads and never writes, so it is safe to run at any time.

# COMMAND ----------

import json
import sys

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog")
dbutils.widgets.text("lib_path", "", "Workspace directory holding bronze.py")
dbutils.widgets.text(
    "expect_quarantined",
    "false",
    "Require the quarantine to be non-empty (after seeding a malformed file)",
)

sys.path.insert(0, dbutils.widgets.get("lib_path"))

import bronze  # noqa: E402
import catalog  # noqa: E402

CATALOG = dbutils.widgets.get("catalog")
EXPECT_QUARANTINED = dbutils.widgets.get("expect_quarantined").lower() == "true"

failures = []


def check(condition, message):
    """Record a failed claim rather than raising on the first one.

    A run that stops at the first failure tells you one thing per cluster
    start, and a cluster start here is four minutes and a few cents.
    """
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 1 — both streams land from a cold start

# COMMAND ----------

counts = {}

for stream in bronze.STREAMS:
    print(f"{catalog.schema('bronze', stream)}")
    for candidate in bronze.sources_for(stream):
        name = catalog.table("bronze", candidate.stream, candidate.table)
        try:
            rows = spark.table(name).count()
        except Exception as error:  # the message is the finding
            check(False, f"{name} is not readable: {error}")
            continue
        counts[name] = rows
        check(rows > 0, f"{name} holds {rows} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ingestion metadata, on every row
# MAGIC
# MAGIC The scope bullet, not a criterion: "source path, ingestion timestamp, and
# MAGIC the `source_url` / `harvested_at` captured at harvest time". The first two
# MAGIC are on every table; the last two are on `raw_documents`, because that is
# MAGIC the only place they honestly exist — a response body does not carry the
# MAGIC URL it was fetched from, and inventing one would be worse than joining.

# COMMAND ----------

for name in counts:
    frame = spark.table(name)
    columns = set(frame.columns)
    missing = {bronze.INGESTED_AT, bronze.SOURCE_PATH} - columns
    check(not missing, f"{name} carries its path and ingestion time")
    if missing:
        continue
    blank = frame.where(
        f"{bronze.SOURCE_PATH} IS NULL OR {bronze.INGESTED_AT} IS NULL"
    ).count()
    check(blank == 0, f"{name} has {blank} rows with no provenance")

# Quarantined rows are exempt, and the exemption is the promise rather than a
# weakening of it: a document that did not parse has no citation to carry, and
# bronze keeps it anyway. What is asserted is that every row which DID parse
# carries both fields — so a missing citation is always either a bad document
# sitting in the quarantine, or a failure here.
documents = catalog.table("bronze", "harvested", "raw_documents")
uncited = (
    spark.table(documents)
    .where(f"NOT {bronze.QUARANTINED} AND (source_url IS NULL OR harvested_at IS NULL)")
    .selectExpr(bronze.SOURCE_PATH, bronze.RESCUED_DATA)
    .limit(5)
    .collect()
)
check(
    not uncited,
    f"{documents}: every parsed row carries a citation"
    + ("" if not uncited else f"; offenders {[row[0] for row in uncited]}"),
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 2 — re-running is idempotent
# MAGIC
# MAGIC Auto Loader records the files it has consumed, so a second update over an
# MAGIC unchanged landing zone appends nothing. What that looks like from here is
# MAGIC that a row's identity still appears once. Run the pipeline twice and then
# MAGIC run this: a duplicated identity is a file that was read again.

# COMMAND ----------

for stream in bronze.STREAMS:
    for candidate in bronze.sources_for(stream):
        name = catalog.table("bronze", candidate.stream, candidate.table)
        if name not in counts:
            continue
        identity = ", ".join(candidate.identity)
        distinct = spark.table(name).selectExpr(*candidate.identity).distinct().count()
        check(
            distinct == counts[name],
            f"{name}: {counts[name]} rows, {distinct} distinct ({identity})",
        )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 3 — malformed input is quarantined, not dropped
# MAGIC
# MAGIC The update completing at all is half of it, and the job that ran this
# MAGIC notebook is the other half: this cell only runs if the pipeline finished.
# MAGIC What is left is that the bad record is somewhere a person can find it.

# COMMAND ----------

quarantined = 0

for stream in bronze.STREAMS:
    name = catalog.table("bronze", stream, bronze.QUARANTINE_TABLE)
    try:
        rows = spark.table(name).count()
    except Exception as error:  # the message is the finding
        check(False, f"{name} is not readable: {error}")
        continue
    quarantined += rows
    check(True, f"{name} holds {rows} rows")
    if rows:
        spark.table(name).show(10, truncate=120)

if EXPECT_QUARANTINED:
    check(
        quarantined > 0,
        f"a malformed input was seeded and {quarantined} rows were quarantined",
    )
else:
    print(
        f"  note  {quarantined} rows quarantined; not asserted (expect_quarantined=false)"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## The verdict

# COMMAND ----------

print()
for name in sorted(counts):
    print(f"{counts[name]:>8}  {name}")
print()

if failures:
    raise AssertionError(
        f"{len(failures)} bronze claims failed:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )

# The verdict, machine-readable. A job's notebook output is what the run API
# returns, so the counts this asserted on are readable without opening the
# workspace -- which is what lets a run be quoted in a document.
dbutils.notebook.exit(
    json.dumps({"tables": counts, "quarantined": quarantined}, sort_keys=True)
)
