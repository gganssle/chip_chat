# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — the acceptance criteria, as assertions
# MAGIC
# MAGIC Issue [#36](https://github.com/gganssle/chip_chat/issues/36) has five
# MAGIC acceptance criteria and every one of them is a claim about a live system.
# MAGIC This notebook is those claims, run as the `chip-chat-gold-verify` job, so
# MAGIC SUCCESS means the assertions passed rather than the notebook merely
# MAGIC finishing. Same shape as `bronze_verify.py` and `silver_verify.py`, and
# MAGIC for the same reason.
# MAGIC
# MAGIC 1. **All four marts built, matching the schema exactly.** Every mart
# MAGIC    exists, holds rows, and publishes exactly the columns
# MAGIC    `chip_chat.databricks.gold` declares — in order, with the declared
# MAGIC    types. RFC-001 §04's schema is transcribed into `gold.RFC_COLUMNS` and
# MAGIC    `test_gold.py` holds the declarations to it, so a column checked here
# MAGIC    is a column the RFC named.
# MAGIC 2. **A known customer's usual order comes back right** — the Phase 3
# MAGIC    demo criterion. The known customers are `persona_fixtures`, which
# MAGIC    `chip_chat.data_gen.fixtures` measured independently, in Python, from
# MAGIC    the same history. Two derivations that never saw each other have to
# MAGIC    name the same entree.
# MAGIC 3. **`confidence` is calibrated.** Every Regular fixture lands in
# MAGIC    `gold.STATED` and every Explorer fixture in `gold.NO_USUAL`.
# MAGIC    `gold.CALIBRATION` is where that expectation is written down and
# MAGIC    `gold.CONFIDENCE_BANDS` is where the boundary is documented.
# MAGIC 4. **`derived_at` populated on every row**, on all four marts. RFC-001
# MAGIC    §10 serves a stale mart *with its timestamp*, and a null there is a
# MAGIC    mart that cannot be served stale honestly.
# MAGIC 5. **Marts rebuild deterministically from the same silver input.** The
# MAGIC    pipeline's own query is re-run here, twice, against the same silver
# MAGIC    tables — and both runs must equal the published mart on every column
# MAGIC    except `derived_at`, which is the wall clock and is meant to move.
# MAGIC
# MAGIC It also checks two things none of the five name. **The containment
# MAGIC property**: sums taken across marts have to agree, so
# MAGIC `sum(spend_summary.order_count)` must equal `customer_360.order_count`
# MAGIC for every visitor — which is what one settled-order rule buys, and what
# MAGIC two would quietly cost. And **the expectations really were fatal**, in
# MAGIC the sense `silver_verify.py` means it.
# MAGIC
# MAGIC Run it after the pipeline, from the `chip-chat-gold-verify` job. It reads
# MAGIC and never writes, so it is safe to run at any time.

# COMMAND ----------

import json
import sys

from pyspark.sql import functions as F  # noqa: N812

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog")
dbutils.widgets.text("lib_path", "", "Workspace directory holding gold.py")

sys.path.insert(0, dbutils.widgets.get("lib_path"))

import catalog  # noqa: E402
import gold  # noqa: E402

CATALOG = dbutils.widgets.get("catalog")

failures = []
counts = {}


def check(condition, message):
    """Record a failed claim rather than raising on the first one.

    A run that stops at the first failure tells you one thing per cluster
    start, and a cluster start here is four minutes and a few cents.
    """
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        failures.append(message)


def gold_name(candidate):
    return catalog.table(gold.LAYER, candidate.stream, candidate.name)


def silver_name(stream, name):
    return catalog.table(gold.SOURCE_LAYER, stream, name)


# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 1 — all four built, matching the schema exactly
# MAGIC
# MAGIC "Exactly" is the operative word and it is why this compares the whole
# MAGIC column list in order rather than checking that the ones it wants are
# MAGIC present. The agent's read tools query these columns by name; a mart that
# MAGIC has grown an extra column is a mart somebody has started using as a
# MAGIC scratchpad, and a mart whose columns have been reordered is a mart whose
# MAGIC positional consumers are about to be wrong quietly.

# COMMAND ----------

