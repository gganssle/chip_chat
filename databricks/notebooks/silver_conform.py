# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — clean, deduplicate and conform both streams
# MAGIC
# MAGIC Issue [#34](https://github.com/gganssle/chip_chat/issues/34). This is the
# MAGIC source of the `chip-chat-silver-conform` Lakeflow Spark Declarative
# MAGIC Pipeline. Like `bronze_ingest.py` it is almost empty: every decision that
# MAGIC is not "call Spark" lives in `chip_chat.databricks.silver`, where
# MAGIC `databricks/tests/test_silver.py` reads it without a cluster. This file
# MAGIC is the loop.
# MAGIC
# MAGIC **Bronze is what arrived; silver is what is true.** Three consequences,
# MAGIC and all three are visible in `conform()` below:
# MAGIC
# MAGIC 1. A row bronze flagged `_quarantined` never enters silver. Bronze is
# MAGIC    where a bad record is allowed to land — flagged, kept, queryable.
# MAGIC    Silver is where it is not allowed through.
# MAGIC 2. Every expectation is `expect_all_or_fail`. There is no warn level in
# MAGIC    this pipeline and no `expect_or_drop`, because a dropped row is a
# MAGIC    silent wrong answer six weeks later.
# MAGIC 3. Deduplication partitions on the **published key** and never on a
# MAGIC    display name. Two Chipotle items share a name across categories, and a
# MAGIC    dedup keyed on names would delete a menu item nobody removed.
# MAGIC
# MAGIC **Materialized views, not streaming tables.** Silver deduplicates over a
# MAGIC window and resolves foreign keys by joining, and neither is something a
# MAGIC streaming append can do honestly — a duplicate that arrives in a later
# MAGIC update has to be able to displace the row already written. A full
# MAGIC recompute each update is the correct semantics here and, at this corpus
# MAGIC size, also the cheap one.
# MAGIC
# MAGIC **Configuration**, both supplied by Terraform:
# MAGIC `chip_chat.catalog`, `chip_chat.lib_path`.

# COMMAND ----------

import sys

from pyspark import pipelines as dp
from pyspark.sql import functions as F  # noqa: N812
from pyspark.sql.types import (
    ArrayType,
    IntegerType,
    StringType,
    StructField,
    StructType,
)

# The declarations are workspace files uploaded beside this notebook, not an
# installed package — same arrangement, and same reason, as `bronze_ingest.py`.
# `silver.py` imports nothing but the standard library so that this upload is
# all the packaging there is, and so that the HTML reader below runs on the
# driver without a cluster library.
LIB_PATH = spark.conf.get("chip_chat.lib_path")
if LIB_PATH not in sys.path:
    sys.path.insert(0, LIB_PATH)

import bronze  # noqa: E402
import catalog  # noqa: E402
import silver  # noqa: E402

CATALOG = spark.conf.get("chip_chat.catalog")

if CATALOG != catalog.CATALOG:
    raise ValueError(
        f"the pipeline is configured for catalog {CATALOG!r} but the layout "
        f"module names {catalog.CATALOG!r}; one of the two has drifted"
    )

print(f"catalog        {CATALOG}")
print(f"conformed      {len(silver.TABLES)} tables")
print(f"corpus         {len(silver.CORPUS)} tables")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The conformed tables
# MAGIC
# MAGIC One materialized view per entry in `silver.TABLES`, built the same four
# MAGIC ways every time: drop what bronze quarantined, keep one row per published
# MAGIC key, cast the columns that arrive as strings, and resolve every foreign
# MAGIC key by joining.
# MAGIC
# MAGIC The join **carries a column** rather than leaving a boolean behind.
# MAGIC `order_items` comes out of silver with `item_name` on it, from the real
# MAGIC catalogue, and the expectation is that the name is not null. A
# MAGIC `_item_id_resolved` flag would be a receipt for a check and nothing else;
# MAGIC `item_name` is a column the serving layer wants anyway, and its nullness
# MAGIC is the violation. One column, two jobs.
# MAGIC
# MAGIC The join is a broadcast left join. Left, because an inner join would
# MAGIC *drop* the violating row and quietly satisfy the expectation it exists to
# MAGIC test — the single easiest way to write a referential-integrity check that
# MAGIC can never fail.

