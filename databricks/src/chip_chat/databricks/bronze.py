"""What lands in bronze, from where, and under which Auto Loader options.

Issue #33 asks for "a declarative pipeline carrying both streams" out of the
ADLS landing zone and into bronze, untransformed. The pipeline itself is
``databricks/notebooks/bronze_ingest.py``, and it is deliberately almost empty:
it loops over :data:`SOURCES` and defines one streaming table per entry. Every
decision that is not "call Spark" lives here instead, where it can be read in
one screen and asserted by ``databricks/tests/test_bronze.py`` without a
cluster.

**This module imports nothing but the standard library, and that is
load-bearing.** A Lakeflow pipeline runs a notebook in the workspace, not an
installed wheel, so the same file has to be importable two ways: as
``chip_chat.databricks.bronze`` under pytest, and as a flat top-level ``bronze``
on the driver, where Terraform uploads this exact file beside the notebook and
the notebook puts its directory on ``sys.path``. An import of a sibling module
would work in the first case and fail in the second, which is why the one
constant this module shares with :mod:`chip_chat.databricks.catalog` — the
``bronze_<stream>`` schema name — is spelled out in :func:`schema_name` and
asserted equal to the catalog module's answer in the tests rather than imported.

**What is here and what is not.** #33 enumerates the harvested side: "HTML/JSON
responses, Document Intelligence extractions, PDFs". That is the fetch-once
cache in ``raw/`` and the analysis cache in ``analysis/`` — the corpus, and the
first three entries in :data:`SOURCES`.

The parsed tables under ``parsed/`` and ``catalog/`` are the harvest package's
*output* rather than the corpus, and five of their names collide between the two
directories — ``menu_items``, ``item_prices``, ``stores``, ``caveats`` and
``item_allergens`` are each written both by a parser and by the catalogue build.
#33 refused to choose between them by naming convention and left the choice to
conformance, which is #34; #34 made it, and it is recorded above
:data:`_REFERENCE`. In one line: the catalogue is the consolidation of the
parsers, so the catalogue wins for every name it publishes and ``parsed/`` is
read only for what the catalogue does not consolidate. Those tables are landed
here, tagged ``gh-34``, because a reference table silver conforms against has to
arrive the same way everything else does — through a checkpoint that makes a
re-run idempotent and a quarantine that catches a document that did not parse.

**Idempotence is a property of the checkpoint, not of a key.** Auto Loader
records the files it has consumed in ``cloudFiles.schemaLocation`` and never
reads one twice, so re-running the pipeline over an unchanged landing zone
appends nothing. ``cloudFiles.allowOverwrites`` is deliberately absent: turning
it on would make a rewritten file at the same path re-ingest, which is exactly
the duplication the second acceptance criterion forbids.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

__all__ = [
    "INGESTED_AT",
    "JSON",
    "LAYER",
    "QUARANTINED",
    "QUARANTINE_TABLE",
    "RESCUED_DATA",
    "SOURCES",
    "SOURCE_MODIFIED_AT",
    "SOURCE_PATH",
    "SOURCE_SIZE_BYTES",
    "STREAMS",
    "Source",
    "autoloader_options",
    "landing_path",
    "metadata_columns",
    "quarantine_columns",
    "quarantine_predicate",
    "schema_location",
    "schema_name",
    "source",
    "sources_for",
]

LAYER: Final = "bronze"
"""The medallion layer everything in this module writes to."""

STREAMS: Final[tuple[str, ...]] = ("harvested", "synthetic")
"""The two populations, as plain strings.

``chip_chat.databricks.catalog.STREAMS`` is the definition; this is a copy,
because this module may not import that one. ``test_bronze.py`` asserts the two
agree, so a stream added there and forgotten here fails ``make ci``.
"""

JSON: Final = "json"
"""``cloudFiles.format`` for a document the harvest wrote as JSON."""

BINARY: Final = "binaryFile"
"""``cloudFiles.format`` for bytes we refuse to interpret at all."""

# --- The columns every bronze row carries beyond its own -------------------
#
# Underscore-prefixed, so they cannot collide with a field the landing zone
# already has and so a `SELECT * EXCEPT (...)` in silver can strip them in one
# clause. Spark's own `_metadata` column is where three of the four come from;
# it is a hidden column on every file source and survives schema evolution.

INGESTED_AT: Final = "_ingested_at"
"""When the pipeline read the row. Not when the page was fetched — that is
``harvested_at``, and the two are different clocks on purpose."""

SOURCE_PATH: Final = "_source_path"
"""The full ``abfss://`` path of the file the row came out of."""

SOURCE_MODIFIED_AT: Final = "_source_modified_at"
"""The file's last-modified time, as ADLS reports it."""

SOURCE_SIZE_BYTES: Final = "_source_size_bytes"
"""The file's size in bytes."""

