# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — chunk the corpus at the boundaries its publishers drew
# MAGIC
# MAGIC Issue [#35](https://github.com/gganssle/chip_chat/issues/35). This is the
# MAGIC source of the `chip-chat-gold-chunk` Lakeflow Spark Declarative Pipeline.
# MAGIC Like `bronze_ingest.py` and `silver_conform.py` it is almost empty: every
# MAGIC decision that is not "call Spark" lives in `chip_chat.databricks.gold_chunks`,
# MAGIC where `databricks/tests/test_gold_chunks.py` runs it over the real harvest
# MAGIC fixtures without a cluster.
# MAGIC
# MAGIC **Chunking follows structure, not length** (RFC-001 §08). There is no
# MAGIC window size in this notebook and none in the module it calls. Six silver
# MAGIC tables become six kinds of chunk, and every boundary is one somebody else
# MAGIC drew:
# MAGIC
# MAGIC | Kind | Silver table | The boundary |
# MAGIC | --- | --- | --- |
# MAGIC | `MENU_ITEM` | `menu_items` | the menu's own item |
# MAGIC | `POLICY_SECTION` | `policy_sections` | the page's own heading |
# MAGIC | `FAQ_ENTRY` | `faq_entries` | the FAQ's own question |
# MAGIC | `ALLERGEN_CAVEAT` | `caveats` | the paragraph as published |
# MAGIC | `DOCUMENT_BLOCK` | `document_blocks` | the document's own heading |
# MAGIC | `NUTRITION_ROW` | `document_tables` | the table's own row |
# MAGIC
# MAGIC **Gold reads silver and never bronze.** A chunk built from bronze would be
# MAGIC a chunk of a row silver had quarantined, deduplicated away, or failed an
# MAGIC expectation on — which is a retrievable sentence that this lakehouse has
# MAGIC already decided is not true.
# MAGIC
# MAGIC **Gold reads the harvested stream and never the synthetic one.** RFC-001
# MAGIC §04 keeps the real catalogue and the invented account data apart, and this
# MAGIC is where blurring it would cost the most: an invented order reaching the
# MAGIC retrieval index is a fabricated fact with a real-looking citation on it.
# MAGIC There is no loop over `catalog.STREAMS` below, on purpose.
# MAGIC
# MAGIC **Configuration**, both supplied by Terraform:
# MAGIC `chip_chat.catalog`, `chip_chat.lib_path`.

# COMMAND ----------

import sys

