# Databricks notebook source
# MAGIC %md
# MAGIC # Lineage probe
# MAGIC
# MAGIC Issue #32 asks for lineage "visible from a raw file through to a gold mart".
# MAGIC This is that sentence, executable.
# MAGIC
# MAGIC It writes one small document into the ADLS landing zone, reads it into
# MAGIC `bronze_harvested`, conforms it into `silver_harvested`, aggregates it into
# MAGIC `gold_harvested`, and then asks Unity Catalog to describe what just
# MAGIC happened. The assertion is on the answer Unity Catalog gives, not on the
# MAGIC code above it — the point is that the platform recorded the chain, not
# MAGIC that this notebook believes it did.
# MAGIC
# MAGIC It runs as the jobs service principal, so it is also a proof that the
# MAGIC grants in `infra/terraform/databricks_catalog.tf` are sufficient to build a
# MAGIC medallion: every statement below is one that #33 and #34 will issue too.
# MAGIC
# MAGIC The three tables it leaves behind are the evidence. Lineage is a property
# MAGIC of objects that exist, so dropping them would delete the thing this
# MAGIC notebook was run to demonstrate. Set `cleanup` to `true` to remove them
# MAGIC anyway.

# COMMAND ----------

import json
import time
import urllib.parse
import urllib.request

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog name")
dbutils.widgets.text("raw_uri", "", "abfss:// URI of the raw container")
dbutils.widgets.text("run_id", "", "Job run id, supplied by the job as {{job.run_id}}")
dbutils.widgets.dropdown("cleanup", "false", ["false", "true"], "Drop the probe tables")

catalog = dbutils.widgets.get("catalog")
raw_uri = dbutils.widgets.get("raw_uri").rstrip("/")
run_id = dbutils.widgets.get("run_id") or "manual"
cleanup = dbutils.widgets.get("cleanup") == "true"

if not raw_uri.startswith("abfss://"):
    raise ValueError(
        f"raw_uri must be an abfss:// URI on the /dfs endpoint, got {raw_uri!r}."
    )

BRONZE = f"{catalog}.bronze_harvested.lineage_probe"
SILVER = f"{catalog}.silver_harvested.lineage_probe"
GOLD = f"{catalog}.gold_harvested.lineage_probe"

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. A raw file
# MAGIC
# MAGIC Written with `dbutils.fs.put` rather than with Spark, so that the only
# MAGIC lineage edge touching this path is the *read* in the next cell. A Spark
# MAGIC write here would put a second edge in the graph and make the assertion
# MAGIC ambiguous.
# MAGIC
# MAGIC The shape mirrors what `chip_chat.harvest` actually lands: the body of a
# MAGIC page, plus the `source_url` and `harvested_at` that RFC-001 §08 requires to
# MAGIC survive all the way into a citation.

# COMMAND ----------

source_path = f"{raw_uri}/_lineage_probe/{run_id}/menu.json"

documents = [
    {
        "source_url": "https://www.chipotle.com/order/build-your-own",
        "harvested_at": "2026-08-26T00:00:00Z",
        "item_name": "Burrito",
        "category": "Entree",
        "calories": 1075,
    },
    {
        "source_url": "https://www.chipotle.com/order/build-your-own",
        "harvested_at": "2026-08-26T00:00:00Z",
        "item_name": "Burrito Bowl",
        "category": "Entree",
        "calories": 1010,
    },
    {
        "source_url": "https://www.chipotle.com/order/build-your-own",
        "harvested_at": "2026-08-26T00:00:00Z",
        "item_name": "Salad",
        "category": "Entree",
        "calories": 640,
    },
]

