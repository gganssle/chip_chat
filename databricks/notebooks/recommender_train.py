# Databricks notebook source
# MAGIC %md
# MAGIC # The recommender — fit, judge, register
# MAGIC
# MAGIC Issue [#37](https://github.com/gganssle/chip_chat/issues/37), and the
# MAGIC `train` task of the `chip-chat-recommender` job. It fits a co-occurrence
# MAGIC model, measures it against a temporal holdout **and against a popularity
# MAGIC baseline**, logs everything to MLflow, registers a version in Unity
# MAGIC Catalog, and moves the `@champion` alias only if the run earned it.
# MAGIC
# MAGIC Every decision is in `chip_chat.databricks.recommender`, where
# MAGIC `databricks/tests/test_recommender.py` reads it without a cluster. This
# MAGIC notebook is the Spark calls and the MLflow calls.
# MAGIC
# MAGIC **The baseline is the point, not a formality.** PRD requirement P2 asks
# MAGIC for recommendations grounded in the visitor's actual ordering behaviour
# MAGIC *rather than generic popularity*, and says a global top-sellers list does
# MAGIC not satisfy it **even if it scores well**. It does score well — most
# MAGIC people's next order contains a staple — so this run reports four hit
# MAGIC rates rather than one, and promotes on the pair a top-sellers list cannot
# MAGIC win: `novel_hit_rate_at_k`, hits on items that visitor had never ordered.
# MAGIC
# MAGIC **The model is fitted twice**, and the second fit is the one registered.
# MAGIC The training-window fit is what the holdout can honestly judge; the
# MAGIC full-history refit, with the same hyperparameters, is what gets deployed,
# MAGIC because throwing away the most recent fifth of a customer's history to
# MAGIC serve them is a strange thing to do on purpose. The refit is also what
# MAGIC `item_affinity_agreement` compares against `gold_synthetic.item_affinity`
# MAGIC — issue #37 asks that the mart come from the model, #36 landed first and
# MAGIC made it a materialized view with its own determinism criterion, so the
# MAGIC relationship is inverted and checked rather than rebuilt.
# MAGIC
# MAGIC **Nothing here reads `demo_visitors`.** RFC-001 §04's containment holds
# MAGIC over everything downstream of silver, not only over the four marts, and a
# MAGIC recommender that read a visitor's `stated_preferences` would break it
# MAGIC while looking helpful. `test_recommender.py` checks this file for it.
# MAGIC
# MAGIC **Parameters**, all supplied by the job: `catalog`, `lib_path`,
# MAGIC `experiment`.

# COMMAND ----------

import json
import sys
import tempfile
from pathlib import Path

import mlflow
import pandas as pd

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog")
dbutils.widgets.text("lib_path", "", "Workspace directory holding recommender.py")
dbutils.widgets.text("experiment", "", "MLflow experiment path")

LIB_PATH = dbutils.widgets.get("lib_path")
if LIB_PATH not in sys.path:
    sys.path.insert(0, LIB_PATH)

import catalog  # noqa: E402
import recommender  # noqa: E402
import recommender_model  # noqa: E402

CATALOG = dbutils.widgets.get("catalog")

if CATALOG != catalog.CATALOG:
    raise ValueError(
        f"the job is configured for catalog {CATALOG!r} but the layout module "
        f"names {catalog.CATALOG!r}; one of the two has drifted"
    )

# Unity Catalog is the registry, not the workspace one. Without this the run
# would register a two-level name into a registry this project does not use, and
# the failure would be a model that exists in the wrong place rather than an
# error.
mlflow.set_registry_uri("databricks-uc")
mlflow.set_experiment(dbutils.widgets.get("experiment"))

MODEL = f"{CATALOG}.{recommender.MODEL_SCHEMA}.{recommender.MODEL_NAME}"

ORDERS = catalog.table(recommender.SOURCE_LAYER, "synthetic", "orders")
ORDER_ITEMS = catalog.table(recommender.SOURCE_LAYER, "synthetic", "order_items")
MENU_ITEMS = catalog.table(recommender.SOURCE_LAYER, "harvested", "menu_items")
AFFINITY_MART = catalog.table(
    recommender.LAYER, recommender.STREAM, recommender.REFERENCE_MART
)