# COMMAND ----------


def bronze_name(candidate):
    """Return the fully qualified bronze table ``candidate`` reads."""
    source = bronze.source(candidate.source)
    return catalog.table(bronze.LAYER, source.stream, source.table)


def silver_name(stream, name):
    """Return a fully qualified silver table name."""
    return catalog.table(silver.LAYER, stream, name)


def latest_per(frame, expression):
    """Keep one row per key: the latest arrival wins, the file path breaks ties."""
    return (
        frame.withColumn(silver.DEDUP_RANK, F.expr(expression))
        .where(f"{silver.DEDUP_RANK} = 1")
        .drop(silver.DEDUP_RANK)
    )


def deduplicate(frame, candidate):
    """Keep one row per published key, for a table declared in `silver.TABLES`."""
    return latest_per(frame, silver.dedup_expression(candidate))


def resolve(frame, reference):
    """Left-join ``reference`` and carry its columns onto every row."""
    projection = [F.col(reference.key).alias("_reference_key")] + [
        F.col(source).alias(alias) for source, alias in reference.carries
    ]
    lookup = spark.read.table(silver_name(reference.stream, reference.table)).select(
        *projection
    )
    return frame.join(
        F.broadcast(lookup),
        frame[reference.column] == lookup["_reference_key"],
        "left",
    ).drop("_reference_key")


def conform(candidate):
    """Declare the materialized view that conforms one bronze table.

    Defined in a function rather than in the loop body so that each closure
    binds its own table. A decorator applied inside a `for` captures the loop
    variable, and every view would then conform the last table declared.
    """
    constraints = {
        expectation.name: expectation.constraint
        for expectation in silver.expectations(candidate)
    }

    @dp.materialized_view(
        name=silver_name(candidate.stream, candidate.name),
        comment=candidate.comment,
        table_properties={
            "chip_chat.stream": candidate.stream,
            "chip_chat.issue": "gh-34",
            "delta.enableChangeDataFeed": "true",
        },
    )
    @dp.expect_all_or_fail(constraints)
    def _conform():
        frame = spark.read.table(bronze_name(candidate)).where(
            f"NOT {silver.QUARANTINED}"
        )
        frame = deduplicate(frame, candidate)
        frame = frame.selectExpr(*silver.select_expressions(candidate))
        for reference in candidate.references:
            frame = resolve(frame, reference)
        return frame

    return _conform


for _table in silver.TABLES:
    conform(_table)

# COMMAND ----------

# MAGIC %md
# MAGIC ## The corpus: extraction
# MAGIC
# MAGIC The harvested bytes become prose here, and this is the one place in the
# MAGIC lakehouse that parses HTML. `silver.extract_blocks` is
# MAGIC `html.parser.HTMLParser` — the standard library, and the same parser the
# MAGIC harvest reads Chipotle's pages with — so it travels in the uploaded
# MAGIC workspace file and needs no cluster library.
# MAGIC
# MAGIC The pointer and the body are two bronze tables and are joined here on the
# MAGIC digest. `raw_bodies` is content-addressed: the blob's **file name is the
# MAGIC SHA-256 of its own bytes**, so the join key is the last segment of
# MAGIC `_source_path` rather than a column, and a body that fails that join is a
# MAGIC body whose pointer was never written.
# MAGIC
# MAGIC Only `text/html` is extracted. The JSON endpoints are already a table —
# MAGIC the catalogue — and the PDFs are read further down out of the Document
# MAGIC Intelligence extraction, which keeps their tables as tables.

# COMMAND ----------