for candidate in gold.MARTS:
    name = gold_name(candidate)
    try:
        frame = spark.table(name)
        rows = frame.count()
    except Exception as error:  # the message is the finding
        check(False, f"{name} is not readable: {error}")
        continue
    counts[name] = rows
    check(rows > 0, f"{name} holds {rows} rows ({candidate.grain})")

    published = tuple(field.name for field in frame.schema.fields)
    declared = gold.column_names(candidate)
    check(
        published == declared,
        f"{name} publishes {list(declared)}"
        + ("" if published == declared else f" — it publishes {list(published)}"),
    )

    types = {
        field.name: field.dataType.simpleString().upper().replace(" ", "")
        for field in frame.schema
    }
    for column in candidate.columns:
        want = column.sql_type.upper().replace(" ", "")
        got = types.get(column.name, "<missing>")
        check(
            got == want,
            f"{name}.{column.name} is {want}"
            + ("" if got == want else f" — it is {got}"),
        )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 1, continued — the expectations really were fatal
# MAGIC
# MAGIC An expectation a table was published in spite of is an expectation that
# MAGIC was not enforced. Every constraint is re-run here as a filter and
# MAGIC required to match nothing, which is the same check `silver_verify.py`
# MAGIC makes and separates the same two cases: an `expect_all_or_fail` that
# MAGIC stopped the update leaves the *previous* mart in place and looks healthy
# MAGIC from outside, while a constraint downgraded to a warning leaves violating
# MAGIC rows in the current one. Only the second is visible here, and it is the
# MAGIC one that is a lie.
# MAGIC
# MAGIC This is also where **criterion 4** is settled: `derived_at` is a required
# MAGIC column on all four marts, so `derived_at_is_present` is one of the
# MAGIC constraints below and applies to every row.

# COMMAND ----------

for candidate in gold.MARTS:
    name = gold_name(candidate)
    if name not in counts:
        continue
    for expectation in gold.expectations(candidate):
        violations = spark.table(name).where(f"NOT ({expectation.constraint})")
        offenders = violations.count()
        check(
            offenders == 0,
            f"{name}: {expectation.name} holds"
            + ("" if offenders == 0 else f" — {offenders} rows do not"),
        )
        if offenders:
            violations.show(5, truncate=80)

# COMMAND ----------

# MAGIC %md
# MAGIC ## The containment one settled-order rule buys
# MAGIC
# MAGIC `gold.SETTLED_STATUSES` is applied to all four marts rather than to the
# MAGIC money ones only, so every count in this layer counts the same orders.
# MAGIC That makes an identity available that would otherwise be a coincidence:
# MAGIC a visitor's monthly order counts sum to their lifetime one, and their
# MAGIC monthly spend sums to their lifetime spend, exactly.
# MAGIC
# MAGIC Two columns both called `order_count` that counted different orders is
# MAGIC precisely the sort of thing that produces a confident wrong answer in
# MAGIC conversation. This is the assertion that they do not.

# COMMAND ----------

summary = spark.table(gold_name(gold.mart("spend_summary")))
customers = spark.table(gold_name(gold.mart("customer_360")))

rolled = summary.groupBy("demo_id").agg(
    F.sum("order_count").alias("rolled_orders"),
    F.sum("total").alias("rolled_spend"),
)
disagreement = customers.join(rolled, "demo_id", "full_outer").where(
    "rolled_orders IS NULL OR order_count IS NULL "
    "OR rolled_orders <> order_count OR rolled_spend <> lifetime_spend"
)
offenders = disagreement.count()
check(
    offenders == 0,
    "spend_summary sums to customer_360 for every visitor"
    + ("" if offenders == 0 else f" — {offenders} disagree"),
)
if offenders:
    disagreement.show(5, truncate=60)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 2 — a known customer's usual order