RESCUED_DATA: Final = "_rescued_data"
"""Auto Loader's rescued data column.

Everything the reader could place nowhere lands here as JSON: a field the schema
does not have, and a value whose type does not match the one already recorded.
It is what makes "type changes surfaced rather than coerced" true — a mismatched
value is moved here rather than cast, and the row still lands.

⚠️ **It does not catch a document that failed to parse as a whole.** A truncated
JSON file read with ``multiLine`` produces a row of nulls and a rescued data
column that is *empty*, so a quarantine keyed on this alone lets a corrupt
document through looking like a legitimately sparse one. Verified on
`dbw-chip-chat`, 2026-08-26, by seeding a truncated pointer (gh-33). That is why
:func:`quarantine_predicate` tests the row's identity as well.
"""

QUARANTINED: Final = "_quarantined"
"""Whether this row failed to arrive intact. See :func:`quarantine_predicate`.

A boolean column rather than a filter written out at each call site, so the
quarantine is queryable in bronze itself and not only through the view below.
"""

QUARANTINE_TABLE: Final = "quarantine"
"""Name of the per-stream view holding every quarantined row.

A table rather than a directory of rejected files. A quarantine path nobody
queries is where bad records go to be forgotten; a table in the same schema,
under the same grants, is where somebody notices them.
"""


@dataclass(frozen=True, slots=True)
class Source:
    """One landing-zone location and the bronze table it becomes.

    Attributes:
        table: The bronze table name, unqualified.
        stream: ``"harvested"`` or ``"synthetic"``.
        path: Key prefix under the raw container, without a leading slash.
            Auto Loader lists it recursively.
        fmt: ``cloudFiles.format``. :data:`JSON` or :data:`BINARY`.
        comment: What the table holds, for the Unity Catalog comment. Written
            for somebody reading the catalogue browser with no issue open.
        multiline: Whether one JSON document spans several lines. True for the
            documents the harvest writes with ``indent=2`` — the cache
            pointers, the Document Intelligence results and the population
            manifest — and false for the ``.jsonl`` tables, which are one
            object per line. Getting this wrong does not error: a multi-line
            document read line-by-line is simply an entire file of rescued
            data, which is why it is stated per source rather than guessed.
        identity: The columns that identify a row, from the record's own
            documentation rather than invented here. Bronze enforces nothing
            with them — it is a landing layer and a duplicate is a fact about
            the landing zone, not an error. They exist so that
            ``bronze_verify.py`` can assert the second acceptance criterion:
            after a re-run, ``COUNT(*)`` still equals ``COUNT(DISTINCT
            identity)``.
        schema_hints: ``cloudFiles.schemaHints``, one ``name TYPE`` clause per
            entry. Pinned deliberately narrowly — see
            :func:`autoloader_options`.
        glob: ``pathGlobFilter``, matched against the file name.
            Used where several tables share one directory, as the generated
            population does.
        issue: The issue that added this source, as the Unity Catalog table
            property ``chip_chat.issue``. Two issues land tables into these
            schemas — #33 brought the corpus and the generated population, #34
            brought the reference tables it needed to conform them against —
            and a table that cannot say which is a table nobody can trace to
            the argument that put it there.
    """

    table: str
    stream: str
    path: str
    fmt: str
    comment: str
    identity: tuple[str, ...]
    multiline: bool = False
    schema_hints: tuple[str, ...] = ()
    glob: str | None = None
    issue: str = "gh-33"

    @property
    def schema(self) -> str:
        """The unqualified schema this table lives in: ``bronze_harvested``."""
        return schema_name(self.stream)

    @property
    def is_parsed(self) -> bool:
        """Whether the reader interprets the bytes, and so can reject them.

        Only a parsed source can quarantine anything. :data:`BINARY` reads a
        file as an opaque blob and cannot fail to understand it, so a binary
        source has no rescued data column and is absent from the quarantine
        view. That is a fact about the format rather than an exemption: bytes
        we refuse to interpret cannot be malformed until something interprets
        them, which happens in silver.
        """
        return self.fmt == JSON


def schema_name(stream: str) -> str:
    """Return the unqualified schema for ``stream`` in the bronze layer.

    Args:
        stream: One of :data:`STREAMS`.

    Returns:
        ``bronze_harvested`` or ``bronze_synthetic``.

    Raises:
        ValueError: If ``stream`` is not one of :data:`STREAMS`. A pipeline
            configuration value arrives as a string nothing type-checked.
    """
    if stream not in STREAMS:
        raise ValueError(f"unknown stream {stream!r}; expected one of {STREAMS}")
    return f"{LAYER}_{stream}"