def lib():
    """Return the ``silver`` module, importable in whichever process is asking.

    ⚠️ **A UDF body may not close over an imported module.** The `sys.path`
    entry added at the top of this notebook belongs to the *driver*. A Python
    UDF is cloudpickled and unpickled inside a Python worker — a separate
    process, forked from a daemon the cluster started before this notebook ran
    — and cloudpickle serializes a module global **by name**, emitting a bare
    `__import__("silver")` that the worker cannot satisfy. The failure is
    `ModuleNotFoundError: No module named 'silver'` inside a
    `SerializationError`, at the first row rather than at declaration, so the
    graph validates and the flow dies. Observed on `dbw-chip-chat`, 2026-08-26
    (gh-34), on the three corpus flows at once.

    Calling this instead keeps the module out of the closure: `LIB_PATH` is a
    string and is pickled by value, and a function defined in a notebook is
    pickled by value too, so what reaches the worker is the path and the import
    rather than a name it has to resolve.

    This is the same uploaded workspace file, on the same `sys.path`, and it
    stays true that the file pytest imports is the file that runs — the
    arrangement `bronze_ingest.py` describes, extended to the one layer that
    has UDFs at all.

    Returns:
        The ``silver`` declarations module.
    """
    import sys

    if LIB_PATH not in sys.path:
        sys.path.insert(0, LIB_PATH)
    import silver

    return silver


# COMMAND ----------

_BLOCK = StructType(
    [
        StructField("position", IntegerType()),
        StructField("heading", StringType()),
        StructField("text", StringType()),
        StructField(silver.BLOCK_SHA256, StringType()),
    ]
)

_TABLE_ROW = StructType(
    [
        StructField("table_index", IntegerType()),
        StructField("row_index", IntegerType()),
        StructField("caption", StringType()),
        StructField("page_number", IntegerType()),
        StructField("column_headers", ArrayType(StringType())),
        StructField("cells", ArrayType(StringType())),
    ]
)


@F.udf(returnType=ArrayType(_BLOCK))
def extract_blocks(body):
    """Return one document's visible prose, furniture removed.

    A body that will not decode as UTF-8 returns nothing rather than raising.
    That is not leniency about bad data — bronze already quarantined anything
    that failed to parse, and a mis-encoded HTML page is a fact about the
    publisher. It arrives here as a document with no blocks, which `_documents`
    excludes from the corpus and `silver_verify` counts and prints by URL.
    """
    if body is None:
        return []
    try:
        document = bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        return []
    module = lib()
    return [
        {
            "position": block.position,
            "heading": block.heading,
            "text": block.text,
            "block_sha256": module.block_digest(block.heading, block.text),
        }
        for block in module.extract_blocks(document)
    ]


@F.udf(returnType=StringType())
def text_sha256(blocks):
    """Return the digest of a document's extracted prose."""
    module = lib()
    return module.text_digest(
        tuple(
            module.Block(
                position=row["position"], heading=row["heading"], text=row["text"]
            )
            for row in (blocks or ())
        )
    )


@F.udf(returnType=ArrayType(StringType()))
def analysis_paragraphs(result):
    """Return the prose paragraphs of one Document Intelligence result."""
    return list(lib().analysis_paragraphs(result)) if result else []


@F.udf(returnType=ArrayType(_TABLE_ROW))
def analysis_table_rows(result):
    """Return every extracted table row, whole, with its column headings."""
    return list(lib().analysis_table_rows(result)) if result else []


def harvested_documents():
    """Return every harvested HTML body with its pointer's citation attached.

    The pointer carries `source_url` and `harvested_at`; the body carries the
    bytes. Neither is useful without the other, and the harvest is the only
    place either can honestly come from.
    """
    pointers = latest_per(
        spark.read.table(catalog.table(bronze.LAYER, "harvested", "raw_documents"))
        .where(f"NOT {silver.QUARANTINED}")
        .where(f"lower(content_type) LIKE '{silver.HTML_CONTENT_TYPE}%'"),
        silver.latest_row("requested_url"),
    ).selectExpr(
        "requested_url",
        "source_url",
        "harvested_at",
        "status_code",
        "content_sha256",
    )
    # The blob's file name IS the SHA-256 of its own bytes, so the digest is
    # read off the path rather than out of a column the binary reader does not
    # have. It is deduplicated on that digest for the same reason everything
    # else is deduplicated: a rewritten file is a second row of one fact.
    bodies = latest_per(
        spark.read.table(
            catalog.table(bronze.LAYER, "harvested", "raw_bodies")
        ).withColumn(
            "content_sha256",
            F.expr(f"element_at(split({silver.SOURCE_PATH}, '/'), -1)"),
        ),
        silver.latest_row("content_sha256"),
    ).selectExpr("content_sha256", "content AS body")
    return pointers.join(bodies, "content_sha256", "inner").withColumn(
        "blocks", extract_blocks(F.col("body"))
    )


