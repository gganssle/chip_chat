# Databricks notebook source
# MAGIC %md
# MAGIC # The recommender — batch score into a table
# MAGIC
# MAGIC Issue [#37](https://github.com/gganssle/chip_chat/issues/37), and the
# MAGIC `publish` task of the `chip-chat-recommender` job. It loads the
# MAGIC `@champion` version of the registered model, scores every visitor, and
# MAGIC writes `gold_synthetic.recommendations`.
# MAGIC
# MAGIC **This is the whole point of the task existing.** The issue asks that the
# MAGIC serving path read a table rather than call a model on the conversational
# MAGIC path, and RFC-001 §06 backs `get_recommendations` with a gold mart. A
# MAGIC model invoked inside a chat turn would put an inference — and a model
# MAGIC endpoint's availability, and its cold start — between a visitor and an
# MAGIC answer that was already computable overnight.
# MAGIC
# MAGIC **It loads an alias, never a version number.** A run that could not beat
# MAGIC the popularity baseline on novel hit rate left `@champion` where it was,
# MAGIC so this task republishes the same recommendations rather than worse ones.
# MAGIC That is the whole mechanism: the alias is the deployment.
# MAGIC
# MAGIC **The rationale is rendered here, not in the model.** The sentence needs
# MAGIC the two items' published names, which live in
# MAGIC `silver_harvested.menu_items`, and joining them is a join. Keeping the
# MAGIC model's own output free of anything harvested also means a catalogue
# MAGIC re-harvest changes the wording without invalidating a model version.
# MAGIC
# MAGIC **Parameters**, both supplied by the job: `catalog`, `lib_path`.

# COMMAND ----------

import json
import sys

import mlflow
import pandas as pd

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog")
dbutils.widgets.text("lib_path", "", "Workspace directory holding recommender.py")

LIB_PATH = dbutils.widgets.get("lib_path")
if LIB_PATH not in sys.path:
    sys.path.insert(0, LIB_PATH)

import catalog  # noqa: E402
import recommender  # noqa: E402

CATALOG = dbutils.widgets.get("catalog")

if CATALOG != catalog.CATALOG:
    raise ValueError(
        f"the job is configured for catalog {CATALOG!r} but the layout module "
        f"names {catalog.CATALOG!r}; one of the two has drifted"
    )

mlflow.set_registry_uri("databricks-uc")

MODEL = f"{CATALOG}.{recommender.MODEL_SCHEMA}.{recommender.MODEL_NAME}"
ALIAS = f"models:/{MODEL}@{recommender.CHAMPION_ALIAS}"

ORDERS = catalog.table(recommender.SOURCE_LAYER, "synthetic", "orders")
ORDER_ITEMS = catalog.table(recommender.SOURCE_LAYER, "synthetic", "order_items")
MENU_ITEMS = catalog.table(recommender.SOURCE_LAYER, "harvested", "menu_items")
TARGET = catalog.table(recommender.LAYER, recommender.STREAM, recommender.MART)

version = (
    mlflow.MlflowClient()
    .get_model_version_by_alias(MODEL, recommender.CHAMPION_ALIAS)
    .version
)
model = mlflow.pyfunc.load_model(ALIAS)

