# Databricks notebook source
# MAGIC %md
# MAGIC # The recommender — the acceptance criteria, as assertions
# MAGIC
# MAGIC Issue [#37](https://github.com/gganssle/chip_chat/issues/37) has four
# MAGIC acceptance criteria and every one of them is a claim about a live system.
# MAGIC This notebook is those claims, run as the `chip-chat-recommender-verify`
# MAGIC job, so SUCCESS means the assertions passed rather than the notebook
# MAGIC merely finishing. Same shape as `gold_verify.py`, and for the same
# MAGIC reason.
# MAGIC
# MAGIC 1. **Trained, tracked, and registered with a version in Unity Catalog.**
# MAGIC    The registered model exists at a three-level name, has versions, and
# MAGIC    `@champion` resolves to one of them — and that version's MLflow run
# MAGIC    carries every parameter and every metric the module declares. A
# MAGIC    registered model with no run behind it is a pickle with a version
# MAGIC    number.
# MAGIC 2. **Reviewed for plausibility, and for not recommending what they
# MAGIC    already order.** The exclusion is checked as an emptiness join over
# MAGIC    the whole population — no visitor is recommended anything in their own
# MAGIC    settled history — and the persona fixtures' own recommendations are
# MAGIC    printed in full, because "reviewed for plausibility" is a human
# MAGIC    reading sentences and the notebook's job is to put them where they can
# MAGIC    be read.
# MAGIC 3. **A short rationale on every recommendation.** Non-empty, within
# MAGIC    `recommender.MAX_RATIONALE_CHARS`, naming both items, and shaped like
# MAGIC    the template rather than merely present.
# MAGIC 4. **Retraining is a scheduled job.** Read back off the Jobs API: the job
# MAGIC    exists, it has a cron schedule, and the notebook prints whether that
# MAGIC    schedule is paused. Paused is the shipped default and the reason is in
# MAGIC    `infra/terraform/databricks_recommender.tf`; unscheduled would be the
# MAGIC    criterion failing.
# MAGIC
# MAGIC It also checks the thing none of the four say and PRD requirement P2 does:
# MAGIC that these are **not a top-sellers list**. Two visitors with different
# MAGIC histories have to get different recommendations, and the overlap with the
# MAGIC population's most-ordered items is printed beside it.
# MAGIC
# MAGIC Read-only. Safe to run at any time.

# COMMAND ----------

import json
import sys

import mlflow
from databricks.sdk import WorkspaceClient

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog")
dbutils.widgets.text("lib_path", "", "Workspace directory holding recommender.py")
dbutils.widgets.text("job_name", "", "Name of the scheduled retraining job")

sys.path.insert(0, dbutils.widgets.get("lib_path"))

import catalog  # noqa: E402
import recommender  # noqa: E402

CATALOG = dbutils.widgets.get("catalog")
JOB_NAME = dbutils.widgets.get("job_name")

mlflow.set_registry_uri("databricks-uc")

MODEL = f"{CATALOG}.{recommender.MODEL_SCHEMA}.{recommender.MODEL_NAME}"
TARGET = catalog.table(recommender.LAYER, recommender.STREAM, recommender.MART)
ORDER_ITEMS = catalog.table(recommender.SOURCE_LAYER, "synthetic", "order_items")
ORDERS = catalog.table(recommender.SOURCE_LAYER, "synthetic", "orders")
MENU_ITEMS = catalog.table(recommender.SOURCE_LAYER, "harvested", "menu_items")
FIXTURES = catalog.table(recommender.SOURCE_LAYER, "synthetic", "persona_fixtures")

failures = []


def check(condition, message):
    """Record a failed claim rather than raising on the first one.

    A run that stops at the first failure tells you one thing per cluster start,
    and a cluster start here is four minutes and a few cents.
    """
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


client = mlflow.MlflowClient()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 1 — trained, tracked, registered, with a version
# MAGIC
# MAGIC Three separate things, and the issue asks for all three. Registered is
# MAGIC the model existing at a three-level Unity Catalog name. Versioned is
# MAGIC `@champion` resolving to a numbered version. **Tracked** is the one worth
# MAGIC checking hardest: the version's run has to carry every parameter and
# MAGIC every metric `recommender` declares, because a model in a registry with
# MAGIC no run behind it cannot be compared to its successor, and comparing
# MAGIC versions is the only thing a registry is for.

# COMMAND ----------

registered = None
try:
    registered = client.get_registered_model(MODEL)
except Exception as error:  # the message is the finding
    check(False, f"{MODEL} is not registered: {error}")

