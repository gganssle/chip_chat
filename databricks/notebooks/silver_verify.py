# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — the acceptance criteria, as assertions
# MAGIC
# MAGIC Issue [#34](https://github.com/gganssle/chip_chat/issues/34) has three
# MAGIC acceptance criteria and every one of them is a claim about a live system.
# MAGIC This notebook is those claims, run as the `chip-chat-silver-verify` job,
# MAGIC so SUCCESS means the assertions passed rather than the notebook merely
# MAGIC finishing. Same shape as `bronze_verify.py`, and for the same reason.
# MAGIC
# MAGIC 1. **Silver tables exist for both streams with expectations enforced,
# MAGIC    failing the pipeline on violation.** Every declared table exists and
# MAGIC    holds rows, and the expectations were fatal — which this notebook can
# MAGIC    only observe indirectly, and does: it re-runs the constraint SQL and
# MAGIC    requires zero violating rows. An `expect_all_or_fail` that had been
# MAGIC    quietly downgraded would show up here as rows that exist and should
# MAGIC    not.
# MAGIC 2. **Deduplication measurably reduces the corpus without losing a
# MAGIC    distinct fact.** Two numbers, both computed: the reduction, and the
# MAGIC    conservation. Citations after deduplication must equal occurrences
# MAGIC    before it — that equality is the difference between removing a
# MAGIC    duplicate and losing a source. The reduction is asserted at the
# MAGIC    document level as an inequality and at the block level as an
# MAGIC    *equality* against the distinct `(heading, text)` pairs, recomputed
# MAGIC    here from the text rather than from the digest the pipeline grouped
# MAGIC    on. `distinct < occurrences` cannot tell a digest that stopped
# MAGIC    collapsing anything from a corpus in which no fact is published on
# MAGIC    two documents, and the seeded landing zone is the second of those.
# MAGIC 3. **Boilerplate removal verified against a sample of chunks.** Furniture
# MAGIC    is the text that is on nearly every page, so the assertion is that no
# MAGIC    surviving block appears in more than `MAXIMUM_DOCUMENT_SHARE` of the
# MAGIC    corpus. The sample is printed underneath it: the ten most widely
# MAGIC    repeated blocks, which is where a missed footer would be if there were
# MAGIC    one.
# MAGIC
# MAGIC It also checks the thing #34's brief is bluntest about, which is not one
# MAGIC of the three: **deduplication must not collapse two genuinely different
# MAGIC menu items that share a name.** Silver must hold one row per distinct
# MAGIC `item_id` in bronze, however many names collide.
# MAGIC
# MAGIC Run it after the pipeline, from the `chip-chat-silver-verify` job. It
# MAGIC reads and never writes, so it is safe to run at any time.

# COMMAND ----------

import json
import sys

dbutils.widgets.text("catalog", "chip_chat", "Unity Catalog catalog")
dbutils.widgets.text("lib_path", "", "Workspace directory holding silver.py")

sys.path.insert(0, dbutils.widgets.get("lib_path"))

import bronze  # noqa: E402
import catalog  # noqa: E402
import silver  # noqa: E402

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


def silver_name(stream, name):
    return catalog.table(silver.LAYER, stream, name)


# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 1 — both streams, conformed and populated

# COMMAND ----------

for stream in silver.STREAMS:
    print(f"{catalog.schema(silver.LAYER, stream)}")
    for candidate in silver.tables_for(stream):
        name = silver_name(stream, candidate.name)
        try:
            rows = spark.table(name).count()
        except Exception as error:  # the message is the finding
            check(False, f"{name} is not readable: {error}")
            continue
        counts[name] = rows
        check(rows > 0, f"{name} holds {rows} rows")