# COMMAND ----------

# MAGIC %md
# MAGIC ## The corpus: deduplication
# MAGIC
# MAGIC A document is identified by the digest of its **prose**, not of its
# MAGIC bytes. Bronze already content-addresses the bytes, which is the right
# MAGIC identity for a landing zone and the wrong one for a corpus: two responses
# MAGIC differing only in a build hash or an A/B test's class name are two files
# MAGIC and one document.
# MAGIC
# MAGIC What deduplication conserves is **citations**. Every collapsed row adds
# MAGIC an entry to the array rather than disappearing, which is why
# MAGIC `silver_verify.py` can assert that the citation count after
# MAGIC deduplication equals the occurrence count before it. That equality is the
# MAGIC difference between removing a duplicate and losing a source.
# MAGIC
# MAGIC `source_url` and `harvested_at` are promoted out of the array to the
# MAGIC row — the most recent citation, because that is the harvest the text
# MAGIC currently reflects — so that a chunk can cite itself with one field and
# MAGIC the issue's fourth expectation is a column check rather than a traversal.

# COMMAND ----------


def _blocks_text(blocks):
    """Return a document's blocks rendered back into one readable string.

    Kept beside the structured `blocks` column rather than instead of it. The
    array is what #35 chunks; this is what a person reads when they open the
    table to check that the boilerplate really is gone, which is the whole of
    the third acceptance criterion.
    """
    return F.concat_ws(
        "\n\n",
        F.transform(
            blocks,
            lambda block: F.concat_ws(
                "\n", F.coalesce(block["heading"], F.lit("")), block["text"]
            ),
        ),
    )


@dp.materialized_view(
    name=silver_name("harvested", "documents"),
    comment=silver.corpus("documents").comment,
    table_properties={
        "chip_chat.stream": "harvested",
        "chip_chat.issue": "gh-34",
        "delta.enableChangeDataFeed": "true",
    },
)
@dp.expect_all_or_fail(
    {e.name: e.constraint for e in silver.corpus("documents").expectations}
)
def _documents():
    # ⚠️ **A fetched page that carries no prose is not a document.** Some pages
    # are fetched for a machine-readable value rather than for what they say —
    # `chipotle.policy.CATERING_URL` is read "for the address of its script
    # bundle, nothing else" — and what comes back is a Vue shell whose every
    # element is furniture by the tag list. It extracts to nothing, correctly.
    # Left in, it fails `says_something` and stops the whole layer over a page
    # nobody wanted the text of; the expectation is therefore gone and this
    # filter is in its place. It is the one row this pipeline removes without
    # failing, so `silver_verify` bounds it against
    # `silver.MAXIMUM_PROSELESS_SHARE` and prints every URL it excluded: a
    # stripper that regressed would empty the corpus rather than one page of
    # it. Observed on `dbw-chip-chat`, 2026-08-26 (gh-34).
    extracted = (
        harvested_documents()
        .withColumn("block_count", F.size("blocks"))
        .where("block_count > 0")
        .drop("block_count")
        .withColumn(silver.TEXT_SHA256, text_sha256("blocks"))
    )
    grouped = extracted.groupBy(silver.TEXT_SHA256).agg(
        F.sort_array(
            F.collect_list(
                F.struct(
                    F.col("harvested_at"),
                    F.col("source_url"),
                    F.col("requested_url"),
                    F.col("content_sha256"),
                    F.col("status_code"),
                )
            ),
            asc=False,
        ).alias(silver.CITATION),
        F.first("blocks").alias("blocks"),
    )
    return grouped.select(
        F.col(silver.TEXT_SHA256),
        F.col(silver.CITATION)[0]["source_url"].alias("source_url"),
        F.col(silver.CITATION)[0]["harvested_at"].alias("harvested_at"),
        _blocks_text(F.col("blocks")).alias("text"),
        F.size("blocks").alias("block_count"),
        F.col("blocks"),
        F.col(silver.CITATION),
        F.size(silver.CITATION).alias("citation_count"),
        F.current_timestamp().alias(silver.CONFORMED_AT),
    ).withColumn("character_count", F.length("text"))