# --- The harvested corpus ---------------------------------------------------
#
# Three tables, and between them they hold what `chip_chat.harvest` wrote:
# a pointer per URL, a body per distinct set of bytes, and a Document
# Intelligence reading per PDF. The split is the harvest's own -- see the module
# docstring of `chip_chat.harvest.cache` -- and it is worth keeping rather than
# flattening, because the pointer is small and re-read often while the body is
# large and read once.
#
# `source_url` and `harvested_at` are on `raw_documents` and nowhere else. That
# is the whole reason the pointer is a table: RFC-001 section 08 needs both
# fields to survive into a response payload as citations, and by the time a
# chunk reaches the retrieval index there is nothing left to recover them from.

_HARVESTED: Final[tuple[Source, ...]] = (
    Source(
        table="raw_documents",
        identity=("requested_url",),
        stream="harvested",
        path="raw/index",
        fmt=JSON,
        multiline=True,
        comment=(
            "One row per harvested URL, exactly as the fetch-once cache wrote "
            "it. Carries source_url and harvested_at, which are captured at "
            "the edge because there is nowhere downstream to recover them "
            "from, and content_sha256, which joins to raw_bodies. Landed by "
            "the Auto Loader pipeline in gh-33; untransformed."
        ),
        schema_hints=(
            "requested_url STRING",
            "source_url STRING",
            "harvested_at TIMESTAMP",
            "status_code INT",
            "content_type STRING",
            "content_sha256 STRING",
            "content_key STRING",
            "previous_sha256 STRING",
        ),
    ),
    Source(
        table="raw_bodies",
        identity=("path",),
        stream="harvested",
        path="raw/blobs/sha256",
        fmt=BINARY,
        comment=(
            "The response bodies themselves — HTML, JSON and PDF — as bytes, "
            "content-addressed by their own SHA-256. Read as binaryFile "
            "because bronze does not decide what a document is; that is what "
            "content_type on raw_documents is for. Join on content_sha256, "
            "which is the file name. Landed by gh-33."
        ),
    ),
    Source(
        table="document_analyses",
        identity=("content_sha256", "model_id", "api_version"),
        stream="harvested",
        path="analysis",
        fmt=JSON,
        multiline=True,
        comment=(
            "Document Intelligence readings of the harvested PDFs, keyed by "
            "the digest of the bytes analysed and by the model and API "
            "version that read them. `result` is kept as the service's own "
            "JSON text rather than as an inferred struct: the shape varies "
            "per document and inferring it would evolve the schema on every "
            "new PDF. Landed by gh-33."
        ),
        schema_hints=(
            "content_sha256 STRING",
            "model_id STRING",
            "api_version STRING",
            "analyzed_at TIMESTAMP",
            "result STRING",
        ),
    ),
)


# --- The reference tables the accounts are conformed against ----------------
#
# Added by #34, and this is where the collision #33 recorded and deferred gets
# settled. Five table names -- `menu_items`, `item_prices`, `stores`, `caveats`
# and `item_allergens` -- are each written twice: once by a parser under
# `parsed/chipotle/*`, and once by the catalogue build under
# `catalog/chipotle/`. #33 refused to pick by naming convention and left the
# choice to conformance, which is #34.
#
# THE CATALOGUE WINS, EVERY TIME. The two files are not two candidates for one
# fact -- one is an input to the other. `catalog/records.py` says so in its own
# header: the parsed tables are "one harvest of one site", and the catalogue is
# "the consolidation three other subsystems resolve against". Landing both would
# land the same item twice under two names, which is exactly the duplication
# #34 exists to remove.
#
# So `parsed/` is read for nothing the catalogue publishes. What is read from it
# is only what the catalogue does not consolidate: the published policy prose,
# which is corpus rather than catalogue, and the Rewards Exchange line-up, which
# the loyalty ledger's redemptions have to resolve against.
#
# `parsed/chipotle/menu`, `parsed/chipotle/nutrition` and `parsed/chipotle/pdf`
# are therefore not ingested at all. Everything in them reaches the lakehouse
# through the catalogue, and the PDFs reach it as bytes and as a Document
# Intelligence reading already.

_CATALOGUE_PREFIX: Final = "catalog/chipotle"

_POLICY_PREFIX: Final = "parsed/chipotle/policy"

_ISSUE: Final = "gh-34"