print(f"catalog        {CATALOG}")
print(f"model          {MODEL}")
print(f"reads          {ORDERS}")
print(f"reads          {ORDER_ITEMS}")
print(f"reads          {MENU_ITEMS}")
print(f"compares to    {AFFINITY_MART}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The split
# MAGIC
# MAGIC One relation — every (order, item) event over the settled history, each
# MAGIC carrying whether it falls in the training window. The fit and the
# MAGIC evaluation both read it, so the split is computed once and cannot differ
# MAGIC between them.
# MAGIC
# MAGIC The split instant is read out of the data rather than off the wall clock,
# MAGIC for `gold.AS_OF`'s reason: a window that moves on its own makes two runs
# MAGIC over the same silver incomparable, and a metric that drifts because time
# MAGIC passed is a metric nobody can act on.

# COMMAND ----------

spark.sql(recommender.training_query(ORDERS, ORDER_ITEMS)).createOrReplaceTempView(
    "events"
)

split = spark.sql(
    "SELECT sum(CAST(is_training AS INT)) AS training, "
    "count(*) - sum(CAST(is_training AS INT)) AS holdout FROM events"
).first()
print(f"  {split['training']} training events, {split['holdout']} held out")

if split["holdout"] == 0:
    raise AssertionError(
        "the holdout is empty, so nothing measured below would be a measurement"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## The fit
# MAGIC
# MAGIC A co-occurrence count and three denominators per ordered pair — counts
# MAGIC rather than scores, so that a logged version carries the evidence and a
# MAGIC hyperparameter sweep can rescore an existing fit rather than refit it.
# MAGIC `recommender.score` is where the shrinkage is applied.
# MAGIC
# MAGIC The pairs are collected to the driver, and that is a decision rather than
# MAGIC an oversight. The support floor is 25 co-orders over a population of five
# MAGIC hundred customers and a menu of a few hundred items, so the fitted model
# MAGIC is thousands of rows, not millions; it has to fit in one process anyway,
# MAGIC because that process is what MLflow serialises into a model version. The
# MAGIC assertion below is what makes "small enough" a checked claim rather than
# MAGIC an assumption that fails quietly on a bigger population.

# COMMAND ----------

MAX_PAIRS = 2_000_000
"""Pairs the driver will hold. A ceiling on the assumption, not a tuning knob."""


def fit(training_only):
    """Return the fitted pairs over the training window, or over everything."""
    statement = recommender.affinity_query("events", training_only=training_only)
    frame = spark.sql(statement)
    total = frame.count()
    if total > MAX_PAIRS:
        raise AssertionError(
            f"{total} pairs cleared the support floor, which is more than this "
            f"notebook collects to the driver ({MAX_PAIRS}). The model is meant "
            "to be small enough to serialise; raise the floor or distribute the "
            "scoring, but do not raise this quietly"
        )
    return [
        recommender.Affinity(
            item_id=row["item_id"],
            related_item_id=row["related_item_id"],
            co_orders=row["co_orders"],
            orders_with_item=row["orders_with_item"],
            orders_with_related=row["orders_with_related"],
            orders=row["orders"],
        )
        for row in frame.collect()
    ]


trained = fit(training_only=True)
refit = fit(training_only=False)
print(f"  {len(trained)} pairs from the training window")
print(f"  {len(refit)} pairs from the whole history")

if not refit:
    raise AssertionError(
        "no pair cleared the support floor, so the fit is empty. Registering it "
        "would put a model that recommends nothing into the registry with a "
        "version number, which is worse than having no model at all"
    )

entrees = frozenset(
    row["item_id"]
    for row in spark.sql(
        f"SELECT item_id FROM {MENU_ITEMS} "
        f"WHERE category = '{recommender.ENTREE_CATEGORY}'"
    ).collect()
)
print(f"  {len(entrees)} of the catalogue's items are composed entrees")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The holdout
# MAGIC
# MAGIC Each visitor is scored from their **training** history only, and judged
# MAGIC on what they actually ordered afterwards. Then the same visitors are
# MAGIC scored by a popularity list built from the same training window, and
# MAGIC judged by the same function — `recommender.hit_rates`, called twice, so
# MAGIC that the comparison is between two models rather than between two
# MAGIC measurement routines.

# COMMAND ----------

history_rows = spark.sql(
    "SELECT demo_id, item_id, count(DISTINCT order_id) AS orders_with "
    "FROM events WHERE is_training GROUP BY demo_id, item_id"
).collect()
order_rows = spark.sql(
    "SELECT demo_id, count(DISTINCT order_id) AS orders "
    "FROM events WHERE is_training GROUP BY demo_id"
).collect()
holdout_rows = spark.sql(
    "SELECT demo_id, collect_set(item_id) AS items "
    "FROM events WHERE NOT is_training GROUP BY demo_id"
).collect()

histories = {}
for row in history_rows:
    histories.setdefault(row["demo_id"], {})[row["item_id"]] = row["orders_with"]
order_counts = {row["demo_id"]: row["orders"] for row in order_rows}
holdout = {row["demo_id"]: list(row["items"]) for row in holdout_rows}

by_seed = {}
for pair in trained:
    by_seed.setdefault(pair.item_id, []).append(pair)

suggested = {}
baseline_items = recommender.popular_items(
    {
        row["item_id"]: row["orders_with"]
        for row in spark.sql(
            "SELECT item_id, count(DISTINCT order_id) AS orders_with "
            "FROM events WHERE is_training GROUP BY item_id"
        ).collect()
    }
)
baseline_suggested = {}

for visitor, history in histories.items():
    pairs = [pair for item in sorted(history) for pair in by_seed.get(item, ())]
    suggested[visitor] = [
        suggestion.item_id
        for suggestion in recommender.recommend(
            history, order_counts[visitor], pairs, entrees=entrees
        )
    ]
    baseline_suggested[visitor] = list(baseline_items)

hit_rate, novel_hit_rate, scored = recommender.hit_rates(suggested, holdout, histories)
baseline_hit_rate, baseline_novel_hit_rate, _ = recommender.hit_rates(
    baseline_suggested, holdout, histories
)

catalogue = spark.sql(f"SELECT count(*) AS items FROM {MENU_ITEMS}").first()["items"]
recommended_items = {item for items in suggested.values() for item in items}
coverage = round(len(recommended_items) / catalogue, 6) if catalogue else 0.0

print(f"  {scored} visitors scored against {len(baseline_items)} baseline items")
print(f"  {recommender.HIT_RATE.name:<28} {hit_rate}")
print(f"  {recommender.BASELINE_HIT_RATE.name:<28} {baseline_hit_rate}")
print(f"  {recommender.NOVEL_HIT_RATE.name:<28} {novel_hit_rate}")
print(f"  {recommender.BASELINE_NOVEL_HIT_RATE.name:<28} {baseline_novel_hit_rate}")
print(f"  {recommender.COVERAGE.name:<28} {coverage}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Does the refit reproduce the mart?
# MAGIC
# MAGIC `item_affinity_agreement` is issue #37's *"produce the `item_affinity`
# MAGIC mart from the registered model"*, turned the only way round that does not
# MAGIC take a passing acceptance criterion away from #36. The mart stays a
# MAGIC materialized view; the model's full-history refit has to agree with it,
# MAGIC pair for pair and to the mart's own scale.
# MAGIC
# MAGIC Anything below 1.0 means the two definitions of lift have drifted, which
# MAGIC is a finding either way round: either the model has stopped being a
# MAGIC co-occurrence model, or the mart has.

# COMMAND ----------

published = {
    (row["item_id"], row["related_item_id"]): row["lift"]
    for row in spark.table(AFFINITY_MART).collect()
}
shared = [pair for pair in refit if (pair.item_id, pair.related_item_id) in published]
matched = sum(
    1 for pair in shared if pair.lift == published[(pair.item_id, pair.related_item_id)]
)
agreement = round(matched / len(shared), 6) if shared else 0.0

print(f"  the refit found {len(refit)} pairs; the mart publishes {len(published)}")
print(f"  {len(shared)} are in both, and {matched} carry the same lift")
print(f"  {recommender.AGREEMENT.name:<28} {agreement}")

if len(shared) != len(refit) or len(shared) != len(published):
    print(
        "  NOTE: the two fits do not cover the same pairs. Both apply "
        f"{recommender.MINIMUM_CO_ORDERS} co-orders to the same settled "
        "statuses, so a difference here is silver having moved between the "
        "pipeline update and this run rather than a disagreement about lift"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## The run
# MAGIC
# MAGIC Parameters, metrics, the fitted pairs as an artifact, and the model
# MAGIC itself — with `recommender.py` logged beside it as `code_paths`, so the
# MAGIC version in the registry is the model that produced the metrics logged
# MAGIC next to it rather than whatever the module says next week.
# MAGIC
# MAGIC The parameters are logged with their explanations as tags. MLflow records
# MAGIC a parameter as a string with nothing attached, and a run whose reader has
# MAGIC to open the source to find out what `shrinkage=40` did is a run that is
# MAGIC tracked but not documented.

# COMMAND ----------

metrics = {
    recommender.HIT_RATE.name: float(hit_rate),
    recommender.NOVEL_HIT_RATE.name: float(novel_hit_rate),
    recommender.BASELINE_HIT_RATE.name: float(baseline_hit_rate),
    recommender.BASELINE_NOVEL_HIT_RATE.name: float(baseline_novel_hit_rate),
    recommender.COVERAGE.name: float(coverage),
    recommender.AGREEMENT.name: float(agreement),
    recommender.VISITORS_SCORED.name: float(scored),
    recommender.PAIRS_KEPT.name: float(len(refit)),
}

with mlflow.start_run() as run:
    mlflow.log_params(
        {setting.name: setting.value for setting in recommender.hyperparameters()}
    )
    mlflow.set_tags(
        {f"why.{setting.name}": setting.why for setting in recommender.hyperparameters()}
    )
    mlflow.set_tags(
        {f"means.{measure.name}": measure.summary for measure in recommender.METRICS}
    )
    mlflow.log_metrics(metrics)

    staged = Path(tempfile.mkdtemp())
    fitted = staged / f"{recommender_model.ARTIFACT}.json"
    fitted.write_text(
        json.dumps(
            {
                "entrees": sorted(entrees),
                "pairs": [vars(pair) for pair in refit],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    example = pd.DataFrame(
        {
            "demo_id": ["example"],
            "history_json": [json.dumps({pair.item_id: 1 for pair in refit[:1]})],
            "orders": [1],
        }
    )

    logged = mlflow.pyfunc.log_model(
        name="model",
        python_model=recommender_model.Recommender(),
        artifacts={recommender_model.ARTIFACT: str(fitted)},
        code_paths=[
            f"{LIB_PATH}/recommender.py",
            f"{LIB_PATH}/recommender_model.py",
        ],
        signature=recommender_model.signature(),
        input_example=example,
        registered_model_name=MODEL,
    )

version = logged.registered_model_version
print(f"  registered {MODEL} version {version} from run {run.info.run_id}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The alias
# MAGIC
# MAGIC An alias, not a stage. Unity Catalog replaced the Workspace Model
# MAGIC Registry's `Staging`/`Production` transitions with aliases, and
# MAGIC `transition_model_version_stage` is not a call this registry has — the
# MAGIC same judgement this project made about `import dlt`.
# MAGIC
# MAGIC `recommender_publish.py` loads `@champion` and nothing else, so a run
# MAGIC that cannot beat the popularity baseline on novel hit rate leaves the
# MAGIC alias where it is and publishes nothing. That is the property worth
# MAGIC having: a bad training run is a version in the registry with its metrics
# MAGIC attached, not a worse table in front of a visitor.

# COMMAND ----------

promoted = recommender.beats_baseline(novel_hit_rate, baseline_novel_hit_rate)

if promoted:
    mlflow.MlflowClient().set_registered_model_alias(
        MODEL, recommender.CHAMPION_ALIAS, version
    )
    print(f"  @{recommender.CHAMPION_ALIAS} now points at version {version}")
else:
    print(
        f"  version {version} did NOT take @{recommender.CHAMPION_ALIAS}: its "
        f"{recommender.NOVEL_HIT_RATE.name} of {novel_hit_rate} is not "
        f"{recommender.MINIMUM_MARGIN} above the popularity baseline's "
        f"{baseline_novel_hit_rate}"
    )
    print("  the run is logged; the published table is untouched")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The verdict

# COMMAND ----------

# Machine-readable, for the reason every verify notebook here ends this way: a
# job's notebook output is what the run API returns, so the numbers are quotable
# without opening the workspace. The publish task reads `promoted` to decide
# whether there is anything new to publish.
dbutils.notebook.exit(
    json.dumps(
        {
            "model": MODEL,
            "version": version,
            "run_id": run.info.run_id,
            "promoted": promoted,
            "metrics": metrics,
        },
        sort_keys=True,
    )
)
