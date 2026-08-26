# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — #35's acceptance criteria, as assertions
# MAGIC
# MAGIC Issue [#35](https://github.com/gganssle/chip_chat/issues/35) has three
# MAGIC acceptance criteria. Two of them are already tests —
# MAGIC `databricks/tests/test_gold.py` runs the chunker over the recorded
# MAGIC nutrition sheet and the recorded catalogue, and `make ci` is where they
# MAGIC fail. What those cannot check is the live table, so this notebook is the
# MAGIC other half, run as the `chip-chat-gold-verify` job. SUCCESS means the
# MAGIC assertions passed rather than the notebook merely finishing. Same shape
# MAGIC as `silver_verify.py`, and for the same reason.
# MAGIC
# MAGIC 1. **Chunk table produced, metadata schema fixed.** The table exists,
# MAGIC    holds rows of every kind, and its columns are exactly `gold.FIELDS` —
# MAGIC    no more, no fewer, in that order. A column the pipeline stopped
# MAGIC    writing would show up here rather than as an empty facet in #48's
# MAGIC    index.
# MAGIC 2. **No nutrition table is split across a chunk boundary, and every
# MAGIC    chunk has a citable source.** Both as constraints re-run over the
# MAGIC    published table. An `expect_all_or_fail` quietly downgraded to a
# MAGIC    warning leaves violating rows in the current version, and that is the
# MAGIC    case this catches. It also counts the rows of every extracted table
# MAGIC    against the chunks made from them: a table of eight rows that produced
# MAGIC    seven chunks lost one, and a table that produced nine split one.
# MAGIC 3. **A sample of twenty chunks reviewed by hand for whether each is
# MAGIC    independently answerable.** This is a person's job and the notebook's
# MAGIC    part is to put the *same* twenty in front of them every time — sampled
# MAGIC    deterministically, spread across the kinds, printed whole. See
# MAGIC    `docs/corpus-chunking.md` for the review this produced.
# MAGIC
# MAGIC It also reports, without failing on, the chunks longer than
# MAGIC `gold.EMBEDDING_CHARACTER_BUDGET`. That is deliberate and is the whole of
# MAGIC #35's third bullet: a published section too long to embed well is a fact
# MAGIC about the publisher and an argument for a finer harvested boundary. It is
# MAGIC never an argument for inventing one here, so the number is printed and
# MAGIC nothing acts on it.
# MAGIC
# MAGIC Run it after the pipeline, from the `chip-chat-gold-verify` job. It reads
# MAGIC and never writes, so it is safe to run at any time.

# COMMAND ----------

import json
import sys

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog")
dbutils.widgets.text("lib_path", "", "Workspace directory holding gold.py")

sys.path.insert(0, dbutils.widgets.get("lib_path"))

import catalog  # noqa: E402
import gold  # noqa: E402
import silver  # noqa: E402

CATALOG = dbutils.widgets.get("catalog")

SAMPLE = 20
"""How many chunks the third criterion asks a person to read.

Not a random twenty each run. The sample below is ordered so that the same
twenty come back every time, which is what makes a hand review something you
can repeat after a change rather than a fresh opinion about fresh chunks.
"""

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


CHUNKS = catalog.table(gold.LAYER, gold.STREAM, gold.CHUNK_TABLE)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 1 — the table, and the schema it promised

# COMMAND ----------

try:
    chunks = spark.table(CHUNKS)
    total = chunks.count()
except Exception as error:  # the message is the finding
    raise AssertionError(f"{CHUNKS} is not readable: {error}") from error

counts["total"] = total
check(total > 0, f"{CHUNKS} holds {total} chunks")

declared = [entry.name for entry in gold.FIELDS]
check(
    chunks.columns == declared,
    "the chunk table's columns are exactly the declared metadata schema"
    + ("" if chunks.columns == declared else f" — it has {chunks.columns}"),
)

by_kind = {
    row[gold.KIND]: row["count"] for row in chunks.groupBy(gold.KIND).count().collect()
}
for kind in gold.KINDS:
    counts[kind] = by_kind.get(kind, 0)
    check(counts[kind] > 0, f"{kind}: {counts[kind]} chunks")