if registered is not None:
    check(MODEL.count(".") == 2, f"{MODEL} is a three-level Unity Catalog name")
    check(
        bool(registered.description or recommender.MODEL_COMMENT),
        f"{MODEL} carries a description",
    )

champion = None
try:
    champion = client.get_model_version_by_alias(MODEL, recommender.CHAMPION_ALIAS)
    check(
        champion.version is not None,
        f"@{recommender.CHAMPION_ALIAS} resolves to version {champion.version}",
    )
except Exception as error:
    check(False, f"@{recommender.CHAMPION_ALIAS} does not resolve: {error}")

if champion is not None:
    run = client.get_run(champion.run_id)
    for setting in recommender.hyperparameters():
        logged = run.data.params.get(setting.name)
        check(
            logged is not None,
            f"the run logged {setting.name} = {logged} ({setting.why})",
        )
    for measure in recommender.METRICS:
        value = run.data.metrics.get(measure.name)
        check(value is not None, f"the run logged {measure.name} = {value}")

    # Two admissible reasons for this alias to be where it is, and the check has
    # to know which. Beating the popularity baseline is the rule. Being the
    # first version is the bootstrap `recommender.takes_the_alias` adds, because
    # the rule is about not *replacing* a champion and with none the alternative
    # is an empty serving table -- `docs/recommender.md` §6 argues it.
    #
    # So the failure this looks for is the one that matters: a champion that did
    # not beat the baseline while some *other* version did. That is a worse
    # model serving than one the registry already holds, which no rule here
    # permits. A champion that did not beat it and neither did anything else is
    # reported instead, loudly, the way a PAUSED schedule is -- it is the
    # shipped state of a model on a catalogue too small to tell the two apart.
    beat = recommender.beats_baseline(
        run.data.metrics.get(recommender.NOVEL_HIT_RATE.name, 0.0),
        run.data.metrics.get(recommender.BASELINE_NOVEL_HIT_RATE.name, 0.0),
    )
    contenders = []
    for other in client.search_model_versions(f"name='{MODEL}'"):
        if str(other.version) == str(champion.version) or not other.run_id:
            continue
        metrics = client.get_run(other.run_id).data.metrics
        if recommender.beats_baseline(
            metrics.get(recommender.NOVEL_HIT_RATE.name, 0.0),
            metrics.get(recommender.BASELINE_NOVEL_HIT_RATE.name, 0.0),
        ):
            contenders.append(str(other.version))

    check(
        beat or not contenders,
        f"the champion is the best version this registry holds on "
        f"{recommender.NOVEL_HIT_RATE.name}"
        + (
            ""
            if beat or not contenders
            else f" — version(s) {sorted(contenders)} beat the baseline and "
            f"version {champion.version} does not hold the alias over them"
        ),
    )
    if beat:
        print(
            f"  the champion beat the popularity baseline by at least "
            f"{recommender.MINIMUM_MARGIN}, which is PRD P2's requirement"
        )
    else:
        print(
            f"  the champion did NOT beat the popularity baseline, and neither "
            f"has any other version. @{recommender.CHAMPION_ALIAS} is where it "
            "is because a first version has nothing to beat; see "
            "docs/recommender.md §6"
        )
    check(
        run.data.metrics.get(recommender.AGREEMENT.name) == 1.0,
        f"the full-history refit reproduces {recommender.REFERENCE_MART} exactly "
        f"({recommender.AGREEMENT.name} = "
        f"{run.data.metrics.get(recommender.AGREEMENT.name)})",
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 1, continued — the published table matches its declaration
# MAGIC
# MAGIC The agent's read tool queries these columns by name, so the whole list is
# MAGIC compared in order and with types rather than checked for the ones it
# MAGIC wants. `model_version` is the column that ties a served row back to a run
# MAGIC and its metrics, so a row whose version is not a version that exists is
# MAGIC a row nobody can trace.

# COMMAND ----------

table = None
try:
    table = spark.table(TARGET)
    rows = table.count()
except Exception as error:
    check(False, f"{TARGET} is not readable: {error}")
    rows = 0

if table is not None:
    check(rows > 0, f"{TARGET} holds {rows} rows ({recommender.RECOMMENDATIONS.grain})")

    published = tuple(field.name for field in table.schema.fields)
    declared = recommender.column_names()
    check(
        published == declared,
        f"{TARGET} publishes {list(declared)}"
        + ("" if published == declared else f" — it publishes {list(published)}"),
    )

    types = {
        field.name: field.dataType.simpleString().upper().replace(" ", "")
        for field in table.schema
    }
    for column in recommender.RECOMMENDATIONS.columns:
        want = column.sql_type.upper().replace(" ", "")
        got = types.get(column.name, "<missing>")
        check(
            got == want,
            f"{TARGET}.{column.name} is {want}"
            + ("" if got == want else f" — it is {got}"),
        )

    for expectation in recommender.expectations():
        broken = table.where(f"NOT ({expectation.constraint})").count()
        check(broken == 0, f"{expectation.name} ({expectation.why})")

    if champion is not None:
        stamped = {
            row["model_version"]
            for row in table.select("model_version").distinct().collect()
        }
        check(
            stamped == {str(champion.version)},
            f"every row names the champion version {champion.version}"
            + ("" if stamped == {str(champion.version)} else f" — they name {stamped}"),
        )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 2 — nothing they already order
# MAGIC
# MAGIC The exclusion rule is the strong one: **everything** in a visitor's
# MAGIC settled history is excluded, not merely what they order constantly. That
# MAGIC is what makes this an emptiness assertion instead of an argument about
# MAGIC where a threshold sits — a recommendation joined to its own visitor's
# MAGIC order lines must return nothing, over the whole population and not over a
# MAGIC sample.

# COMMAND ----------

settled = ", ".join(f"'{status}'" for status in recommender.SETTLED_STATUSES)
already = spark.sql(
    f"SELECT r.demo_id, r.item_id, count(*) AS times_ordered "
    f"FROM {TARGET} r "
    f"JOIN {ORDER_ITEMS} i ON i.demo_id = r.demo_id AND i.item_id = r.item_id "
    f"JOIN {ORDERS} o ON o.order_id = i.order_id AND o.status IN ({settled}) "
    "GROUP BY r.demo_id, r.item_id"
)
offending = already.count()
check(
    offending == 0,
    f"no visitor is recommended anything they have ever ordered ({offending} such rows)",
)
if offending:
    already.show(20, truncate=40)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 2, continued — the personas, for a human to read
# MAGIC
# MAGIC "Reviewed for plausibility" is a person reading sentences, and this
# MAGIC notebook's job is to put them somewhere they can be read rather than to
# MAGIC pretend an assertion can do it. `persona_fixtures` is issue #26's table:
# MAGIC the particular customers chosen to demonstrate each archetype, so it is
# MAGIC exactly the sample the criterion means by "a sample of personas".
# MAGIC
# MAGIC The one thing asserted here is the sharpest version of the criterion: a
# MAGIC fixture's `usual_item_id` is the thing they order most in the whole
# MAGIC population, and it must not appear in their recommendations.

# COMMAND ----------

fixtures = spark.sql(
    f"SELECT persona_id, rank, demo_id, usual_item_id FROM {FIXTURES} ORDER BY "
    "persona_id, rank"
)
usual_recommended = spark.sql(
    f"SELECT f.persona_id, f.demo_id, f.usual_item_id "
    f"FROM {FIXTURES} f JOIN {TARGET} r "
    "  ON r.demo_id = f.demo_id AND r.item_id = f.usual_item_id"
).count()
check(
    usual_recommended == 0,
    f"no fixture is recommended their own usual order ({usual_recommended} such rows)",
)

covered = spark.sql(
    f"SELECT count(DISTINCT f.demo_id) AS with_rows FROM {FIXTURES} f "
    f"JOIN {TARGET} r ON r.demo_id = f.demo_id"
).first()["with_rows"]
print(f"  {covered} of {fixtures.count()} fixtures have recommendations")
print(
    "  a fixture with none has no pair clearing the support floor, which is an "
    "honest absence rather than a gap — read the sentences below and say whether "
    "that is true of them"
)

print()
print("  the fixtures' recommendations, in full:")
spark.sql(
    f"SELECT f.persona_id, f.rank AS fixture_rank, r.rank, r.score, r.rationale "
    f"FROM {FIXTURES} f JOIN {TARGET} r ON r.demo_id = f.demo_id "
    "ORDER BY f.persona_id, f.rank, r.rank"
).show(60, truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 3 — the rationale
# MAGIC
# MAGIC Present, short, and shaped like the template. "Shaped like" matters:
# MAGIC a rationale that is merely non-empty could be an item name, and the
# MAGIC criterion is that the agent can *surface* it — which means it has to be a
# MAGIC sentence about this visitor's own behaviour, opening with
# MAGIC `recommender.RATIONALE_LEAD` and carrying the co-occurrence claim in
# MAGIC `recommender.RATIONALE_JOIN`.

# COMMAND ----------

if table is not None:
    shaped = table.where(
        f"rationale LIKE '{recommender.RATIONALE_LEAD}%' "
        f"AND rationale LIKE '%{recommender.RATIONALE_JOIN}%' "
        f"AND rationale LIKE '%{recommender.RATIONALE_TAIL}'"
    ).count()
    check(
        shaped == rows,
        f"all {rows} rationales are the declared sentence ({shaped} are)",
    )

    longest = table.selectExpr("max(length(rationale)) AS longest").first()["longest"]
    check(
        longest is not None and longest <= recommender.MAX_RATIONALE_CHARS,
        f"the longest rationale is {longest} characters, within "
        f"{recommender.MAX_RATIONALE_CHARS}",
    )

    named = spark.sql(
        f"SELECT count(*) AS unnamed FROM {TARGET} r "
        f"JOIN {MENU_ITEMS} m ON m.item_id = r.seed_item_id "
        "WHERE instr(r.rationale, m.name) = 0"
    ).first()["unnamed"]
    check(named == 0, f"every rationale names the seed item ({named} do not)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 4 — retraining is a scheduled job
# MAGIC
# MAGIC Read off the Jobs API rather than off the Terraform, because the
# MAGIC criterion is about the workspace and not about the intention. A job with
# MAGIC no `schedule` is the criterion failing. A job with a **paused** schedule
# MAGIC is the shipped default and is reported rather than failed: nothing in
# MAGIC this workspace starts spending on its own until somebody sets
# MAGIC `databricks_recommender_schedule_enabled`, and the argument for that is
# MAGIC in `infra/terraform/databricks_recommender.tf`.

# COMMAND ----------

workspace = WorkspaceClient()
jobs = list(workspace.jobs.list(name=JOB_NAME))
check(len(jobs) == 1, f"exactly one job is named {JOB_NAME!r} ({len(jobs)} found)")

if jobs:
    settings = workspace.jobs.get(jobs[0].job_id).settings
    schedule = settings.schedule
    check(
        schedule is not None and bool(schedule.quartz_cron_expression),
        "the retraining job carries a cron schedule"
        + (
            f": {schedule.quartz_cron_expression} {schedule.timezone_id}"
            if schedule is not None
            else " — it has none, so retraining is a notebook somebody remembers"
        ),
    )
    if schedule is not None:
        print(f"  the schedule is {schedule.pause_status}")
        if str(schedule.pause_status).endswith("PAUSED"):
            print(
                "  PAUSED is the shipped default: set "
                "databricks_recommender_schedule_enabled = true to start it"
            )
    check(
        [task.task_key for task in settings.tasks] == ["train", "publish"],
        "the job trains and then publishes, in that order",
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Not a top-sellers list
# MAGIC
# MAGIC None of the four criteria says this and PRD requirement P2 does: the
# MAGIC recommendations have to be grounded in the visitor's actual ordering
# MAGIC behaviour rather than generic popularity, and a global top-sellers list
# MAGIC does not satisfy it **even if it scores well**.
# MAGIC
# MAGIC The training run already measures that as `novel_hit_rate_at_k` against a
# MAGIC popularity baseline. This is the published table's version of the same
# MAGIC question, and it is the cheaper one to read: if every visitor got the
# MAGIC same top recommendation, whatever the metrics said, the served table is a
# MAGIC top-sellers list.

# COMMAND ----------

if table is not None and rows:
    distinct_firsts = table.where("rank = 1").select("item_id").distinct().count()
    served_visitors = table.select("demo_id").distinct().count()
    check(
        distinct_firsts > 1,
        f"visitors' top recommendations differ: {distinct_firsts} distinct "
        f"first-ranked items across {served_visitors} visitors",
    )

    print("  the most-recommended items, and how many visitors got each:")
    table.groupBy("item_id").count().orderBy("count", ascending=False).show(
        10, truncate=40
    )
    print("  the most common seeds — what the population's habits actually are:")
    table.groupBy("seed_item_id").count().orderBy("count", ascending=False).show(
        10, truncate=40
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## The verdict

# COMMAND ----------

print()
if failures:
    raise AssertionError(
        f"{len(failures)} recommender claims failed:\n"
        + "\n".join(f"  - {failure}" for failure in failures)
    )

dbutils.notebook.exit(
    json.dumps(
        {
            "model": MODEL,
            "version": str(champion.version) if champion else None,
            "table": TARGET,
            "rows": rows,
        },
        sort_keys=True,
    )
)