# COMMAND ----------

# MAGIC %md
# MAGIC ## The corpus as facts
# MAGIC
# MAGIC One row per distinct block of prose, with every document that published
# MAGIC it in `citations`. This is "the same nutrition figure published on three
# MAGIC pages is one fact with three citations, not three facts", and it is the
# MAGIC table #35 chunks.
# MAGIC
# MAGIC `document_frequency` beside `corpus_documents` is the evidence that
# MAGIC boilerplate removal worked. Furniture is, by definition, the text that is
# MAGIC on nearly every page — but "nearly every page" is a claim about *pages
# MAGIC that have nothing else in common*, and a bare share cannot make that
# MAGIC distinction. When 86 percent of the corpus is one site section, a share
# MAGIC measures how the seed list was built and reports it as a verdict on the
# MAGIC stripper. That is not a hypothetical: it held this table at zero rows
# MAGIC from 26 August 2026, on one promotional module Chipotle publishes on all
# MAGIC thirty of its store pages and nowhere else.
# MAGIC
# MAGIC So the frequency is checked twice, and the two fail for different
# MAGIC reasons and say so by name in the event log.
# MAGIC `silver.FURNITURE_EXPECTATION` convicts on the share only once the block
# MAGIC crosses site sections, which is the part of "on nearly every page" that
# MAGIC a lopsided corpus cannot fake.
# MAGIC `silver.EVERY_DOCUMENT_EXPECTATION` is the floor underneath it: a block
# MAGIC on *every* document of a corpus of more than one is furniture whatever
# MAGIC the corpus is made of, and no composition argument excuses it.
# MAGIC
# MAGIC Half is a generous threshold on purpose, and it did not move — the
# MAGIC denominator did. A genuinely shared fact is exactly what this table is
# MAGIC *supposed* to collapse into one row with several citations, and the check
# MAGIC must not turn that success into a failure. That paragraph was written
# MAGIC before the case arrived and it described it exactly;
# MAGIC `docs/decisions/corpus-document-frequency.md` is the argument for
# MAGIC believing it rather than raising the number until the update passed.

# COMMAND ----------


@dp.materialized_view(
    name=silver_name("harvested", "document_blocks"),
    comment=silver.corpus("document_blocks").comment,
    table_properties={
        "chip_chat.stream": "harvested",
        "chip_chat.issue": "gh-34",
        "delta.enableChangeDataFeed": "true",
    },
)
@dp.expect_all_or_fail(
    {e.name: e.constraint for e in silver.corpus("document_blocks").expectations}
)
def _document_blocks():
    documents = spark.read.table(silver_name("harvested", "documents"))
    corpus_documents = documents.select(F.count("*").alias("corpus_documents"))
    occurrences = documents.select(
        F.col(silver.TEXT_SHA256),
        F.col("source_url"),
        F.col("harvested_at"),
        F.explode("blocks").alias("block"),
    )
    grouped = occurrences.groupBy(
        F.col("block")[silver.BLOCK_SHA256].alias(silver.BLOCK_SHA256)
    ).agg(
        F.sort_array(
            F.collect_list(
                F.struct(
                    F.col("harvested_at"),
                    F.col("source_url"),
                    F.col(silver.TEXT_SHA256),
                    F.col("block")["position"].alias("position"),
                )
            ),
            asc=False,
        ).alias(silver.CITATION),
        F.countDistinct(silver.TEXT_SHA256).alias(silver.DOCUMENT_FREQUENCY),
        F.first(F.col("block")["heading"]).alias("heading"),
        F.first(F.col("block")["text"]).alias("text"),
    )
    return grouped.crossJoin(F.broadcast(corpus_documents)).select(
        F.col(silver.BLOCK_SHA256),
        F.col("heading"),
        F.col("text"),
        F.length("text").alias("character_count"),
        F.col(silver.CITATION)[0]["source_url"].alias("source_url"),
        F.col(silver.CITATION)[0]["harvested_at"].alias("harvested_at"),
        F.col(silver.CITATION),
        F.size(silver.CITATION).alias("citation_count"),
        F.col(silver.DOCUMENT_FREQUENCY),
        F.col("corpus_documents"),
        F.current_timestamp().alias(silver.CONFORMED_AT),
    )