print(f"catalog        {CATALOG}")
print(f"model          {ALIAS}")
print(f"version        {version}")
print(f"writes         {TARGET}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## What each visitor has ordered
# MAGIC
# MAGIC The same relation `recommender_train.py` split, read whole: scoring
# MAGIC serves a customer today, so it uses all of their history rather than the
# MAGIC training window. `is_training` is ignored here and that is deliberate —
# MAGIC the split exists to make the evaluation honest, not to hide four months
# MAGIC of somebody's orders from the model that is about to advise them.
# MAGIC
# MAGIC The history is also the exclusion list. Every item in it is something the
# MAGIC visitor has ordered, and nothing they have ordered can be recommended.

# COMMAND ----------

spark.sql(recommender.training_query(ORDERS, ORDER_ITEMS)).createOrReplaceTempView(
    "events"
)

visitors = spark.sql(
    "SELECT demo_id, "
    "  to_json(map_from_entries(collect_list(struct(item_id, orders_with)))) "
    "    AS history_json, "
    "  max(orders) AS orders "
    "FROM ("
    "  SELECT e.demo_id, e.item_id, count(DISTINCT e.order_id) AS orders_with, "
    "         t.orders AS orders "
    "  FROM events e "
    "  JOIN (SELECT demo_id, count(DISTINCT order_id) AS orders "
    "        FROM events GROUP BY demo_id) t ON t.demo_id = e.demo_id "
    "  GROUP BY e.demo_id, e.item_id, t.orders"
    ") GROUP BY demo_id"
).toPandas()

print(f"  {len(visitors)} visitors have a settled order history")

if visitors.empty:
    raise AssertionError(
        "no visitor has a settled order, so there is nothing to recommend from "
        "and replacing the table would empty the serving path"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## The scoring
# MAGIC
# MAGIC One `predict` on the driver rather than a `spark_udf`, for the reason
# MAGIC `recommender_train.py` collects the fit: five hundred visitors and a
# MAGIC model measured in thousands of pairs is one process's worth of work, and
# MAGIC the ceiling below is what makes that a checked claim rather than an
# MAGIC assumption. A population that outgrows it should distribute the scoring
# MAGIC on purpose, not discover the need when a driver runs out of memory.

# COMMAND ----------

MAX_VISITORS = 250_000
"""Visitors the driver will score in one call. A ceiling on the assumption."""

if len(visitors) > MAX_VISITORS:
    raise AssertionError(
        f"{len(visitors)} visitors is more than this notebook scores on the "
        f"driver ({MAX_VISITORS}). Distribute the scoring with an "
        "`mlflow.pyfunc.spark_udf` rather than raising this quietly"
    )

answers = model.predict(visitors[["demo_id", "history_json", "orders"]])
scored_json = pd.DataFrame(
    {
        "demo_id": visitors["demo_id"].to_numpy(),
        "recommendations_json": answers["recommendations_json"].to_numpy(),
    }
)
spark.createDataFrame(scored_json).createOrReplaceTempView("scored_json")

# The two decimals travel as strings and are cast back here. JSON has one number
# type and it is a double, so a decimal that crossed as a JSON number would
# arrive with a rounding error in the last place -- and DECIMAL(12,6) exists in
# this layer precisely so that a rebuild reproduces the row.
spark.sql(
    "SELECT j.demo_id, s.item_id, s.seed_item_id, "
    f"  CAST(s.seed_share AS {recommender.SCORE}) AS seed_share, "
    f"  CAST(s.score AS {recommender.SCORE}) AS score, "
    "  s.rank AS rank "
    "FROM scored_json j "
    "LATERAL VIEW explode("
    f"  from_json(j.recommendations_json, '{recommender.scored_schema()}')"
    ") exploded AS s"
).createOrReplaceTempView("scored")

suggested = spark.table("scored").count()
served = spark.sql("SELECT count(DISTINCT demo_id) AS visitors FROM scored").first()
print(f"  {suggested} recommendations for {served['visitors']} visitors")
print(
    f"  {len(visitors) - served['visitors']} visitors got none, which is the "
    "honest absence rather than a popularity fallback"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## The table
# MAGIC
# MAGIC `CREATE OR REPLACE TABLE` rather than a truncate and an insert. Delta
# MAGIC replaces the table in one commit, so a reader mid-publish sees last
# MAGIC night's recommendations or tonight's and never an empty serving path —
# MAGIC which matters more here than in the pipeline-built marts, because this
# MAGIC table is written by a job and a job can die between two statements.
# MAGIC
# MAGIC The column comments are `recommender.RECOMMENDATIONS`' own `why` fields.
# MAGIC A column in a catalogue browser is a name and a type unless somebody
# MAGIC wrote down what the number means, and writing it in two places is how the
# MAGIC two come to disagree.

# COMMAND ----------


def quoted(text):
    """Return `text` as a SQL string literal."""
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


spark.sql(
    f"CREATE OR REPLACE TABLE {TARGET} AS "
    + recommender.publish_query("scored", MENU_ITEMS, str(version))
)

published = spark.table(TARGET)
columns = tuple(field.name for field in published.schema.fields)
if columns != recommender.column_names():
    raise AssertionError(
        f"{TARGET} published {list(columns)} but the declaration is "
        f"{list(recommender.column_names())}"
    )

spark.sql(f"COMMENT ON TABLE {TARGET} IS {quoted(recommender.RECOMMENDATIONS.comment)}")
spark.sql(
    f"ALTER TABLE {TARGET} SET TBLPROPERTIES ("
    f"  'chip_chat.stream' = {quoted(recommender.STREAM)},"
    "  'chip_chat.issue' = 'gh-37',"
    f"  'chip_chat.grain' = {quoted(recommender.RECOMMENDATIONS.grain)},"
    f"  'chip_chat.model' = {quoted(MODEL)},"
    f"  'chip_chat.model_version' = {quoted(str(version))},"
    "  'delta.enableChangeDataFeed' = 'true'"
    ")"
)
for column in recommender.RECOMMENDATIONS.columns:
    spark.sql(
        f"ALTER TABLE {TARGET} ALTER COLUMN {column.name} COMMENT {quoted(column.why)}"
    )

print(f"  {published.count()} rows in {TARGET}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The expectations
# MAGIC
# MAGIC The pipeline-built marts get `expect_all_or_fail` from Lakeflow. This
# MAGIC table is written by a job, so the same constraints are re-run here as
# MAGIC filters that must match nothing, and a match fails the task.
# MAGIC
# MAGIC Fatal rather than a warning, for `gold.Expectation`'s reason: this table
# MAGIC is what the agent answers from, so a row that violates its own definition
# MAGIC is a wrong answer in somebody's conversation rather than a line in an
# MAGIC event log. It fails *after* the write on purpose — a table that exists
# MAGIC and is provably wrong is a better thing to debug than a table that
# MAGIC silently stayed at yesterday's contents.

# COMMAND ----------

violations = {}
for expectation in recommender.expectations():
    broken = published.where(f"NOT ({expectation.constraint})").count()
    print(("  ok   " if broken == 0 else "  FAIL ") + expectation.name)
    if broken:
        violations[expectation.name] = broken

if violations:
    raise AssertionError(
        f"{len(violations)} expectations failed on {TARGET}: "
        + ", ".join(f"{name} ({rows} rows)" for name, rows in sorted(violations.items()))
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## The verdict

# COMMAND ----------

dbutils.notebook.exit(
    json.dumps(
        {
            "table": TARGET,
            "model": MODEL,
            "version": str(version),
            "rows": published.count(),
            "visitors": served["visitors"],
        },
        sort_keys=True,
    )
)