_REFERENCE: Final[tuple[Source, ...]] = (
    Source(
        table="menu_items",
        identity=("item_id",),
        stream="harvested",
        path=_CATALOGUE_PREFIX,
        fmt=JSON,
        glob="menu_items.jsonl",
        issue=_ISSUE,
        comment=(
            "The consolidated menu: identity from the online menu, calories "
            "and allergen marks from the nutrition metadata, and a source_url "
            "per document merged rather than one that covers both. This is "
            "the table an order item has to resolve against, which is why it "
            "is here and the parser's own menu_items is not. Landed by gh-34 "
            "as the catalogue build wrote it; untransformed."
        ),
        schema_hints=(
            "item_id STRING",
            "name STRING",
            "category STRING",
            "item_type STRING",
            "primary_filling STRING",
            "description STRING",
            "is_composed BOOLEAN",
            "allergens ARRAY<STRING>",
            "allergen_disclosure STRING",
            "source_url STRING",
            "harvested_at TIMESTAMP",
            "nutrition_source_url STRING",
            "nutrition_harvested_at TIMESTAMP",
            "allergen_source_url STRING",
            "allergen_harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="item_prices",
        identity=("restaurant_id", "item_id"),
        stream="harvested",
        path=_CATALOGUE_PREFIX,
        fmt=JSON,
        glob="item_prices.jsonl",
        issue=_ISSUE,
        comment=(
            "What each restaurant charges for each item. Money is landed as "
            "the string the catalogue wrote, for the reason orders.total is: "
            "casting it is silver's job. Landed by gh-34."
        ),
        schema_hints=(
            "restaurant_id INT",
            "item_id STRING",
            "is_available BOOLEAN",
            "eligible_for_delivery BOOLEAN",
            "source_url STRING",
            "harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="modifiers",
        identity=("modifier_id",),
        stream="harvested",
        path=_CATALOGUE_PREFIX,
        fmt=JSON,
        glob="modifiers.jsonl",
        issue=_ISSUE,
        comment=(
            "What may go in which slot on which item. Identity is the pair "
            "<item_id>:<modifier_item_id>, because the same ingredient on a "
            "different item is a different modifier. Landed by gh-34."
        ),
        schema_hints=(
            "modifier_id STRING",
            "item_id STRING",
            "modifier_item_id STRING",
            "name STRING",
            "slot STRING",
            "derivation STRING",
            "group_name STRING",
            "modifier_type STRING",
            "min_quantity INT",
            "max_quantity INT",
            "is_default BOOLEAN",
            "portion_options ARRAY<STRING>",
            "source_url STRING",
            "harvested_at TIMESTAMP",
            "nutrition_source_url STRING",
            "nutrition_harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="stores",
        identity=("store_id",),
        stream="harvested",
        path=_CATALOGUE_PREFIX,
        fmt=JSON,
        glob="stores.jsonl",
        issue=_ISSUE,
        comment=(
            "The restaurants, with a published week of hours kept nested on "
            "the row rather than split into a side table a serialiser wanted. "
            "The name comes from the restaurant API and the address from the "
            "locator, which is why the row carries two provenances. Landed by "
            "gh-34."
        ),
        schema_hints=(
            "store_id INT",
            "name STRING",
            "street_address STRING",
            "city STRING",
            "region STRING",
            "postal_code STRING",
            (
                "hours ARRAY<STRUCT<day_of_week: STRING, opens: STRING, "
                "closes: STRING, is_published: BOOLEAN>>"
            ),
            "page_url STRING",
            "source_url STRING",
            "harvested_at TIMESTAMP",
            "profile_source_url STRING",
            "profile_harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="item_allergens",
        identity=("item_id", "allergen_code"),
        stream="harvested",
        path=_CATALOGUE_PREFIX,
        fmt=JSON,
        glob="item_allergens.jsonl",
        issue=_ISSUE,
        comment=(
            "One row per item per allergen code, carrying the published state "
            "verbatim: CONTAINS, NOT_LISTED or NOT_PUBLISHED. Landed by "
            "gh-34, three-valued, uncollapsed."
        ),
        schema_hints=(
            "item_id STRING",
            "allergen_code STRING",
            "status STRING",
            "source_url STRING",
            "harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="allergens",
        identity=("allergen_code",),
        stream="harvested",
        path=_CATALOGUE_PREFIX,
        fmt=JSON,
        glob="allergens.jsonl",
        issue=_ISSUE,
        comment=(
            "The published allergen vocabulary: the codes the chart uses and "
            "the words it renders them with. Landed by gh-34."
        ),
        schema_hints=(
            "allergen_code STRING",
            "name STRING",
            "badge_text STRING",
            "source_url STRING",
            "harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="caveats",
        identity=("position",),
        stream="harvested",
        path=_CATALOGUE_PREFIX,
        fmt=JSON,
        glob="caveats.jsonl",
        issue=_ISSUE,
        comment=(
            "The caveats Chipotle publishes beside the allergen chart, in "
            "published order — including the one saying foods contact one "
            "another during preparation and the chart does not reflect it. "
            "Landed by gh-34."
        ),
        schema_hints=(
            "position INT",
            "heading STRING",
            "text STRING",
            "source_url STRING",
            "harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="vocabulary",
        identity=("slot", "value"),
        stream="harvested",
        path=_CATALOGUE_PREFIX,
        fmt=JSON,
        glob="vocabulary.jsonl",
        issue=_ISSUE,
        comment=(
            "The slot vocabulary RFC-001 §07's stage-4 schema is generated "
            "from, with the item_ids each term resolves to. Landed by gh-34."
        ),
        schema_hints=(
            "slot STRING",
            "value STRING",
            "name STRING",
            "item_ids ARRAY<STRING>",
            "derivation STRING",
            "source_url STRING",
            "harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="catalog_manifest",
        identity=("catalog_version",),
        stream="harvested",
        path=_CATALOGUE_PREFIX,
        fmt=JSON,
        multiline=True,
        glob="manifest.json",
        issue=_ISSUE,
        comment=(
            "One row: which harvest produced this catalogue and the digest of "
            "every table in it. `tables` is kept as JSON text so a table added "
            "to the catalogue does not evolve this schema. Landed by gh-34."
        ),
        schema_hints=(
            "catalog_version STRING",
            "content_version STRING",
            "reference_restaurant_id INT",
            "restaurant_ids ARRAY<INT>",
            "tables STRING",
        ),
    ),
    Source(
        table="policy_documents",
        identity=("document_id",),
        stream="harvested",
        path=_POLICY_PREFIX,
        fmt=JSON,
        glob="policy_documents.jsonl",
        issue=_ISSUE,
        comment=(
            "The published policy pages, one row each, so their sections have "
            "something to hang off. From parsed/ rather than catalog/ because "
            "the catalogue consolidates food and this is prose. Landed by "
            "gh-34."
        ),
        schema_hints=(
            "document_id STRING",
            "kind STRING",
            "title STRING",
            "section_count INT",
            "source_url STRING",
            "harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="policy_sections",
        identity=("document_id", "position"),
        stream="harvested",
        path=_POLICY_PREFIX,
        fmt=JSON,
        glob="policy_sections.jsonl",
        issue=_ISSUE,
        comment=(
            "Policy prose split at the boundaries the page itself published. "
            "RFC-001 §08 chunks a terms document by section, and a boundary "
            "lost in the harvest cannot be recovered downstream. Landed by "
            "gh-34."
        ),
        schema_hints=(
            "document_id STRING",
            "position INT",
            "heading STRING",
            "text STRING",
            "source_url STRING",
            "harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="faq_categories",
        identity=("category_position", "subcategory_position"),
        stream="harvested",
        path=_POLICY_PREFIX,
        fmt=JSON,
        glob="faq_categories.jsonl",
        issue=_ISSUE,
        comment=(
            "The FAQ's published two-level table of contents, in its published "
            "order. That order is the document structure of a FAQ. Landed by "
            "gh-34."
        ),
        schema_hints=(
            "category STRING",
            "category_position INT",
            "subcategory STRING",
            "subcategory_position INT",
            "entry_count INT",
            "source_url STRING",
            "harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="faq_entries",
        identity=("category", "subcategory", "rank"),
        stream="harvested",
        path=_POLICY_PREFIX,
        fmt=JSON,
        glob="faq_entries.jsonl",
        issue=_ISSUE,
        comment=(
            "One published question and its answer. `links` carries every URL "
            "the answer pointed at, because the text keeps the words a link "
            "was made of and a URL living only in an href would be lost. "
            "Landed by gh-34."
        ),
        schema_hints=(
            "category STRING",
            "subcategory STRING",
            "rank INT",
            "question STRING",
            "answer STRING",
            "links ARRAY<STRING>",
            "is_top_question BOOLEAN",
            "source_url STRING",
            "harvested_at TIMESTAMP",
        ),
    ),
    Source(
        table="rewards",
        identity=("position",),
        stream="harvested",
        path=_POLICY_PREFIX,
        fmt=JSON,
        glob="rewards.jsonl",
        issue=_ISSUE,
        comment=(
            "The published Rewards Exchange line-up and what each reward "
            "costs, in points. Every redemption in the loyalty ledger has to "
            "resolve to a row here — which is why this one parsed table is "
            "landed and the rest of parsed/chipotle/policy is not. Landed by "
            "gh-34."
        ),
        schema_hints=(
            "position INT",
            "name STRING",
            "point_cost INT",
            "image_path STRING",
            "source_url STRING",
            "harvested_at TIMESTAMP",
        ),
    ),
)

# --- The generated account data ---------------------------------------------
#
# One table per file the generator writes, plus its manifest. They share a
# directory, so each is selected by `glob` rather than by its own prefix.
#
# The hints are all identity, provenance and always-null columns, and money is
# absent from them on purpose. `orders.total`, `order_items.unit_price` and
# `lifetime_spend` are written as strings so that the population digest is
# stable across machines; casting a string to a decimal is a transformation,
# and bronze does not transform. Silver casts them, and can say what it did.
#
# The always-null columns -- `thread_id`, `home_store_override`,
# `stated_preferences`, `home_store_name` -- are hinted precisely because they
# are always null. Inference over a column with no values in it produces no
# usable type, and the column would arrive the day a visitor first edits their
# persona rather than the day the table was created.

_SYNTHETIC_PREFIX: Final = "accounts/synthetic"

_SYNTHETIC: Final[tuple[Source, ...]] = (
    Source(
        table="personas",
        identity=("persona_id",),
        stream="synthetic",
        path=_SYNTHETIC_PREFIX,
        fmt=JSON,
        glob="personas.jsonl",
        comment=(
            "The seven archetypes the population was composed from. An "
            "archetype, not a customer. Landed by gh-33 as the generator "
            "wrote it."
        ),
        schema_hints=(
            "persona_id STRING",
            "label STRING",
            "home_store INT",
            "seed_points INT",
        ),
    ),
    Source(
        table="persona_fixtures",
        identity=("persona_id", "rank"),
        stream="synthetic",
        path=_SYNTHETIC_PREFIX,
        fmt=JSON,
        glob="persona_fixtures.jsonl",
        comment=(
            "The particular customers chosen to demonstrate each archetype, "
            "with the measurements the choice was made on. Landed by gh-33."
        ),
        schema_hints=(
            "demo_id STRING",
            "persona_id STRING",
            "label STRING",
            "rank INT",
            "home_store INT",
            "home_store_name STRING",
            "first_order_at TIMESTAMP",
            "last_order_at TIMESTAMP",
            "usual_item_id STRING",
            "usual_modifiers ARRAY<STRING>",
        ),
    ),
    Source(
        table="demo_visitors",
        identity=("demo_id",),
        stream="synthetic",
        path=_SYNTHETIC_PREFIX,
        fmt=JSON,
        glob="demo_visitors.jsonl",
        comment=(
            "The five hundred synthetic customers a public visitor is "
            "assigned one of. The three editable columns — display_name, "
            "home_store_override, stated_preferences — live here, and no "
            "gold mart reads this table: RFC-001 §04 makes that containment "
            "the mechanism rather than a rule. Landed by gh-33."
        ),
        schema_hints=(
            "demo_id STRING",
            "display_name STRING",
            "persona_id STRING",
            "thread_id STRING",
            "home_store_override INT",
            "stated_preferences STRING",
            "created_at TIMESTAMP",
            "last_seen TIMESTAMP",
        ),
    ),
    Source(
        table="orders",
        identity=("order_id",),
        stream="synthetic",
        path=_SYNTHETIC_PREFIX,
        fmt=JSON,
        glob="orders.jsonl",
        comment=(
            "Eighteen months of orders. `total` is landed as the string the "
            "generator wrote, not as a decimal: casting it is silver's job "
            "and bronze does not transform. Landed by gh-33."
        ),
        schema_hints=(
            "order_id STRING",
            "demo_id STRING",
            "store_id INT",
            "placed_at TIMESTAMP",
            "status STRING",
            "channel STRING",
            "priced_restaurant_id INT",
        ),
    ),
    Source(
        table="order_items",
        identity=("order_id", "line_number"),
        stream="synthetic",
        path=_SYNTHETIC_PREFIX,
        fmt=JSON,
        glob="order_items.jsonl",
        comment=(
            "Order lines, keyed by (order_id, line_number) so one order can "
            "hold two of the same item built differently. Every item_id is a "
            "real catalogue item. Landed by gh-33."
        ),
        schema_hints=(
            "order_id STRING",
            "line_number INT",
            "item_id STRING",
            "qty INT",
            "modifiers ARRAY<STRING>",
        ),
    ),
    Source(
        table="loyalty_ledger",
        identity=("entry_id",),
        stream="synthetic",
        path=_SYNTHETIC_PREFIX,
        fmt=JSON,
        glob="loyalty_ledger.jsonl",
        comment=(
            "Signed point movements, computed at Chipotle's published earn "
            "rate. `order_id` and `reward_name` are null for opening "
            "balances and expiries. Landed by gh-33."
        ),
        schema_hints=(
            "entry_id STRING",
            "demo_id STRING",
            "delta INT",
            "reason STRING",
            "order_id STRING",
            "reward_name STRING",
            "created_at TIMESTAMP",
        ),
    ),
    Source(
        table="population_manifest",
        identity=("population_version",),
        stream="synthetic",
        path=_SYNTHETIC_PREFIX,
        fmt=JSON,
        multiline=True,
        glob="manifest.json",
        comment=(
            "One row: which seed, which catalogue and which published rewards "
            "terms produced this population, and the digest of every table in "
            "it. The provenance record a mart that looks wrong is traced back "
            "through. `tables` is kept as JSON text so a table added to the "
            "population does not evolve this schema. Landed by gh-33."
        ),
        schema_hints=(
            "population_version STRING",
            "seed BIGINT",
            "catalog_content_version STRING",
            "rewards_content_version STRING",
            "window_starts_at TIMESTAMP",
            "window_ends_at TIMESTAMP",
            "tables STRING",
        ),
    ),
)

SOURCES: Final[tuple[Source, ...]] = _HARVESTED + _REFERENCE + _SYNTHETIC
"""Every landing-zone location bronze ingests, harvested stream first."""


def sources_for(stream: str) -> Iterator[Source]:
    """Yield every source belonging to ``stream``, in declaration order.

    Args:
        stream: One of :data:`STREAMS`.

    Yields:
        The matching sources.

    Raises:
        ValueError: If ``stream`` is unknown.
    """
    schema_name(stream)
    for candidate in SOURCES:
        if candidate.stream == stream:
            yield candidate


def source(table: str) -> Source:
    """Return the source producing the bronze table named ``table``.

    Args:
        table: An unqualified bronze table name.

    Returns:
        The source.

    Raises:
        KeyError: If no source produces that table.
    """
    for candidate in SOURCES:
        if candidate.table == table:
            return candidate
    raise KeyError(f"no bronze source produces {table!r}")


def landing_path(raw_uri: str, candidate: Source) -> str:
    """Return the ``abfss://`` directory Auto Loader lists for ``candidate``.

    Args:
        raw_uri: The raw container's URI, with or without a trailing slash.
        candidate: The source.

    Returns:
        ``<raw_uri>/<candidate.path>``.

    Raises:
        ValueError: If ``raw_uri`` is not an ``abfss://`` URI. ``wasbs://``
            reaches the same bytes without the hierarchical namespace, which
            is not what the lakehouse is built on, and the mistake is silent
            until directory semantics matter.
    """
    if not raw_uri.startswith("abfss://"):
        raise ValueError(
            f"raw_uri must be an abfss:// URI on the /dfs endpoint, got {raw_uri!r}"
        )
    return f"{raw_uri.rstrip('/')}/{candidate.path}"


def schema_location(checkpoint_uri: str, candidate: Source) -> str:
    """Return where Auto Loader keeps its schema and its file ledger.

    One directory per table, never shared. It holds both the inferred schema
    and the record of which files have been consumed, so two tables pointed at
    the same location would each believe they had already read the other's
    files — and the second one would land empty rather than fail.

    Args:
        checkpoint_uri: Root for every table's Auto Loader state.
        candidate: The source.

    Returns:
        ``<checkpoint_uri>/<schema>/<table>``.

    Raises:
        ValueError: If ``checkpoint_uri`` is not an ``abfss://`` URI.
    """
    if not checkpoint_uri.startswith("abfss://"):
        raise ValueError(
            f"checkpoint_uri must be an abfss:// URI, got {checkpoint_uri!r}"
        )
    return f"{checkpoint_uri.rstrip('/')}/{candidate.schema}/{candidate.table}"


def autoloader_options(candidate: Source, *, checkpoint_uri: str) -> dict[str, str]:
    """Return the reader options for ``candidate``, as Spark takes them.

    Three of the issue's five scope bullets are decided here, so each is
    spelled out rather than left to a default that happens to be right today.

    **Schema evolution is ``addNewColumns``** on every parsed source, and
    ``none`` on the binary one because Auto Loader will not accept anything else
    there. A file carrying a column the
    table does not have stops the update, records the wider schema, and is
    picked up whole by the retry — which a pipeline does for itself. New
    columns are therefore tolerated and no row is ever dropped for being
    unexpected. The alternative worth naming is ``rescue``, which would keep
    the new column's values in :data:`RESCUED_DATA` and never widen the table;
    that turns a new field into permanent quarantine, which is the opposite of
    tolerating it.

    **Type changes are surfaced, not coerced.** ``cloudFiles.inferColumnTypes``
    is on, so a column has a real type rather than being a string, and Auto
    Loader never re-types a column that already exists: a value that no longer
    fits goes to :data:`RESCUED_DATA` with the row's path beside it. The hints
    narrow where that can happen to the columns something downstream actually
    depends on — identities, the two citation fields, and the columns that are
    always null today and would otherwise have no type at all. Payload columns
    are left to inference, because bronze pinning a type it does not use is
    bronze having an opinion about content.

    **Bad records are quarantined, not dropped.** ``rescuedDataColumn`` is the
    single mechanism: a record that does not parse arrives with every field
    null and its text in :data:`RESCUED_DATA`, and the row still lands. There
    is no ``DROPMALFORMED`` and no ``FAILFAST`` anywhere here — the first would
    lose the record silently and the second would fail the update, and the
    acceptance criterion asks for neither.

    Args:
        candidate: The source to build options for.
        checkpoint_uri: Root for Auto Loader state; see
            :func:`schema_location`.

    Returns:
        Reader options, ready to splat into ``.options()``.
    """
    options = {
        "cloudFiles.format": candidate.fmt,
        "cloudFiles.schemaLocation": schema_location(checkpoint_uri, candidate),
        # ⚠️ `binaryFile` REJECTS every evolution mode but `none`. Its schema is
        # four fixed columns, so there is nothing for a new column to arrive in
        # — but the reader does not treat the setting as vacuous and refuses the
        # flow outright with CF_UNSUPPORTED_SCHEMA_EVOLUTION_MODE. Observed on
        # `dbw-chip-chat`, 2026-08-26 (gh-33).
        "cloudFiles.schemaEvolutionMode": (
            "addNewColumns" if candidate.is_parsed else "none"
        ),
    }
    if candidate.glob is not None:
        # ⚠️ `pathGlobFilter`, NOT `cloudFiles.pathGlobFilter`. It is a generic
        # file-source option rather than an Auto Loader one, and Auto Loader
        # validates its own namespace: the prefixed spelling is refused at
        # stream start with CF_UNKNOWN_OPTION_KEYS_ERROR naming the key
        # lower-cased, which reads like a typo in the value. Observed on
        # `dbw-chip-chat`, 2026-08-26 (gh-33).
        options["pathGlobFilter"] = candidate.glob
    if candidate.is_parsed:
        options["cloudFiles.inferColumnTypes"] = "true"
        options["cloudFiles.rescuedDataColumn"] = RESCUED_DATA
        options["multiLine"] = "true" if candidate.multiline else "false"
        if candidate.schema_hints:
            options["cloudFiles.schemaHints"] = ", ".join(candidate.schema_hints)
    return options


def quarantine_predicate(candidate: Source) -> str:
    """Return the SQL for "this row did not arrive intact".

    Two clauses, because one mechanism does not cover both failures.

    :data:`RESCUED_DATA` catches the *partial* failure: a field the schema does
    not have, or a value whose type does not match. The row is otherwise fine
    and the rescued JSON says what was wrong with it.

    The identity clause catches the *total* failure. A document that did not
    parse at all arrives as a row of nulls with nothing rescued, and the only
    thing that distinguishes it from a legitimately sparse record is that it has
    no identity: no ``order_id``, no ``requested_url``, nothing to call it by. A
    row nothing can name did not arrive.

    Args:
        candidate: A parsed source.

    Returns:
        A SQL boolean expression.

    Raises:
        ValueError: If ``candidate`` is not parsed.
    """
    if not candidate.is_parsed:
        raise ValueError(
            f"{candidate.table!r} is read as {candidate.fmt} and cannot "
            "quarantine anything; only a parsed source can"
        )
    nameless = " AND ".join(f"{column} IS NULL" for column in candidate.identity)
    return f"({RESCUED_DATA} IS NOT NULL OR ({nameless}))"


def metadata_columns(candidate: Source) -> tuple[str, ...]:
    """Return the SQL expressions appended to every row of ``candidate``.

    Returned as expression strings rather than as Column objects so that this
    module stays importable without PySpark and so the tests can read what the
    pipeline will actually do.

    ``_metadata`` is Spark's hidden per-file column. It is not part of the
    inferred schema, so it survives schema evolution and cannot collide with a
    field the landing zone already has.

    Args:
        candidate: The source.

    Returns:
        ``expr AS name`` clauses, in the order they are added.
    """
    columns = [
        f"current_timestamp() AS {INGESTED_AT}",
        f"_metadata.file_path AS {SOURCE_PATH}",
        f"_metadata.file_modification_time AS {SOURCE_MODIFIED_AT}",
        f"_metadata.file_size AS {SOURCE_SIZE_BYTES}",
    ]
    if candidate.is_parsed:
        columns.append(f"{quarantine_predicate(candidate)} AS {QUARANTINED}")
    return tuple(columns)


def quarantine_columns(candidate: Source) -> tuple[str, ...]:
    """Return the projection every stream's quarantine view is a union of.

    One shape for every table, so the union is well defined: which table the
    row was headed for, which file it came out of, when it was read, and what
    the reader could not place. The rescued JSON is the reason — it names the
    offending fields and carries the source path itself — so there is no
    second column restating it in prose.

    Args:
        candidate: A parsed source.

    Returns:
        ``expr AS name`` clauses.

    Raises:
        ValueError: If ``candidate`` is not parsed. A binary source has no
            rescued data column and belongs to no quarantine.
    """
    if not candidate.is_parsed:
        raise ValueError(
            f"{candidate.table!r} is read as {candidate.fmt} and cannot "
            "quarantine anything; only a parsed source can"
        )
    return (
        f"'{candidate.table}' AS source_table",
        SOURCE_PATH,
        INGESTED_AT,
        RESCUED_DATA,
    )