from pyspark import pipelines as dp
from pyspark.sql import functions as F  # noqa: N812
from pyspark.sql.types import (
    ArrayType,
    BooleanType,
    DecimalType,
    IntegerType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

# The declarations are workspace files uploaded beside this notebook, not an
# installed package -- same arrangement, and same reason, as `bronze_ingest.py`
# and `silver_conform.py`. `gold_chunks.py` imports nothing but the standard
# library so that this upload is all the packaging there is.
LIB_PATH = spark.conf.get("chip_chat.lib_path")
if LIB_PATH not in sys.path:
    sys.path.insert(0, LIB_PATH)

import catalog  # noqa: E402
import gold_chunks  # noqa: E402
import silver  # noqa: E402

CATALOG = spark.conf.get("chip_chat.catalog")

if CATALOG != catalog.CATALOG:
    raise ValueError(
        f"the pipeline is configured for catalog {CATALOG!r} but the layout "
        f"module names {catalog.CATALOG!r}; one of the two has drifted"
    )

print(f"catalog        {CATALOG}")
print(f"chunk kinds    {len(gold_chunks.KINDS)}")
print(f"chunk fields   {len(gold_chunks.FIELDS)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The chunk struct
# MAGIC
# MAGIC Built from `gold_chunks.FIELDS` rather than beside it, so that a column
# MAGIC added to the schema arrives here without anyone remembering to add it. The one
# MAGIC thing this cell decides is how a SQL type name becomes a Spark type, and
# MAGIC an unknown one raises rather than defaulting to a string — a silently
# MAGIC stringified `DECIMAL` would put the calorie comparison back into prose,
# MAGIC which is the failure the whole schema exists to avoid.

# COMMAND ----------

_CITATION = StructType(
    [
        StructField("harvested_at", TimestampType()),
        StructField("source_url", StringType()),
    ]
)


def spark_type(sql_type):
    """Return the Spark type for one `gold_chunks.Field`'s declared SQL type."""
    if sql_type == "STRING":
        return StringType()
    if sql_type == "INT":
        return IntegerType()
    if sql_type == "BOOLEAN":
        return BooleanType()
    if sql_type == "TIMESTAMP":
        return TimestampType()
    if sql_type == "ARRAY<STRING>":
        return ArrayType(StringType())
    if sql_type.startswith("DECIMAL("):
        precision, scale = sql_type[len("DECIMAL(") : -1].split(",")
        return DecimalType(int(precision), int(scale))
    if sql_type.startswith("ARRAY<STRUCT<"):
        return ArrayType(_CITATION)
    raise ValueError(
        f"chip_chat.databricks.gold_chunks declares a column of type {sql_type!r} and "
        "this notebook does not know how to build it; add it here rather than "
        "letting it fall back to a string"
    )


CHUNK_COLUMNS = [
    F.col(entry.name).cast(spark_type(entry.sql_type)).alias(entry.name)
    for entry in gold_chunks.FIELDS
]

CONSTRAINTS = {entry.name: entry.constraint for entry in gold_chunks.expectations()}

# COMMAND ----------

# MAGIC %md
# MAGIC ## One kind at a time
# MAGIC
# MAGIC Each renderer is a Python function in `gold_chunks.py` that takes one row and
# MAGIC returns one chunk. It is wrapped in a UDF here for the same reason
# MAGIC `silver_conform.py` wraps its HTML reader: the decision belongs in a
# MAGIC module CI can run, and the driver is where the module happens to execute.
# MAGIC
# MAGIC A renderer that raises stops the update, and every one of them raises on
# MAGIC exactly the conditions silver already made impossible — a menu row with no
# MAGIC `item_id`, a table row with more cells than headings. That redundancy is
# MAGIC deliberate: it is the difference between "silver promised this" and "this
# MAGIC is true of the rows that actually arrived".

# COMMAND ----------

_CHUNK = StructType(
    [StructField(entry.name, spark_type(entry.sql_type)) for entry in gold_chunks.FIELDS]
)


def vocabulary_for(kind):
    """Return the published labels a renderer needs beside its row, or None.

    Collected to the driver rather than joined. `silver_harvested.allergens` is
    the published allergen vocabulary and it is nine rows; a join would
    multiply the menu by it to attach a label, and the label is the only thing
    wanted. It is read once, here, and closed over by the UDF.
    """
    entry = gold_chunks.source(kind)
    if entry.vocabulary is None:
        return None
    rows = (
        spark.read.table(
            catalog.table(silver.LAYER, gold_chunks.STREAM, entry.vocabulary)
        )
        .select("allergen_code", "name")
        .collect()
    )
    return {row["allergen_code"]: row["name"] for row in rows}


def renderer_for(kind):
    """Return a UDF wrapping the `gold_chunks` function that renders one kind."""
    render = getattr(gold_chunks, gold_chunks.source(kind).renderer)
    columns = gold_chunks.source(kind).columns
    vocabulary = vocabulary_for(kind)

    @F.udf(returnType=_CHUNK)
    def _render(*values):
        source_row = dict(zip(columns, values, strict=True))
        chunk = (
            render(source_row) if vocabulary is None else render(source_row, vocabulary)
        )
        row = chunk.as_row()
        # `harvested_at` arrives as whatever Spark handed the UDF and goes back
        # out unchanged; `gold_chunks` deliberately does not parse a timestamp
        # would have to guess the format of.
        #
        # The three arrays are tuples on the dataclass and lists here. The
        # citation array is also NARROWED here, by name: silver's citation
        # struct carries four fields and `gold_chunks.FIELDS` declares two, and a
        # struct handed back whole would be matched into the narrower type by
        # POSITION -- which is right today, by luck, and silently wrong the
        # first time silver reorders its struct.
        row[gold_chunks.ALLERGENS] = list(row[gold_chunks.ALLERGENS] or []) or None
        row[gold_chunks.COLUMN_HEADERS] = (
            list(row[gold_chunks.COLUMN_HEADERS] or []) or None
        )
        row["cells"] = list(row["cells"] or []) or None
        row[gold_chunks.CITATION] = [
            {"harvested_at": one["harvested_at"], "source_url": one["source_url"]}
            for one in (row[gold_chunks.CITATION] or ())
        ] or None
        return row

    return _render


def chunks_of(kind):
    """Return the chunks one silver table becomes, as a frame of `gold_chunks.FIELDS`."""
    entry = gold_chunks.source(kind)
    frame = spark.read.table(
        catalog.table(silver.LAYER, gold_chunks.STREAM, entry.table)
    ).select(*[F.col(column) for column in entry.columns])
    return (
        frame.withColumn("_chunk", renderer_for(kind)(*entry.columns))
        .select("_chunk.*")
        .withColumn(gold_chunks.CHUNKED_AT, F.current_timestamp())
        .select(*CHUNK_COLUMNS)
    )


# COMMAND ----------

# MAGIC %md
# MAGIC ## The chunk table
# MAGIC
# MAGIC One materialized view, and a materialized view rather than a streaming
# MAGIC table for the reason silver's are: a re-harvest that changes a published
# MAGIC calorie figure has to be able to *displace* the chunk already written, and
# MAGIC an append-only stream cannot do that. The chunk id is stable across the
# MAGIC rebuild — it names the item, not the wording — so the displacement is a
# MAGIC row updated in place rather than a citation retired.
# MAGIC
# MAGIC The union is by name and not by position. Six frames with the same
# MAGIC columns in the same order today is not a promise about tomorrow, and a
# MAGIC positional union that drifted would put `item_id` into `heading` and fail
# MAGIC no expectation at all.

# COMMAND ----------


@dp.materialized_view(
    name=catalog.table(gold_chunks.LAYER, gold_chunks.STREAM, gold_chunks.CHUNK_TABLE),
    comment=gold_chunks.CHUNK_COMMENT,
    table_properties={
        "chip_chat.stream": gold_chunks.STREAM,
        "chip_chat.issue": "gh-35",
        "delta.enableChangeDataFeed": "true",
    },
)
@dp.expect_all_or_fail(CONSTRAINTS)
def _corpus_chunks():
    frames = [chunks_of(kind) for kind in gold_chunks.KINDS]
    combined = frames[0]
    for frame in frames[1:]:
        combined = combined.unionByName(frame)
    return combined