# MAGIC
# MAGIC The Phase 3 demo criterion, and the strongest check in this notebook,
# MAGIC because the answer it compares against was not computed here.
# MAGIC `persona_fixtures` is issue #26's table: `chip_chat.data_gen.fixtures`
# MAGIC measured each exemplar customer in Python, from the same eighteen months
# MAGIC of history, with its own definition of a commonest basket. The mart
# MAGIC computes its own in Spark and has never seen that code.
# MAGIC
# MAGIC They must name the same entree, built the same way. What they are *not*
# MAGIC required to agree on is a number: `usual_share` is the raw share over
# MAGIC every order, `confidence` is a lower bound over settled orders only, and
# MAGIC `chip_chat.data_gen.records.PersonaFixture.usual_share` says in its own
# MAGIC docstring that the two are deliberately not the same thing.

# COMMAND ----------

usual = spark.table(gold_name(gold.mart("usual_order")))
fixtures = spark.table(silver_name("synthetic", "persona_fixtures"))

known = fixtures.where("usual_item_id IS NOT NULL").join(usual, "demo_id", "left")

missing = known.where("item_id IS NULL")
absent = missing.count()
check(
    absent == 0,
    f"every one of the {known.count()} fixtures with a usual has a mart row"
    + ("" if absent == 0 else f" — {absent} do not"),
)
if absent:
    missing.selectExpr("demo_id", "persona_id", "usual_item_id").show(5, truncate=60)

wrong = known.where(
    "item_id IS NOT NULL AND ("
    "item_id <> usual_item_id "
    "OR sort_array(modifiers) <> sort_array(usual_modifiers))"
)
mismatched = wrong.count()
check(
    mismatched == 0,
    "the mart and issue #26's independent measurement name the same usual order"
    + ("" if mismatched == 0 else f" — {mismatched} disagree"),
)
if mismatched:
    wrong.selectExpr(
        "demo_id",
        "persona_id",
        "usual_item_id",
        "item_id",
        "usual_modifiers",
        "modifiers",
        "usual_share",
    ).show(10, truncate=60)

print()
print("  a sample, read end to end, which is what the criterion asks:")
known.selectExpr(
    "persona_id",
    "label",
    "usual_item_id",
    "item_id",
    "modifiers",
    "round(usual_share, 3) AS fixture_share",
    "confidence",
    "derived_at",
).orderBy("persona_id", "demo_id").show(12, truncate=60)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 3 — the confidence is calibrated
# MAGIC
# MAGIC `gold.CALIBRATION` names the two archetypes and where each must land,
# MAGIC and `gold.CONFIDENCE_BANDS` documents the boundary in words. The unit
# MAGIC tests already prove the arithmetic puts them there at the *bounds*
# MAGIC `population.toml` admits them on; this asserts it about the customers the
# MAGIC shipped population actually contains.
# MAGIC
# MAGIC The distribution is printed underneath, per archetype, because a
# MAGIC calibration that passes with every value pinned to one end is a
# MAGIC calibration nobody has looked at.

# COMMAND ----------

for calibration in gold.CALIBRATION:
    expected = calibration.expected
    theirs = known.where(f"persona_id = '{calibration.persona_id}'")
    population = theirs.count()
    check(population > 0, f"the population contains {calibration.persona_id} fixtures")
    if not population:
        continue
    ceiling = next(
        (other.floor for other in gold.CONFIDENCE_BANDS if other.floor > expected.floor),
        None,
    )
    predicate = f"confidence >= {expected.floor}"
    if ceiling is not None:
        predicate += f" AND confidence < {ceiling}"
    inside = theirs.where(predicate).count()
    check(
        inside == population,
        f"all {population} {calibration.persona_id} fixtures are "
        f"'{expected.name}' ({predicate})"
        + ("" if inside == population else f" — {inside} are"),
    )
    if inside != population:
        theirs.where(f"NOT ({predicate})").selectExpr(
            "demo_id", "confidence", "round(usual_share, 3) AS fixture_share"
        ).show(5, truncate=60)

print()
print("  what a band means, which is the other half of the criterion:")
for reading in gold.CONFIDENCE_BANDS:
    print(f"    >= {reading.floor}  {reading.name:9} {reading.meaning}")
    print(f"    {'':22} -> {reading.licence}")