check(
    chunks.select(gold.CHUNK_ID).distinct().count() == total,
    "every chunk id is distinct — a collision is two facts one of which can "
    "never be quoted",
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 2 — the two required properties, over the live table
# MAGIC
# MAGIC Every expectation re-run as a filter and required to match nothing. The
# MAGIC two cases this separates are worth naming: an `expect_all_or_fail` that
# MAGIC stopped the update leaves the *previous* version of the table in place,
# MAGIC which looks healthy from outside, and a constraint downgraded to a
# MAGIC warning leaves violating rows in the current one. Only the second is
# MAGIC visible here, and it is the one that is a lie.

# COMMAND ----------

for expectation in gold.expectations():
    violations = chunks.where(f"NOT ({expectation.constraint})")
    offenders = violations.count()
    check(
        offenders == 0,
        f"{expectation.name} holds"
        + ("" if offenders == 0 else f" — {offenders} chunks do not"),
    )
    if offenders:
        violations.select(gold.CHUNK_ID, gold.KIND, gold.SOURCE_URL).show(
            5, truncate=False
        )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 2, continued — every extracted row became exactly one chunk
# MAGIC
# MAGIC The constraint above says each nutrition chunk holds one heading per
# MAGIC cell, which is the row arriving whole. This says the rows arrived at all:
# MAGIC a table of eight rows that produced seven chunks lost one, and one that
# MAGIC produced nine split one. Both are "split across a chunk boundary" seen
# MAGIC from the table's side rather than the chunk's, and neither is visible in
# MAGIC a per-row constraint.

# COMMAND ----------

tables = spark.table(catalog.table(silver.LAYER, gold.STREAM, "document_tables"))
extracted = tables.count()
rendered = chunks.where(f"{gold.KIND} = '{gold.NUTRITION_ROW}'").count()
counts["extracted_table_rows"] = extracted
counts["nutrition_chunks"] = rendered
check(
    extracted == rendered,
    f"{extracted} extracted table rows became {rendered} chunks, one each",
)

blocks = spark.table(catalog.table(silver.LAYER, gold.STREAM, "document_blocks")).count()
block_chunks = chunks.where(f"{gold.KIND} = '{gold.DOCUMENT_BLOCK}'").count()
counts["document_blocks"] = blocks
check(
    blocks == block_chunks,
    f"{blocks} deduplicated prose blocks became {block_chunks} chunks, one each",
)

items = spark.table(catalog.table(silver.LAYER, gold.STREAM, "menu_items")).count()
item_chunks = chunks.where(f"{gold.KIND} = '{gold.MENU_ITEM}'").count()
counts["menu_items"] = items
check(
    items == item_chunks,
    f"{items} menu items became {item_chunks} chunks — the issue's first rule, counted",
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Length: reported, never acted on
# MAGIC
# MAGIC `gold.EMBEDDING_CHARACTER_BUDGET` is the one number in this layer and it
# MAGIC splits nothing. A chunk over it is a published section whose publisher
# MAGIC stopped using headings — worth knowing, worth asking the harvest for a
# MAGIC finer boundary about, and never worth inventing a boundary over.

# COMMAND ----------

oversize = chunks.where(f"{gold.CHARACTER_COUNT} > {gold.EMBEDDING_CHARACTER_BUDGET}")
counts["over_budget"] = oversize.count()
print(
    f"  note   {counts['over_budget']} chunks exceed "
    f"{gold.EMBEDDING_CHARACTER_BUDGET} characters. Nothing was split."
)
if counts["over_budget"]:
    oversize.select(
        gold.KIND, gold.HEADING, gold.CHARACTER_COUNT, gold.SOURCE_URL
    ).orderBy(gold.CHARACTER_COUNT, ascending=False).show(10, truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 3 — twenty chunks, printed for a person to read
# MAGIC
# MAGIC The question a reader is answering is *is this chunk independently
# MAGIC answerable* — could somebody handed only this text, and told where it
# MAGIC came from, answer the question it is about without needing the chunk
# MAGIC either side of it. That is not a thing a job can assert, so the job's
# MAGIC part is to make the sample the same one every time: ordered by kind and
# MAGIC then by chunk id, so a re-run after a change puts the same twenty side by
# MAGIC side with the previous twenty.
# MAGIC
# MAGIC The review this produced is `docs/corpus-chunking.md` §6.

# COMMAND ----------

print(f"  {SAMPLE} chunks, deterministic, spread across the six kinds:")
print()
sample = (
    chunks.selectExpr(
        gold.KIND,
        gold.CHUNK_ID,
        gold.HEADING,
        gold.TEXT,
        gold.SOURCE_URL,
        gold.HARVESTED_AT,
        f"row_number() OVER (PARTITION BY {gold.KIND} ORDER BY {gold.CHUNK_ID}) AS _rank",
    )
    .where(f"_rank <= {max(1, SAMPLE // len(gold.KINDS))}")
    .drop("_rank")
    .orderBy(gold.KIND, gold.CHUNK_ID)
    .limit(SAMPLE)
)
for row in sample.collect():
    print(f"--- {row[gold.KIND]}  {row[gold.CHUNK_ID][:12]}")
    print(f"    heading: {row[gold.HEADING]}")
    print(f"    source:  {row[gold.SOURCE_URL]}  ({row[gold.HARVESTED_AT]})")
    print(f"    {row[gold.TEXT]}")
    print()

check(
    sample.count() > 0,
    f"the sample the hand review reads is not empty ({sample.count()} chunks)",
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
        f"{len(failures)} gold claims failed:\n" + "\n".join(f"  - {f}" for f in failures)
    )

# The verdict, machine-readable, for the same reason silver_verify.py ends this
# way: a job's notebook output is what the run API returns, so the numbers this
# asserted on are quotable without opening the workspace.
dbutils.notebook.exit(json.dumps(counts, sort_keys=True))
