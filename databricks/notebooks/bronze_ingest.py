# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze ingestion — both streams, from ADLS, with Auto Loader
# MAGIC
# MAGIC Issue [#33](https://github.com/gganssle/chip_chat/issues/33). This is the
# MAGIC source of the `chip-chat-bronze-ingest` Lakeflow Spark Declarative
# MAGIC Pipeline, and it is deliberately almost empty: every decision that is not
# MAGIC "call Spark" lives in `chip_chat.databricks.bronze`, where it is a value a
# MAGIC test can read without a cluster. This file is the loop.
# MAGIC
# MAGIC **`from pyspark import pipelines as dp`, not `import dlt`.** Delta Live
# MAGIC Tables became Lakeflow Spark Declarative Pipelines in 2026. The old module
# MAGIC still runs, and writing new code against it in 2026 is writing it
# MAGIC pre-deprecated. `docs/service-inventory.md` §3 has the migration table.
# MAGIC
# MAGIC **What the pipeline gives every row.** Four ingestion columns — when the
# MAGIC row was read, which file it came out of, when that file was last written
# MAGIC and how big it was — and, on every parsed source, a `_quarantined` flag.
# MAGIC `source_url` and `harvested_at` are not added here: they are already
# MAGIC columns on `raw_documents`, captured at fetch time by the harvest, which
# MAGIC is the only place they can honestly come from.
# MAGIC
# MAGIC **Configuration**, all four supplied by Terraform:
# MAGIC `chip_chat.raw_uri`, `chip_chat.catalog`, `chip_chat.checkpoint_uri`,
# MAGIC `chip_chat.lib_path`.

# COMMAND ----------

import sys

from pyspark import pipelines as dp

# The declarations are a workspace file uploaded beside this notebook, not an
# installed package: a pipeline runs a notebook on the driver and there is no
# wheel to import from. `bronze.py` and `catalog.py` are stdlib-only for exactly
# this reason, so the same files serve pytest and this driver unchanged.
LIB_PATH = spark.conf.get("chip_chat.lib_path")
if LIB_PATH not in sys.path:
    sys.path.insert(0, LIB_PATH)

import bronze  # noqa: E402
import catalog  # noqa: E402

RAW_URI = spark.conf.get("chip_chat.raw_uri")
CATALOG = spark.conf.get("chip_chat.catalog")
CHECKPOINT_URI = spark.conf.get("chip_chat.checkpoint_uri")

if CATALOG != catalog.CATALOG:
    raise ValueError(
        f"the pipeline is configured for catalog {CATALOG!r} but the layout "
        f"module names {catalog.CATALOG!r}; one of the two has drifted"
    )

print(f"catalog       {CATALOG}")
print(f"landing zone  {RAW_URI}")
print(f"checkpoints   {CHECKPOINT_URI}")
print(f"sources       {len(bronze.SOURCES)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## One streaming table per landing-zone location
# MAGIC
# MAGIC The table names are fully qualified because one pipeline writes into two
# MAGIC schemas — `bronze_harvested` and `bronze_synthetic`. Keeping both streams
# MAGIC in one pipeline is what the issue asks for and is also what makes a single
# MAGIC update the unit of cost: one cluster starts, both streams land, it stops.
# MAGIC
# MAGIC The expectation records rescued rows in the event log and keeps them.
# MAGIC `expect` rather than `expect_or_drop`: a bad record that is dropped is a
# MAGIC bad record nobody will ever see again.

# COMMAND ----------


def target_name(candidate):
    """Return the fully qualified bronze table for ``candidate``."""
    return catalog.table(bronze.LAYER, candidate.stream, candidate.table)


def define(candidate):
    """Declare the streaming table that lands ``candidate``.

    Defined in a function rather than in the loop body so that each closure
    binds its own source. A decorator applied inside a `for` captures the loop
    variable, and every table would then ingest the last source declared.
    """
    options = bronze.autoloader_options(candidate, checkpoint_uri=CHECKPOINT_URI)
    path = bronze.landing_path(RAW_URI, candidate)
    columns = bronze.metadata_columns(candidate)

    @dp.table(
        name=target_name(candidate),
        comment=candidate.comment,
        table_properties={
            "chip_chat.stream": candidate.stream,
            "chip_chat.issue": "gh-33",
            "delta.enableChangeDataFeed": "true",
        },
    )
    @dp.expect("record_parsed_cleanly", f"{bronze.QUARANTINED} = false")
    def _ingest():
        return (
            spark.readStream.format("cloudFiles")
            .options(**options)
            .load(path)
            .selectExpr("*", *columns)
        )

    return _ingest


def define_binary(candidate):
    """Declare a binary source, which has nothing to expect of its contents."""
    options = bronze.autoloader_options(candidate, checkpoint_uri=CHECKPOINT_URI)
    path = bronze.landing_path(RAW_URI, candidate)
    columns = bronze.metadata_columns(candidate)

    @dp.table(
        name=target_name(candidate),
        comment=candidate.comment,
        table_properties={
            "chip_chat.stream": candidate.stream,
            "chip_chat.issue": "gh-33",
        },
    )
    def _ingest():
        return (
            spark.readStream.format("cloudFiles")
            .options(**options)
            .load(path)
            .selectExpr("*", *columns)
        )

    return _ingest


for _source in bronze.SOURCES:
    if _source.is_parsed:
        define(_source)
    else:
        define_binary(_source)

# COMMAND ----------

# MAGIC %md
# MAGIC ## The quarantine
# MAGIC
# MAGIC One view per stream, over every parsed table in it. A row is here because
# MAGIC Auto Loader could not place some of it: a field the schema does not have,
# MAGIC a value whose type does not match the one already recorded, or a document
# MAGIC that did not parse at all. In every case the row is *also* in its bronze
# MAGIC table, with `_quarantined = true` — nothing is moved and nothing is
# MAGIC dropped, which is what lets the update finish with malformed input in the
# MAGIC landing zone.
# MAGIC
# MAGIC `_rescued_data` is the reason, verbatim. It names the offending fields and
# MAGIC carries the source path itself, so a second column restating it in prose
# MAGIC would be a worse copy of the evidence.

# COMMAND ----------


def define_quarantine(stream):
    """Declare the quarantine view for one stream."""
    parsed = [c for c in bronze.sources_for(stream) if c.is_parsed]
    name = catalog.table(bronze.LAYER, stream, bronze.QUARANTINE_TABLE)

    @dp.materialized_view(
        name=name,
        comment=(
            "Every row in this stream's bronze tables that Auto Loader could "
            "not fully place, with the rescued JSON that says why. The rows "
            "are still in their own tables, flagged; this view is where "
            "somebody notices them. Built by gh-33."
        ),
        table_properties={
            "chip_chat.stream": stream,
            "chip_chat.issue": "gh-33",
        },
    )
    def _quarantine():
        frame = None
        for candidate in parsed:
            rows = (
                spark.read.table(target_name(candidate))
                .where(bronze.QUARANTINED)
                .selectExpr(*bronze.quarantine_columns(candidate))
            )
            frame = rows if frame is None else frame.unionByName(rows)
        return frame

    return _quarantine


for _stream in bronze.STREAMS:
    define_quarantine(_stream)