print()
print("  and the distribution the assertion above is made against:")
known.groupBy("persona_id").agg(
    F.count("*").alias("fixtures"),
    F.round(F.min("confidence"), 4).alias("lowest"),
    F.round(F.expr("percentile_approx(confidence, 0.5)"), 4).alias("median"),
    F.round(F.max("confidence"), 4).alias("highest"),
).orderBy("persona_id").show(10, truncate=60)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 5 — a rebuild reproduces the mart
# MAGIC
# MAGIC The pipeline's own query, re-run here against the same silver tables, has
# MAGIC to produce the mart that is published. Twice, because two failures are
# MAGIC different findings: a rebuild that disagrees with the *published* mart
# MAGIC means silver moved under it or the query changed, and a rebuild that
# MAGIC disagrees with *itself* means the query is not a function of its input at
# MAGIC all — a tie broken on arrival order, or a float sum that depends on
# MAGIC partitioning.
# MAGIC
# MAGIC `derived_at` is excluded, and it is the only exclusion. It is the wall
# MAGIC clock and it is *supposed* to differ; every other column is a claim about
# MAGIC the same eighteen months of history and must not.

# COMMAND ----------


def rows_differ(left, right):
    """Return how many rows are in one frame and not the other, both ways."""
    return left.exceptAll(right).count() + right.exceptAll(left).count()


rebuilt = {}
for candidate in gold.MARTS:
    name = gold_name(candidate)
    if name not in counts:
        continue
    statement = gold.query(candidate, silver_name)
    comparable = [c for c in gold.column_names(candidate) if c != gold.DERIVED_AT]

    once = spark.sql(statement).select(*comparable).cache()
    twice = spark.sql(statement).select(*comparable)
    published = spark.table(name).select(*comparable)

    unstable = rows_differ(once, twice)
    check(
        unstable == 0,
        f"{name}: the query is a function of its input"
        + ("" if unstable == 0 else f" — {unstable} rows differ between two runs"),
    )

    drifted = rows_differ(once, published)
    check(
        drifted == 0,
        f"{name}: the published mart is what the query rebuilds"
        + ("" if drifted == 0 else f" — {drifted} rows differ"),
    )
    if drifted:
        once.exceptAll(published).show(5, truncate=60)
        published.exceptAll(once).show(5, truncate=60)

    rebuilt[name] = once.count()
    once.unpersist()

# COMMAND ----------

# MAGIC %md
# MAGIC ## What the marts actually say
# MAGIC
# MAGIC Not an assertion. The numbers a reviewer wants beside the ticket: how
# MAGIC many pairs `item_affinity` kept and how many the support threshold
# MAGIC excluded, what the strongest affinities are, and how the population is
# MAGIC distributed across the confidence bands.
# MAGIC
# MAGIC The excluded count is printed rather than left implicit because a
# MAGIC threshold that silently drops most of the table reads, from the outside,
# MAGIC exactly like a population with no affinities in it.

# COMMAND ----------

affinity = spark.table(gold_name(gold.mart("item_affinity")))
print(
    f"  item_affinity keeps {affinity.count()} ordered pairs at a support of "
    f"{gold.MINIMUM_CO_ORDERS} co-orders"
)
print("  the strongest affinities in the population:")
affinity.orderBy("lift", ascending=False).show(10, truncate=60)

print("  the confidence bands, over every visitor rather than the fixtures:")
bands = " ".join(
    f"WHEN confidence >= {b.floor} THEN '{b.name}'" for b in gold.CONFIDENCE_BANDS
)
usual.selectExpr(f"CASE {bands} END AS band").groupBy("band").count().orderBy(
    "band"
).show(truncate=40)

print("  and the shape of customer_360:")
customers.selectExpr(
    "count(*) AS visitors",
    "sum(CAST(lapsed_flag AS INT)) AS lapsed",
    "round(avg(order_count), 1) AS mean_orders",
    "round(avg(cadence_days), 1) AS mean_cadence_days",
    "max(last_order_at) AS observed_through",
).show(truncate=40)

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
        f"{len(failures)} gold claims failed:\n" + "\n".join(f"  - {f}" for f in failures)
    )

# The verdict, machine-readable, for the same reason bronze_verify.py and
# silver_verify.py end this way: a job's notebook output is what the run API
# returns, so the numbers this asserted on are quotable without opening the
# workspace.
dbutils.notebook.exit(json.dumps({"marts": counts, "rebuilt": rebuilt}, sort_keys=True))