# COMMAND ----------

# MAGIC %md
# MAGIC ## The PDFs
# MAGIC
# MAGIC A row of an extracted table is present whole, with the column headings
# MAGIC that give its numbers meaning, or it is not present. Nothing here emits a
# MAGIC cell on its own: RFC-001 §08 is explicit that a nutrition table split
# MAGIC across a boundary produces exactly the confident wrong answers allergen
# MAGIC questions cannot tolerate, and a lone cell is that boundary at its worst.
# MAGIC
# MAGIC Header rows are not rows. They are carried on every row of their table,
# MAGIC which is where they are useful.

# COMMAND ----------


def analysed_documents():
    """Return each Document Intelligence reading with its citation attached.

    The analysis is keyed by the digest of the bytes it read, and the citation
    for those bytes is on the pointer that fetched them. Where several URLs
    served the same PDF the earliest is cited, which is deterministic and is
    the harvest that put the document in the corpus.
    """
    analyses = latest_per(
        spark.read.table(
            catalog.table(bronze.LAYER, "harvested", "document_analyses")
        ).where(f"NOT {silver.QUARANTINED}"),
        silver.latest_row("content_sha256", "model_id", "api_version"),
    ).selectExpr("content_sha256", "model_id", "api_version", "analyzed_at", "result")
    citations = (
        latest_per(
            spark.read.table(
                catalog.table(bronze.LAYER, "harvested", "raw_documents")
            ).where(f"NOT {silver.QUARANTINED}"),
            silver.latest_row("requested_url"),
        )
        .groupBy("content_sha256")
        .agg(
            F.min(F.struct("harvested_at", "source_url")).alias("citation"),
        )
        .select(
            "content_sha256",
            F.col("citation.source_url").alias("source_url"),
            F.col("citation.harvested_at").alias("harvested_at"),
        )
    )
    return analyses.join(citations, "content_sha256", "inner")


@dp.materialized_view(
    name=silver_name("harvested", "document_tables"),
    comment=silver.corpus("document_tables").comment,
    table_properties={
        "chip_chat.stream": "harvested",
        "chip_chat.issue": "gh-34",
        "delta.enableChangeDataFeed": "true",
    },
)
@dp.expect_all_or_fail(
    {e.name: e.constraint for e in silver.corpus("document_tables").expectations}
)
def _document_tables():
    rows = analysed_documents().withColumn(
        "row", F.explode(analysis_table_rows(F.col("result")))
    )
    return rows.select(
        F.col("content_sha256"),
        F.col("model_id"),
        F.col("api_version"),
        F.col("analyzed_at"),
        F.col("row")["table_index"].alias("table_index"),
        F.col("row")["row_index"].alias("row_index"),
        F.col("row")["caption"].alias("caption"),
        F.col("row")["page_number"].alias("page_number"),
        F.col("row")["column_headers"].alias("column_headers"),
        F.col("row")["cells"].alias("cells"),
        F.col("source_url"),
        F.col("harvested_at"),
        F.current_timestamp().alias(silver.CONFORMED_AT),
    )