for entry in silver.CORPUS:
    name = silver_name(entry.stream, entry.name)
    try:
        rows = spark.table(name).count()
    except Exception as error:  # the message is the finding
        check(False, f"{name} is not readable: {error}")
        continue
    counts[name] = rows
    check(rows > 0, f"{name} holds {rows} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 1, continued — the expectations really were fatal
# MAGIC
# MAGIC An expectation that a table was published in spite of is an expectation
# MAGIC that was not enforced. So every constraint is re-run here as a filter and
# MAGIC required to match nothing. The two cases this separates are worth naming:
# MAGIC an `expect_all_or_fail` that stopped the update leaves the *previous*
# MAGIC version of the table in place, which looks healthy from the outside, and
# MAGIC a constraint downgraded to a warning leaves violating rows in the current
# MAGIC one. Only the second is visible here, and it is the one that is a lie.

# COMMAND ----------

for stream in silver.STREAMS:
    for candidate in silver.tables_for(stream):
        name = silver_name(stream, candidate.name)
        if name not in counts:
            continue
        for expectation in silver.expectations(candidate):
            violations = spark.table(name).where(f"NOT ({expectation.constraint})")
            offenders = violations.count()
            check(
                offenders == 0,
                f"{name}: {expectation.name} holds"
                + ("" if offenders == 0 else f" — {offenders} rows do not"),
            )
            if offenders:
                violations.show(5, truncate=80)

for entry in silver.CORPUS:
    name = silver_name(entry.stream, entry.name)
    if name not in counts:
        continue
    for expectation in entry.expectations:
        offenders = spark.table(name).where(f"NOT ({expectation.constraint})").count()
        check(offenders == 0, f"{name}: {expectation.name} holds ({offenders} do not)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## The hard failure the brief is bluntest about
# MAGIC
# MAGIC An order item that does not resolve to a catalogue row is not a warning.
# MAGIC The pipeline cannot publish `order_items` at all if one does not, so what
# MAGIC is left to check here is the *other* direction: that deduplication did
# MAGIC not quietly shrink the catalogue it resolves against.
# MAGIC
# MAGIC Silver must hold one row per distinct `item_id` bronze landed. Two
# MAGIC Chipotle items really do share a name across categories, so a silver
# MAGIC catalogue that is smaller than bronze's distinct count is a dedup keyed
# MAGIC on the wrong column — and the symptom downstream would be an order item
# MAGIC that stopped resolving to food nobody deleted.

# COMMAND ----------

items = silver_name("harvested", "menu_items")
landed = (
    spark.table(catalog.table(bronze.LAYER, "harvested", "menu_items"))
    .where(f"NOT {silver.QUARANTINED}")
    .selectExpr("item_id")
    .distinct()
    .count()
)
kept = counts.get(items, 0)
check(kept == landed, f"{items}: {kept} rows for {landed} distinct item_ids in bronze")

names = spark.table(items).selectExpr("name").distinct().count()
print(f"  note  {kept} items under {names} distinct names")
if names < kept:
    print("        names that collide, and both rows surviving:")
    spark.table(items).createOrReplaceTempView("_silver_menu_items")
    spark.sql(
        "SELECT name, count(*) AS rows, collect_list(item_id) AS item_ids, "
        "collect_list(category) AS categories FROM _silver_menu_items "
        "GROUP BY name HAVING count(*) > 1 ORDER BY rows DESC"
    ).show(10, truncate=80)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 2 — deduplication reduces, and conserves
# MAGIC
# MAGIC Two numbers. The **reduction** is what the criterion asks for: fewer
# MAGIC documents than fetched URLs, fewer facts than block occurrences. The
# MAGIC **conservation** is what stops the reduction from being achieved by
# MAGIC throwing things away — every occurrence that disappeared has to reappear
# MAGIC as a citation.
# MAGIC
# MAGIC A corpus that happens to contain no duplicates at all would pass
# MAGIC conservation and fail reduction, and that is the right way round: it
# MAGIC means the harvest changed, and somebody should look.

# COMMAND ----------

documents = silver_name("harvested", "documents")
blocks = silver_name("harvested", "document_blocks")

# Distinct requested_url, not row count: silver deduplicates the pointers on
# that key before it extracts anything, so a landing zone that was ingested twice
# would otherwise make conservation look broken when it is not.
fetched_urls = (
    spark.table(catalog.table(bronze.LAYER, "harvested", "raw_documents"))
    .where(f"NOT {silver.QUARANTINED}")
    .where(f"lower(content_type) LIKE '{silver.HTML_CONTENT_TYPE}%'")
    .selectExpr("requested_url")
    .distinct()
)
fetched = fetched_urls.count()
distinct_documents = counts.get(documents, 0)
cited = spark.table(documents).selectExpr(f"sum(size({silver.CITATION}))").first()[0]

# Every fetched URL is either a citation on a document or a page that carried no
# prose, and this is where the second of those stops being invisible. The corpus
# excludes a proseless page rather than failing on it -- see
# `silver.MAXIMUM_PROSELESS_SHARE` -- which is the only row removal in the layer
# that does not stop an update, so it is counted, bounded and named here rather
# than trusted. Conservation is then the whole of `fetched` accounted for, which
# is a stronger statement than the citation total alone: a document that
# silently lost a citation and a page that was silently dropped now look
# different from each other.
excluded_urls = fetched_urls.join(
    spark.table(documents)
    .selectExpr(f"explode({silver.CITATION}) AS citation")
    .selectExpr("citation.requested_url AS requested_url"),
    "requested_url",
    "left_anti",
)
proseless = excluded_urls.count()

check(
    distinct_documents < fetched,
    f"{documents}: {fetched} distinct fetched HTML URLs reduced to "
    f"{distinct_documents} distinct documents",
)
check(
    cited + proseless == fetched,
    f"{documents}: {cited} citations and {proseless} pages carrying no prose "
    f"account for all {fetched} fetched URLs — deduplication lost no source",
)
check(
    proseless <= fetched * silver.MAXIMUM_PROSELESS_SHARE,
    f"{documents}: {proseless} of {fetched} fetched HTML URLs extracted to "
    f"nothing, at or under the {silver.MAXIMUM_PROSELESS_SHARE:.0%} ceiling — "
    "a stripper that had regressed would empty the corpus, not one page of it",
)

if proseless:
    print()
    print("  fetched, and carrying no prose — excluded from the corpus:")
    excluded_urls.show(20, truncate=False)

occurrences = spark.table(documents).selectExpr("sum(block_count)").first()[0]
distinct_blocks = counts.get(blocks, 0)
block_citations = (
    spark.table(blocks).selectExpr(f"sum(size({silver.CITATION}))").first()[0]
)

# The block reduction is asserted as an EQUALITY against a number computed here
# from `(heading, text)`, and never as `distinct < occurrences`. The inequality
# looks stronger and is weaker, because it cannot tell the two things apart that
# both produce `distinct == occurrences`: a `block_digest` that stopped
# collapsing anything, and a corpus in which no fact is published on two
# documents. The seeded landing zone is the second — thirty store pages carrying
# one identical block collapse at the DOCUMENT level, and the six documents left
# share nothing — so the inequality fails there on a pipeline that is working.
#
# Recomputing the grouping from the text itself makes that distinction, and
# catches over-collapsing as well as under-collapsing, which the inequality
# never could. `extract_blocks` emits normalised text, so the two agree by
# construction; if they ever disagree it is the digest that moved.
distinct_pairs = (
    spark.table(documents)
    .selectExpr("explode(blocks) AS block")
    .selectExpr("block.heading AS heading", "block.text AS text")
    .distinct()
    .count()
)
collapsed = occurrences - distinct_blocks

check(
    distinct_blocks == distinct_pairs,
    f"{blocks}: {occurrences} block occurrences reduced to "
    f"{distinct_blocks} distinct facts, which is exactly the "
    f"{distinct_pairs} distinct (heading, text) pairs the corpus published — "
    f"{collapsed} occurrence(s) collapsed",
)
check(
    block_citations == occurrences,
    f"{blocks}: {block_citations} citations for {occurrences} occurrences — "
    "no fact lost its provenance",
)

print()
print("  the most widely cited facts — a fact on many pages is the point:")
spark.table(blocks).orderBy(silver.DOCUMENT_FREQUENCY, ascending=False).selectExpr(
    "heading",
    "substring(text, 1, 90) AS text",
    silver.DOCUMENT_FREQUENCY,
    "corpus_documents",
).show(10, truncate=90)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Criterion 3 — boilerplate removal, verified on a sample
# MAGIC
# MAGIC The sample above is the assertion below. Navigation, a footer and a
# MAGIC cookie banner are the text that is on nearly every page, so if the
# MAGIC stripper missed one it is sitting at the top of that list with a document
# MAGIC frequency equal to the size of the corpus.
# MAGIC
# MAGIC The threshold is deliberately generous. A genuinely shared fact — the
# MAGIC allergen caveat, the same nutrition figure on three pages — is exactly
# MAGIC what the previous cell is celebrating, and this one must not turn that
# MAGIC success into a failure. Furniture does not appear on half a site; it
# MAGIC appears on all of it.

# COMMAND ----------

worst = (
    spark.table(blocks)
    .selectExpr(
        "heading",
        "text",
        silver.DOCUMENT_FREQUENCY,
        "corpus_documents",
        f"{silver.DOCUMENT_FREQUENCY} / corpus_documents AS share",
    )
    .orderBy("share", ascending=False)
    .limit(5)
    .collect()
)

for row in worst:
    check(
        row["share"] <= silver.MAXIMUM_DOCUMENT_SHARE,
        f"{blocks}: a block appears in {row['share']:.0%} of the corpus "
        f"(limit {silver.MAXIMUM_DOCUMENT_SHARE:.0%}): "
        f"{(row['heading'] or row['text'])[:70]!r}",
    )

print()
print("  and a sample of chunks, read end to end, which is what the criterion asks:")
spark.table(documents).selectExpr("source_url", "substring(text, 1, 400) AS text").show(
    3, truncate=False
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
        f"{len(failures)} silver claims failed:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )

# The verdict, machine-readable, for the same reason bronze_verify.py ends this
# way: a job's notebook output is what the run API returns, so the numbers this
# asserted on are quotable without opening the workspace.
dbutils.notebook.exit(
    json.dumps(
        {
            "tables": counts,
            "fetched_html": fetched,
            "distinct_documents": distinct_documents,
            "block_occurrences": occurrences,
            "distinct_blocks": distinct_blocks,
        },
        sort_keys=True,
    )
)