dbutils.fs.put(source_path, "\n".join(json.dumps(d) for d in documents), True)
print(f"raw file: {source_path}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Bronze — as landed
# MAGIC
# MAGIC No cleaning. The one thing added is the source path, because by silver the
# MAGIC file the row came from is no longer recoverable from the row.

# COMMAND ----------

spark.sql(f"DROP TABLE IF EXISTS {GOLD}")
spark.sql(f"DROP TABLE IF EXISTS {SILVER}")
spark.sql(f"DROP TABLE IF EXISTS {BRONZE}")

(
    spark.read.json(source_path)
    .selectExpr("*", "_metadata.file_path AS _source_file")
    .write.mode("overwrite")
    .saveAsTable(BRONZE)
)

# Comments on anything a reader would otherwise have to guess at -- which issue
# #32 asks for, and which is also the Unity Catalog metadata this project exists
# to exercise. `_source_file` and `harvested_at` are the two that are genuinely
# not guessable, so they get the long ones.
spark.sql(
    f"COMMENT ON TABLE {BRONZE} IS "
    "'Lineage probe, bronze. Harvested documents exactly as landed in ADLS. "
    "Written by databricks/notebooks/lineage_probe.py (gh-32) to demonstrate "
    "end-to-end lineage; not part of the real medallion.'"
)
for column, comment in {
    "source_url": (
        "The URL the bytes actually came from, after redirects. "
        "This is the citation field (RFC-001 §08)."
    ),
    "harvested_at": (
        "When the fetch completed, UTC. A price or a calorie count is only "
        "meaningful with this beside it."
    ),
    "item_name": "Menu item name as published.",
    "category": "Menu section the item was published under.",
    "calories": "Calories as published, for the item as configured on the source page.",
    "_source_file": (
        "Full abfss:// path of the file this row was read from. Captured here "
        "because it is unrecoverable downstream."
    ),
}.items():
    spark.sql(f"ALTER TABLE {BRONZE} ALTER COLUMN {column} COMMENT '{comment}'")

print(f"bronze: {BRONZE}")
spark.table(BRONZE).show(truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Silver — conformed

# COMMAND ----------

# The comment is built here rather than written inline in the SQL: a quoted
# string that wraps across source lines keeps its newlines and its indentation,
# and those end up in the catalogue for a reader to trip over.
silver_comment = (
    "Lineage probe, silver. Conformed to the RFC-001 §04 menu_items shape. "
    "Written by databricks/notebooks/lineage_probe.py (gh-32)."
)

spark.sql(f"""
    CREATE TABLE {SILVER}
    COMMENT '{silver_comment}'
    AS SELECT
        lower(replace(item_name, ' ', '_')) AS item_id,
        item_name                           AS name,
        category,
        cast(calories AS int)               AS calories,
        source_url,
        cast(harvested_at AS timestamp)     AS harvested_at
    FROM {BRONZE}
""")
print(f"silver: {SILVER}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Gold — a mart
# MAGIC
# MAGIC Aggregated and serving-ready, which is what makes it gold rather than a
# MAGIC third copy of silver.

# COMMAND ----------

gold_comment = (
    "Lineage probe, gold. Per-category calorie summary, the shape a mart "
    "published to Snowflake takes. "
    "Written by databricks/notebooks/lineage_probe.py (gh-32)."
)

spark.sql(f"""
    CREATE TABLE {GOLD}
    COMMENT '{gold_comment}'
    AS SELECT
        category,
        count(*)          AS item_count,
        avg(calories)     AS mean_calories,
        max(harvested_at) AS derived_from_harvest_at
    FROM {SILVER}
    GROUP BY category
""")
for column, comment in {
    "item_count": "How many distinct catalogue items this category holds.",
    "mean_calories": "Mean published calories across those items, unweighted.",
}.items():
    spark.sql(f"ALTER TABLE {GOLD} ALTER COLUMN {column} COMMENT '{comment}'")

spark.sql(
    f"ALTER TABLE {GOLD} ALTER COLUMN derived_from_harvest_at COMMENT '"
    "Newest harvested_at among the rows behind this mart. RFC-001 §08 serves a "
    "stale mart WITH its timestamp rather than silently as fresh, so the "
    "timestamp has to survive the aggregation.'"
)
print(f"gold: {GOLD}")
spark.table(GOLD).show(truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. What Unity Catalog says happened
# MAGIC
# MAGIC The lineage-tracking REST API rather than `system.access.table_lineage`,
# MAGIC because the `system.access` schema has to be enabled by an *account*
# MAGIC admin and a workspace admin cannot do it. The API is workspace-level and
# MAGIC needs nothing turned on.
# MAGIC
# MAGIC Lineage is captured asynchronously, so this polls. A first run usually
# MAGIC resolves in well under a minute; the ceiling is generous because a run
# MAGIC that fails here is indistinguishable from one that was merely early.
# MAGIC
# MAGIC The call is a **GET with query parameters**. The documentation shows a
# MAGIC POST, and a POST answers `404 ENDPOINT_NOT_FOUND` — see `table_lineage`
# MAGIC below.

# COMMAND ----------

context = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
api_url = context.apiUrl().get()
api_token = context.apiToken().get()


def table_lineage(table_name: str) -> dict:
    """Return the lineage graph Unity Catalog holds for ``table_name``.

    ⚠️ GET WITH QUERY PARAMETERS, NOT POST WITH A BODY. The Databricks
    documentation for this endpoint shows a POST, and a POST to it on this
    workspace answers:

        404 {"error_code": "ENDPOINT_NOT_FOUND",
             "message": "No API found for 'POST /lineage-tracking/table-lineage'"}

    which reads like the lineage API is unavailable and is really the wrong verb.
    The same path answers 200 to a GET with the arguments in the query string.
    Verified on `dbw-chip-chat` 2026-08-26 — and worth the paragraph, because a
    404 is exactly the response someone would take as "this feature is off".
    """
    query = urllib.parse.urlencode(
        {"table_name": table_name, "include_entity_lineage": "true"}
    )
    request = urllib.request.Request(
        f"{api_url}/api/2.0/lineage-tracking/table-lineage?{query}",
        headers={"Authorization": f"Bearer {api_token}"},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def _as_list(value: object) -> list[dict]:
    """Return ``value`` as a list of dicts.

    The lineage API returns ``fileInfo`` as a single object on some edges and as
    a list on others. Normalising here keeps the caller from caring which.
    """
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def upstream_names(graph: dict) -> set[str]:
    """Every upstream table and file path in ``graph``, as strings."""
    names = set()
    for edge in graph.get("upstreams", []):
        for table in _as_list(edge.get("tableInfo")):
            parts = (
                table.get("catalog_name"),
                table.get("schema_name"),
                table.get("name"),
            )
            if all(parts):
                names.add(".".join(parts))
        for file_info in _as_list(edge.get("fileInfo")):
            if file_info.get("path"):
                names.add(file_info["path"])
    return names


DEADLINE_SECONDS = 300
started = time.monotonic()
chain: dict[str, set[str]] = {}

while True:
    chain = {name: upstream_names(table_lineage(name)) for name in (GOLD, SILVER, BRONZE)}
    resolved = (
        SILVER in chain[GOLD]
        and BRONZE in chain[SILVER]
        and any("_lineage_probe" in name for name in chain[BRONZE])
    )
    waited = time.monotonic() - started
    if resolved or waited > DEADLINE_SECONDS:
        break
    print(f"lineage not complete after {waited:.0f}s; upstreams so far: {chain}")
    time.sleep(15)

for name, upstreams in chain.items():
    print(f"{name}\n    <- {sorted(upstreams) or '(none recorded)'}")

# COMMAND ----------

problems = []
if SILVER not in chain[GOLD]:
    problems.append(f"{GOLD} does not record {SILVER} as an upstream")
if BRONZE not in chain[SILVER]:
    problems.append(f"{SILVER} does not record {BRONZE} as an upstream")
if not any("_lineage_probe" in name for name in chain[BRONZE]):
    problems.append(f"{BRONZE} does not record the raw file {source_path} as an upstream")

if problems:
    raise AssertionError(
        f"lineage did not resolve within {DEADLINE_SECONDS}s:\n  " + "\n  ".join(problems)
    )

print(
    "ok: lineage resolves from the raw file through bronze and silver to the gold mart\n"
    f"    {source_path}\n      -> {BRONZE}\n      -> {SILVER}\n      -> {GOLD}"
)

# COMMAND ----------

if cleanup:
    for table in (GOLD, SILVER, BRONZE):
        spark.sql(f"DROP TABLE IF EXISTS {table}")
    dbutils.fs.rm(f"{raw_uri}/_lineage_probe/{run_id}", recurse=True)
    print("probe tables and raw file removed")
else:
    print(
        "probe tables left in place -- they ARE the lineage. "
        "Re-run with cleanup=true to remove them."
    )
