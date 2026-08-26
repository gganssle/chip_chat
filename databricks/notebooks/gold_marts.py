# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — the four personalization marts
# MAGIC
# MAGIC Issue [#36](https://github.com/gganssle/chip_chat/issues/36). This is the
# MAGIC source of the `chip-chat-gold-marts` Lakeflow Spark Declarative Pipeline,
# MAGIC and it is the shortest notebook in the lakehouse: every decision, and
# MAGIC this time every line of SQL as well, lives in
# MAGIC `chip_chat.databricks.gold`, where `databricks/tests/test_gold.py` reads
# MAGIC it without a cluster. This file is the loop.
# MAGIC
# MAGIC **This is the lane that earns Databricks its place here.** Snowflake is
# MAGIC the governed low-latency store the agent hits every turn; Databricks is
# MAGIC the batch engine that computes overnight what would be far too slow to
# MAGIC compute mid-conversation. `item_affinity` is a self-join over every order
# MAGIC in the population, and nobody is waiting on it inside a chat turn.
# MAGIC
# MAGIC **The SQL is in the module, not here.** Silver's tables are built the
# MAGIC same four ways, so a declaration plus a loop was the whole pipeline. Each
# MAGIC of these four is a genuinely different aggregation, so what the module
# MAGIC declares is the query itself, with every table name and every threshold
# MAGIC left as a placeholder `gold.query` fills. Two things follow, and both are
# MAGIC why it is worth the indirection:
# MAGIC
# MAGIC 1. A threshold cannot drift from the SQL that applies it, because there
# MAGIC    is exactly one of each and it is a constant with a docstring.
# MAGIC 2. `gold_verify.py` can run **the same query this pipeline ran**, against
# MAGIC    the same silver input, and compare. That is how the fifth acceptance
# MAGIC    criterion — marts rebuild deterministically from the same silver input
# MAGIC    — becomes an assertion instead of a hope.
# MAGIC
# MAGIC **Nothing here reads `demo_visitors`.** RFC-001 §04 answers PRD Q2 by
# MAGIC containment: the three fields a visitor may edit are columns of that
# MAGIC table, no editable field is an input to a mart, and so no edit can
# MAGIC invalidate one. The RFC says a reviewer checks the property by confirming
# MAGIC nothing under the medallion pipeline selects from it. `gold.sources()`
# MAGIC is the list, and `test_gold.py` checks the property over every query and
# MAGIC over this file.
# MAGIC
# MAGIC **Materialized views, not streaming tables**, for the reason silver's
# MAGIC are: an aggregate over eighteen months of orders has to be able to
# MAGIC recompute a row that a late-arriving order changed, and an append-only
# MAGIC stream cannot do that.
# MAGIC
# MAGIC **Configuration**, both supplied by Terraform:
# MAGIC `chip_chat.catalog`, `chip_chat.lib_path`.

# COMMAND ----------

import sys

from pyspark import pipelines as dp

# The declarations are workspace files uploaded beside this notebook, not an
# installed package — same arrangement, and same reason, as `bronze_ingest.py`
# and `silver_conform.py`. `gold.py` imports nothing but the standard library so
# that this upload is all the packaging there is.
LIB_PATH = spark.conf.get("chip_chat.lib_path")
if LIB_PATH not in sys.path:
    sys.path.insert(0, LIB_PATH)

import catalog  # noqa: E402
import gold  # noqa: E402

CATALOG = spark.conf.get("chip_chat.catalog")

if CATALOG != catalog.CATALOG:
    raise ValueError(
        f"the pipeline is configured for catalog {CATALOG!r} but the layout "
        f"module names {catalog.CATALOG!r}; one of the two has drifted"
    )

print(f"catalog        {CATALOG}")
print(f"marts          {len(gold.MARTS)}")
for _source in gold.sources():
    _name = catalog.table(gold.SOURCE_LAYER, _source.stream, _source.table)
    print(f"reads          {_name}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The marts
# MAGIC
# MAGIC One materialized view per entry in `gold.MARTS`, each built from the SQL
# MAGIC the module holds for it, each carrying its own `derived_at`.
# MAGIC
# MAGIC `derived_at` is the only wall-clock value in this layer and it is
# MAGIC deliberate: RFC-001 §10 requires a failed nightly job to serve stale
# MAGIC marts **with their timestamp**, never silently as fresh. Everything else
# MAGIC that needs a "now" — `lapsed_flag`, `cadence_days` — is measured against
# MAGIC the latest settled order in the population instead, so that a mart
# MAGIC rebuilt from unchanged silver is the mart it replaced.
# MAGIC
# MAGIC Every expectation is `expect_all_or_fail`. There is no warn level here
# MAGIC for a sharper reason than silver's: a mart is what the agent answers
# MAGIC from, so a row that violates its own definition is not a line in an event
# MAGIC log, it is a wrong answer in somebody's conversation.

# COMMAND ----------


def silver_name(stream, name):
    """Return the fully qualified silver table `name` in `stream`."""
    return catalog.table(gold.SOURCE_LAYER, stream, name)


def gold_name(stream, name):
    """Return a fully qualified gold table name."""
    return catalog.table(gold.LAYER, stream, name)


def build(candidate):
    """Declare the materialized view that publishes one mart.

    Defined in a function rather than in the loop body so that each closure
    binds its own mart. A decorator applied inside a `for` captures the loop
    variable, and every view would then publish the last mart declared.
    """
    constraints = {
        expectation.name: expectation.constraint
        for expectation in gold.expectations(candidate)
    }
    statement = gold.query(candidate, silver_name)

    @dp.materialized_view(
        name=gold_name(candidate.stream, candidate.name),
        comment=candidate.comment,
        table_properties={
            "chip_chat.stream": candidate.stream,
            "chip_chat.issue": "gh-36",
            "chip_chat.grain": candidate.grain,
            "delta.enableChangeDataFeed": "true",
        },
    )
    @dp.expect_all_or_fail(constraints)
    def _mart():
        return spark.sql(statement)

    return _mart


for _candidate in gold.MARTS:
    build(_candidate)
