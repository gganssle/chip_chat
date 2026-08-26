"""What silver keeps, what it throws away, and what it refuses to let through.

Issue #34 asks for both streams "cleaned, deduplicated and conformed", and the
sentence that governs the whole module is the issue's own: *bronze is what
arrived; silver is what is true.* The pipeline is
``databricks/notebooks/silver_conform.py`` and, like #33's, it is almost empty:
it loops over :data:`TABLES` and defines one materialized view per entry. Every
decision that is not "call Spark" lives here, where
``databricks/tests/test_silver.py`` can assert it without a cluster.

**This module imports nothing but the standard library, and that is
load-bearing** — the same reason it is load-bearing in
:mod:`chip_chat.databricks.bronze`. A Lakeflow pipeline runs a notebook in the
workspace, not an installed wheel, so Terraform uploads this exact file beside
the notebook and the notebook puts its directory on ``sys.path``. It therefore
has to import two ways: as ``chip_chat.databricks.silver`` under pytest, and as
a flat top-level ``silver`` on the driver. The handful of constants it shares
with ``bronze.py`` and ``catalog.py`` are spelled out again below and asserted
equal to theirs in the tests rather than imported.

The HTML reader is here for the same reason and is stdlib as well: it is
:class:`html.parser.HTMLParser`, which is what the harvest already reads
Chipotle's pages with. Boilerplate removal needs a parser, a parser on a driver
means a cluster library, and a cluster library means the uploaded-file trick
stops being enough. It does not have to.

## The three things this module does

**It deduplicates.** Two mechanisms, on two different kinds of duplicate.

A *row* duplicate is a landing-zone artefact: the harvest and the generator
rewrite their tables under new file names, Auto Loader consumes both files, and
bronze — correctly, because bronze does not transform — holds the row twice.
:func:`dedup_expression` keeps the latest arrival per published key. That is
also the only place a key is chosen, and the choice is always the *published*
one: ``item_id`` and never ``name``. Two Chipotle items really do share a name
across categories — "Chips" is sold as a side and as part of a meal — and a
dedup keyed on names would silently merge them into one and produce a menu that
is missing an item nobody deleted.

A *fact* duplicate is the corpus one the issue names: "the same nutrition figure
published on three pages should be one fact with three citations, not three
facts". :func:`block_digest` gives every block of prose an identity taken from
its own text, and the pipeline groups on it. What is conserved is the
citations, not the rows — :data:`CITATION` is an array, and the verify job
asserts that the number of citations after deduplication equals the number of
block occurrences before it. Deduplication that lost a citation would be
deduplication that lost a fact's provenance, which RFC-001 §08 cannot afford.

**It strips boilerplate.** :func:`extract_blocks` takes the visible prose out of
an HTML document and leaves the navigation, the footer, the cookie banner and
the skip links behind. The mechanism is structural — a tag list, a role list and
a small set of class substrings, all named in this module — because a structural
rule can be read and argued with, and a similarity heuristic cannot.

The *evidence* that it worked is separate from the mechanism, and it is
:data:`DOCUMENT_FREQUENCY`: how many distinct documents a block appears in.
Boilerplate is, definitionally, the text that is on nearly every page. So if the
stripper missed the footer, the footer arrives as one block whose document
frequency is the size of the corpus, and
:data:`MAXIMUM_DOCUMENT_SHARE` turns that into an assertion the verify job runs
rather than a sample somebody eyeballs.

**It conforms.** Money arrives from bronze as the string the writer wrote —
deliberately, because casting is a transformation and bronze does not transform
— and :data:`Cast` turns it into ``DECIMAL(10,2)`` here, where the pipeline can
say what it did. Foreign keys are resolved by joining, and the join *carries a
column*: ``order_items`` comes out of silver with ``item_name`` on it, and the
expectation is that the name is not null. That is deliberate. A boolean
``_item_id_resolved`` column would be a receipt for a check and nothing else;
``item_name`` is a column the serving layer wants anyway, and its nullness is
the violation. One column, two jobs, no ceremony.

## Every expectation fails the pipeline

There is no warn level in this module and no ``expect_or_drop``. The issue asks
for expectations "enforced, failing the pipeline on violation", and #34's own
brief is blunter still: an order item that does not resolve to a catalogue row
is a hard failure, not a warning. A dropped row is a silent wrong answer later,
and a warning is a row in an event log nobody reads.

That is a real commitment rather than a slogan: it means a corrupt harvest stops
the lakehouse instead of quietly serving a menu with a hole in it. Bronze is
where a bad record is allowed to land — flagged, quarantined and still there.
Silver is where it is not allowed through. :data:`QUARANTINED` rows never enter
silver at all, which is why the expectations below are about the shape of good
data rather than about parse failures.
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Final

__all__ = [
    "BLOCK_SHA256",
    "BOILERPLATE_CLASS_HINTS",
    "BOILERPLATE_ROLES",
    "BOILERPLATE_TAGS",
    "CALORIES",
    "CITATION",
    "CONFORMED_AT",
    "CORPUS",
    "DEDUP_RANK",
    "DOCUMENT_FREQUENCY",
    "DROPPED",
    "EARN_REASON",
    "EXPIRY_REASON",
    "HTML_CONTENT_TYPE",
    "INGESTED_AT",
    "LAYER",
    "MAIN_TAG",
    "MAXIMUM_DOCUMENT_SHARE",
    "MONEY",
    "QUARANTINED",
    "REASONS",
    "REDEEM_REASON",
    "SEED_REASON",
    "SOURCE_PATH",
    "STREAMS",
    "TABLES",
    "TEXT_SHA256",
    "Block",
    "Cast",
    "Corpus",
    "Expectation",
    "Reference",
    "Table",
    "analysis_paragraphs",
    "analysis_table_rows",
    "block_digest",
    "corpus",
    "dedup_expression",
    "expectations",
    "extract_blocks",
    "latest_row",
    "normalise",
    "schema_name",
    "select_expressions",
    "table",
    "tables_for",
    "text_digest",
]

LAYER: Final = "silver"
"""The medallion layer everything in this module writes to."""

STREAMS: Final[tuple[str, ...]] = ("harvested", "synthetic")
"""The two populations, as plain strings.

``chip_chat.databricks.catalog.STREAMS`` is the definition and this is a copy,
for the reason the module docstring gives. ``test_silver.py`` asserts they
agree.
"""

# --- The bronze columns this layer reads, by name ---------------------------
#
# Copies of `bronze.INGESTED_AT` and friends, for the same reason `STREAMS` is
# a copy: silver.py may not import bronze.py. The tests assert every one of
# them equals bronze's, so a rename there fails `make ci` here.

INGESTED_AT: Final = "_ingested_at"
"""When the bronze pipeline read the row. Carried through; the winning row's."""

SOURCE_PATH: Final = "_source_path"
"""The file the row came out of. Used to break a tie and then dropped."""

QUARANTINED: Final = "_quarantined"
"""Bronze's "this row did not arrive intact" flag. Never true in silver."""

DROPPED: Final[tuple[str, ...]] = (
    SOURCE_PATH,
    "_source_modified_at",
    "_source_size_bytes",
    "_rescued_data",
    QUARANTINED,
)
"""Bronze columns that do not survive into silver.

:data:`INGESTED_AT` does survive, because "when did this row arrive" is a
question about a silver row that still has an honest answer. The other four do
not: ``_rescued_data`` and ``_quarantined`` describe a failure mode silver has
already excluded, and the two file columns describe the file rather than the
fact — which changes every re-harvest without the fact changing at all.
"""

CONFORMED_AT: Final = "_conformed_at"
"""When the silver update ran. Not when the row arrived, and not when the page
was fetched. Three clocks, three columns, on purpose."""

MONEY: Final = "DECIMAL(10,2)"
"""What a price becomes.

Bronze lands money as the string the writer wrote so that a population digest
is stable across machines. Two decimal places and ten digits: Chipotle's
catering packages are the largest numbers in the dataset and are three figures.
A ``DOUBLE`` here would reintroduce exactly the binary-float noise the harvest
went to the trouble of avoiding by parsing money out of the JSON token's own
text.
"""

CALORIES: Final = "DECIMAL(8,2)"
"""What a published calorie figure becomes. Also a string in bronze, and also
not a float: the published figures are exact, and an allergen-adjacent number
that acquires a rounding error is the kind of wrong this project cannot have.
"""


def schema_name(stream: str) -> str:
    """Return the unqualified schema for ``stream`` in the silver layer.

    Args:
        stream: One of :data:`STREAMS`.

    Returns:
        ``silver_harvested`` or ``silver_synthetic``.

    Raises:
        ValueError: If ``stream`` is not one of :data:`STREAMS`.
    """
    if stream not in STREAMS:
        raise ValueError(f"unknown stream {stream!r}; expected one of {STREAMS}")
    return f"{LAYER}_{stream}"


# --- Casts, expectations and references -------------------------------------


@dataclass(frozen=True, slots=True)
class Cast:
    """One column that changes type on the way into silver.

    Attributes:
        column: The column's name, which does not change.
        sql_type: What it becomes.
        why: One line, for the reader of the catalogue browser.
    """

    column: str
    sql_type: str
    why: str

    @property
    def expression(self) -> str:
        """The ``selectExpr`` clause that performs the cast."""
        return f"CAST({self.column} AS {self.sql_type}) AS {self.column}"


@dataclass(frozen=True, slots=True)
class Expectation:
    """One constraint every row of a silver table must satisfy.

    There is no action field. Every expectation in this module is applied with
    ``expect_all_or_fail``, for the reason the module docstring argues at
    length: the issue asks for violations to fail the pipeline, and a warning
    is a row in an event log nobody reads.

    Attributes:
        name: The expectation's name, as it appears in the event log. Lower
            snake case, and a statement of what is true rather than of what
            went wrong — the log reads ``item_id_resolves`` failed.
        constraint: A SQL boolean expression over the table's own columns.
            True means the row is fine.
        why: Why this is worth stopping a pipeline for.
    """

    name: str
    constraint: str
    why: str


@dataclass(frozen=True, slots=True)
class Reference:
    """A foreign key resolved by joining, and the column the join carries.

    A reference is how this module states referential integrity, and it does it
    by *bringing a column across* rather than by leaving a boolean behind. The
    carried column is one the serving layer wants — an item's name, an order's
    timestamp, a reward's point cost — and the expectation is that it is not
    null. See the module docstring.

    Attributes:
        column: The local foreign-key column.
        stream: Which stream the referenced table is in.
        table: The referenced silver table, unqualified.
        key: The column in that table ``column`` matches.
        carries: ``(source_column, alias)`` pairs brought across.
        optional: Whether ``column`` may be null. ``loyalty_ledger.order_id``
            is null for an opening balance, and a null foreign key is not a
            dangling one — so the expectation becomes "null, or it resolves".
        why: What breaks if this does not hold.
    """

    column: str
    stream: str
    table: str
    key: str
    carries: tuple[tuple[str, str], ...]
    why: str
    optional: bool = False

    @property
    def witness(self) -> str:
        """The alias whose nullness means the key did not resolve."""
        return self.carries[0][1]

    @property
    def expectation(self) -> Expectation:
        """The constraint this reference imposes on the joined row."""
        resolved = f"{self.witness} IS NOT NULL"
        if self.optional:
            resolved = f"{self.column} IS NULL OR {resolved}"
        return Expectation(
            name=f"{self.column}_resolves",
            constraint=f"({resolved})",
            why=self.why,
        )


@dataclass(frozen=True, slots=True)
class Table:
    """One conformed silver table.

    Attributes:
        name: The silver table name, unqualified.
        stream: ``"harvested"`` or ``"synthetic"``.
        source: The bronze table it reads. Silver reads bronze and nothing
            else — no silver table reads the landing zone, which is what keeps
            "bronze is what arrived" true of the whole layer rather than of
            most of it.
        identity: The published key. Deduplication partitions on exactly these
            columns, so this tuple is where the "do not collapse two items
            that share a name" rule is actually enforced: it holds published
            identifiers and never a display name.
        required: Columns that may not be null. Separate from ``identity``
            because a compound key may legitimately have a null component —
            an FAQ entry filed directly under a category has no subcategory —
            while ``demo_id`` may never be null on anything.
        comment: What the table holds, for the Unity Catalog comment.
        casts: Columns that change type on the way in.
        references: Foreign keys resolved by joining.
        expectations: Constraints beyond the ones derived from ``required``
            and ``references``.
    """

    name: str
    stream: str
    source: str
    identity: tuple[str, ...]
    comment: str
    required: tuple[str, ...] = ()
    casts: tuple[Cast, ...] = ()
    references: tuple[Reference, ...] = ()
    expectations: tuple[Expectation, ...] = ()

    @property
    def schema(self) -> str:
        """The unqualified schema this table lives in: ``silver_harvested``."""
        return schema_name(self.stream)


def _citation(table_name: str) -> Expectation:
    """Return the "this row can say where it came from" expectation.

    Every harvested row carries ``source_url`` and ``harvested_at`` because
    RFC-001 §08 requires a quoted figure to cite where it came from, and a
    citation reconstructed downstream is a citation invented downstream. The
    issue lists this as an expectation in its own right for corpus chunks; it
    is applied to every harvested table, because a menu item quoted without a
    source is no better than a chunk quoted without one.
    """
    return Expectation(
        name="carries_its_citation",
        constraint="source_url IS NOT NULL AND harvested_at IS NOT NULL",
        why=(
            f"RFC-001 §08 needs both fields to survive out of {table_name} and "
            "into a response payload; nothing downstream can recover them"
        ),
    )


# --- The harvested catalogue -------------------------------------------------
#
# #33 landed the corpus -- the fetched bytes -- and deliberately left the
# harvest's *parsed* output alone, because five table names collide across the
# parsers and choosing between them is conformance. This is where that choice is
# made, and it is made once, for all five: `menu_items`, `item_prices`,
# `stores`, `caveats` and `item_allergens` come from `catalog/chipotle/`.
#
# The argument is in `catalog/src/chip_chat/catalog/records.py`'s own header:
# the parsed tables are one harvest of one site, and the catalogue is "the
# consolidation three other subsystems resolve against". A parsed `menu_items`
# and a catalogue `menu_items` are not two candidates for the same fact -- one
# is an input to the other. Landing both would land the same item twice under
# two names, which is precisely the duplication #34 exists to remove.
#
# So: for every name the catalogue publishes, the catalogue wins and `parsed/`
# is not read at all. What is read from `parsed/` is only what the catalogue
# does not consolidate -- the policy prose and the published rewards, below.

_CATALOGUE_CITATION: Final = ("source_url", "harvested_at")

_MENU_ITEM_REFERENCE: Final = Reference(
    column="item_id",
    stream="harvested",
    table="menu_items",
    key="item_id",
    carries=(("name", "item_name"), ("category", "item_category")),
    why=(
        "a row that prices, modifies or describes an item the catalogue does "
        "not have is a row about food nobody can order"
    ),
)

_HARVESTED: Final[tuple[Table, ...]] = (
    Table(
        name="menu_items",
        stream="harvested",
        source="menu_items",
        identity=("item_id",),
        required=("item_id", "name", "item_type"),
        casts=(
            Cast(
                column="calories",
                sql_type=CALORIES,
                why=(
                    "landed as the string the catalogue wrote so its digest is "
                    "stable; exact here rather than a float, because a "
                    "published figure is exact"
                ),
            ),
        ),
        expectations=(
            _citation("menu_items"),
            Expectation(
                name="allergen_silence_says_which_kind",
                constraint="allergen_disclosure IN ('PUBLISHED', 'NOT_PUBLISHED')",
                why=(
                    "an allergen absent from the list means either 'not listed' "
                    "or 'nothing published about this item at all', and a row "
                    "that cannot say which has merged the two silences"
                ),
            ),
        ),
        comment=(
            "The orderable menu, one row per published item, keyed on the "
            "item_id the menu publishes and NEVER on the name — two items "
            "really do share a name across categories, and a dedup keyed on "
            "names would delete one of them. calories is cast from the string "
            "bronze landed; is_composed says whether that figure is the whole "
            "meal or one component of it. Conformed by gh-34."
        ),
    ),
    Table(
        name="item_prices",
        stream="harvested",
        source="item_prices",
        identity=("restaurant_id", "item_id"),
        required=("restaurant_id", "item_id"),
        casts=(
            Cast(
                column="unit_price",
                sql_type=MONEY,
                why="money is exact, and a price is not a measurement",
            ),
            Cast(
                column="unit_delivery_price",
                sql_type=MONEY,
                why="Chipotle publishes delivery as a separate, higher number",
            ),
        ),
        references=(_MENU_ITEM_REFERENCE,),
        expectations=(
            _citation("item_prices"),
            Expectation(
                name="delivery_is_not_cheaper_than_the_counter",
                constraint="unit_delivery_price >= unit_price",
                why=(
                    "the two prices swapped would quote the wrong one to every "
                    "delivery order and look like a discount"
                ),
            ),
        ),
        comment=(
            "What each restaurant charges for each item. Price is a column on "
            "a restaurant, not on an item — Chipotle's published prices vary "
            "by nearly twenty percent between stores — so a quoted price "
            "always has a store and a harvested_at attached. Conformed by "
            "gh-34."
        ),
    ),
    Table(
        name="modifiers",
        stream="harvested",
        source="modifiers",
        identity=("modifier_id",),
        required=("modifier_id", "item_id", "modifier_item_id", "name"),
        casts=(
            Cast(
                column="delta_calories",
                sql_type=CALORIES,
                why="the component's own published figure, exact; null is not zero",
            ),
        ),
        references=(_MENU_ITEM_REFERENCE,),
        expectations=(_citation("modifiers"),),
        comment=(
            "What may go in which slot on which item. Identity is the pair "
            "<item_id>:<modifier_item_id>, because the same ingredient on a "
            "different item is a different modifier with a different "
            "allowance. Conformed by gh-34."
        ),
    ),
    Table(
        name="stores",
        stream="harvested",
        source="stores",
        identity=("store_id",),
        required=("store_id",),
        expectations=(
            _citation("stores"),
            Expectation(
                name="publishes_a_week",
                constraint="size(hours) = 7",
                why=(
                    "a store missing a day answers 'are you open on Sunday' "
                    "with silence, which reads as closed"
                ),
            ),
        ),
        comment=(
            "The restaurants, with their addresses and their published week of "
            "hours kept nested on the row they belong to. The name comes from "
            "the restaurant API and the address from the locator, which is why "
            "the row carries two provenances. Conformed by gh-34."
        ),
    ),
    Table(
        name="item_allergens",
        stream="harvested",
        source="item_allergens",
        identity=("item_id", "allergen_code"),
        required=("item_id", "allergen_code", "status"),
        references=(
            _MENU_ITEM_REFERENCE,
            Reference(
                column="allergen_code",
                stream="harvested",
                table="allergens",
                key="allergen_code",
                carries=(("name", "allergen_name"),),
                why=(
                    "a mark against a code the published vocabulary does not "
                    "have is a mark nobody can render or explain"
                ),
            ),
        ),
        expectations=(
            _citation("item_allergens"),
            Expectation(
                name="keeps_all_three_published_states",
                constraint="status IN ('CONTAINS', 'NOT_LISTED', 'NOT_PUBLISHED')",
                why=(
                    "a boolean has room for CONTAINS and exactly one other "
                    "thing, so the two kinds of silence merge and both read as "
                    "'does not contain'"
                ),
            ),
        ),
        comment=(
            "One row per item per allergen code, carrying the published state "
            "verbatim: CONTAINS, NOT_LISTED or NOT_PUBLISHED. Nothing here "
            "converts those into a boolean and nothing downstream should. "
            "Conformed by gh-34."
        ),
    ),
    Table(
        name="allergens",
        stream="harvested",
        source="allergens",
        identity=("allergen_code",),
        required=("allergen_code",),
        expectations=(_citation("allergens"),),
        comment=(
            "The published allergen vocabulary: the codes the chart uses and "
            "the words it renders them with. Conformed by gh-34."
        ),
    ),
    Table(
        name="caveats",
        stream="harvested",
        source="caveats",
        identity=("position",),
        required=("position", "text"),
        expectations=(_citation("caveats"),),
        comment=(
            "The caveats Chipotle publishes beside the allergen chart, in the "
            "order the page publishes them — including the one that says foods "
            "contact one another during preparation and the chart does not "
            "reflect it. They travel with the data because an allergen answer "
            "given without them is a different answer. Conformed by gh-34."
        ),
    ),
    Table(
        name="vocabulary",
        stream="harvested",
        source="vocabulary",
        identity=("slot", "value"),
        required=("slot", "value", "derivation"),
        expectations=(
            _citation("vocabulary"),
            Expectation(
                name="every_term_resolves_to_an_item",
                constraint="size(item_ids) > 0",
                why=(
                    "the vision model's enums are generated from this table, "
                    "so a term with nothing behind it is a word the model can "
                    "return and the matcher cannot resolve"
                ),
            ),
        ),
        comment=(
            "The slot vocabulary RFC-001 §07's stage-4 schema is generated "
            "from, with the item_ids each term resolves to and how the term "
            "came to be in its slot. Conformed by gh-34."
        ),
    ),
    Table(
        name="catalog_manifest",
        stream="harvested",
        source="catalog_manifest",
        identity=("catalog_version",),
        required=("catalog_version", "content_version"),
        comment=(
            "One row: which harvest produced this catalogue, and the digest of "
            "every table in it. content_version is the digest with provenance "
            "stripped, which is what the synthetic population records against "
            "— two harvests sharing it compose the same orders. Conformed by "
            "gh-34."
        ),
    ),
    Table(
        name="policy_documents",
        stream="harvested",
        source="policy_documents",
        identity=("document_id",),
        required=("document_id", "kind"),
        expectations=(
            _citation("policy_documents"),
            Expectation(
                name="is_terms_or_an_overview",
                constraint="kind IN ('TERMS', 'OVERVIEW')",
                why=(
                    "retrieval should prefer the contract over the page "
                    "explaining it when asked what the rules are, and it can "
                    "only do that if the row says which it is"
                ),
            ),
        ),
        comment=(
            "The published policy pages, one row each, so their sections have "
            "something to hang off. Conformed by gh-34."
        ),
    ),
    Table(
        name="policy_sections",
        stream="harvested",
        source="policy_sections",
        identity=("document_id", "position"),
        required=("document_id", "position", "text"),
        references=(
            Reference(
                column="document_id",
                stream="harvested",
                table="policy_documents",
                key="document_id",
                carries=(("kind", "document_kind"), ("title", "document_title")),
                why=(
                    "a section of a document that is not in the corpus is a "
                    "chunk that cannot say what it is a section of"
                ),
            ),
        ),
        expectations=(_citation("policy_sections"),),
        comment=(
            "Published policy prose, split at the boundaries the page itself "
            "published rather than at a fixed window — RFC-001 §08 chunks a "
            "terms document by section, and a boundary lost in the harvest "
            "cannot be recovered here. Conformed by gh-34."
        ),
    ),
    Table(
        name="faq_categories",
        stream="harvested",
        source="faq_categories",
        identity=("category_position", "subcategory_position"),
        required=("category", "category_position", "subcategory_position"),
        expectations=(_citation("faq_categories"),),
        comment=(
            "The FAQ's published two-level table of contents, in its published "
            "order. The order is the document structure of a FAQ, so it is a "
            "table rather than something flattened away. Conformed by gh-34."
        ),
    ),
    Table(
        name="faq_entries",
        stream="harvested",
        source="faq_entries",
        identity=("category", "subcategory", "rank"),
        required=("category", "rank", "question", "answer"),
        expectations=(_citation("faq_entries"),),
        comment=(
            "One published question and its answer. `links` carries every URL "
            "the answer pointed at, because the answer text keeps the words a "
            "link was made of and a URL that lived only in an href would "
            "otherwise be lost. Conformed by gh-34."
        ),
    ),
    Table(
        name="rewards",
        stream="harvested",
        source="rewards",
        identity=("position",),
        required=("position", "name", "point_cost"),
        expectations=(
            _citation("rewards"),
            Expectation(
                name="costs_points",
                constraint="point_cost > 0",
                why=(
                    "a redemption is reconciled against this number, and a "
                    "reward that costs nothing reconciles with anything"
                ),
            ),
        ),
        comment=(
            "The published Rewards Exchange line-up and what each reward "
            "costs. Keyed on the published position rather than on the name: "
            "the loyalty ledger joins on the name, so keying on it would let a "
            "duplicated name delete a reward instead of reporting the "
            "collision. The verify job checks for that collision instead. "
            "Conformed by gh-34."
        ),
    ),
)


# --- The synthetic accounts --------------------------------------------------
#
# Conformed to the serving schema: money cast, every visitor-scoped row keyed on
# `demo_id`, and every `item_id` resolved against the real catalogue above. The
# last of those is the one #34 is blunt about -- an order item that does not
# resolve to a catalogue row is a HARD FAILURE -- and it is what keeps RFC-001
# §04's boundary from blurring in the one direction that matters. The accounts
# are invented; the food in them is not, and silver is where that stops being a
# claim in a docstring and becomes a constraint that stops a pipeline.

SEED_REASON: Final = "SIGNUP_BONUS"
"""A persona's opening balance. Points that came from no order."""

EARN_REASON: Final = "ORDER"
"""Points earned on an order, at Chipotle's published earn rate."""

REDEEM_REASON: Final = "REWARD_REDEEMED"
"""A redemption. What was redeemed is ``reward_name``, verbatim."""

EXPIRY_REASON: Final = "POINTS_EXPIRED"
"""A balance that reached the published expiry. Points that bought nothing."""

REASONS: Final[tuple[str, ...]] = (
    SEED_REASON,
    EARN_REASON,
    REDEEM_REASON,
    EXPIRY_REASON,
)
"""Every movement the ledger records, as ``data-gen`` spells them.

Copied from ``data-gen/src/chip_chat/data_gen/population.toml`` for the reason
every other constant here is copied, and asserted equal to the generator's
configuration in ``test_silver.py``. They are load-bearing rather than
decorative: the issue's "every loyalty entry references a real order or a real
reward" is only checkable once the two movements that reference *neither* are
named, and there are exactly two — an opening balance and an expiry.
"""

_ORDER_REFERENCE: Final = Reference(
    column="order_id",
    stream="synthetic",
    table="orders",
    key="order_id",
    carries=(("demo_id", "demo_id"), ("placed_at", "order_placed_at")),
    why="a line or a ledger entry on an order that does not exist bills nobody",
)

_VISITOR_REFERENCE: Final = Reference(
    column="demo_id",
    stream="synthetic",
    table="demo_visitors",
    key="demo_id",
    carries=(("persona_id", "visitor_persona_id"),),
    why=(
        "demo_id is the value Snowflake's row access policies compare against, "
        "so a row scoped to a visitor who does not exist is a row no policy "
        "can decide about"
    ),
)

_SYNTHETIC: Final[tuple[Table, ...]] = (
    Table(
        name="personas",
        stream="synthetic",
        source="personas",
        identity=("persona_id",),
        required=("persona_id", "label"),
        comment=(
            "The seven archetypes the population was composed from. An "
            "archetype, not a customer: five hundred customers share seven of "
            "these. Conformed by gh-34."
        ),
    ),
    Table(
        name="demo_visitors",
        stream="synthetic",
        source="demo_visitors",
        identity=("demo_id",),
        required=("demo_id", "persona_id", "created_at"),
        references=(
            Reference(
                column="persona_id",
                stream="synthetic",
                table="personas",
                key="persona_id",
                carries=(("label", "persona_label"),),
                why=(
                    "the opening message tells a visitor what kind of customer "
                    "they have been assigned, and reads it from here"
                ),
            ),
        ),
        comment=(
            "The five hundred synthetic customers a public visitor is assigned "
            "one of. The three editable columns — display_name, "
            "home_store_override, stated_preferences — live here and nowhere "
            "else, and no gold mart reads this table: RFC-001 §04 makes that "
            "containment the mechanism rather than a rule. Conformed by gh-34."
        ),
    ),
    Table(
        name="persona_fixtures",
        stream="synthetic",
        source="persona_fixtures",
        identity=("persona_id", "rank"),
        required=("demo_id", "persona_id", "rank"),
        casts=(
            Cast(
                column="lifetime_spend",
                sql_type=MONEY,
                why="a measurement of money is still money",
            ),
        ),
        references=(
            _VISITOR_REFERENCE,
            Reference(
                column="usual_item_id",
                stream="harvested",
                table="menu_items",
                key="item_id",
                carries=(("name", "usual_item_name"),),
                optional=True,
                why=(
                    "the fixture is shown to a visitor as 'their usual', and a "
                    "usual that is not on the menu is a demo that opens with a "
                    "lie"
                ),
            ),
        ),
        expectations=(
            Expectation(
                name="demonstrates_the_archetype_it_claims",
                constraint="persona_id = visitor_persona_id",
                why=(
                    "a fixture is shown as an example OF an archetype, so one "
                    "whose customer belongs to a different archetype is an "
                    "example of nothing"
                ),
            ),
        ),
        comment=(
            "The particular customers chosen to demonstrate each archetype, "
            "with the measurements the choice was made on. usual_item_id is "
            "null for a customer with no repeated order, which is a fact about "
            "them rather than a gap. Conformed by gh-34."
        ),
    ),
    Table(
        name="orders",
        stream="synthetic",
        source="orders",
        identity=("order_id",),
        required=("order_id", "demo_id", "placed_at", "store_id"),
        casts=(
            Cast(
                column="total",
                sql_type=MONEY,
                why=(
                    "landed as the string the generator wrote so the "
                    "population digest is stable across machines; cast here, "
                    "where the pipeline can say what it did"
                ),
            ),
        ),
        references=(_VISITOR_REFERENCE,),
        expectations=(
            Expectation(
                name="was_priced_at_a_published_channel",
                constraint="channel IN ('IN_STORE', 'DELIVERY')",
                why=(
                    "the catalogue publishes two prices per item and the total "
                    "is unexplainable until the row says which was used"
                ),
            ),
            Expectation(
                name="totals_a_positive_amount",
                constraint="total > 0",
                why="an order that cost nothing was not an order",
            ),
        ),
        comment=(
            "Eighteen months of orders, total cast to money. channel says "
            "which published price list priced it and priced_restaurant_id "
            "says whose, because Chipotle publishes prices per restaurant and "
            "the population orders from thirty stores. Conformed by gh-34."
        ),
    ),
    Table(
        name="order_items",
        stream="synthetic",
        source="order_items",
        identity=("order_id", "line_number"),
        required=("order_id", "line_number", "item_id", "demo_id"),
        casts=(
            Cast(column="unit_price", sql_type=MONEY, why="money is exact"),
            Cast(column="line_total", sql_type=MONEY, why="money is exact"),
        ),
        references=(_ORDER_REFERENCE, _MENU_ITEM_REFERENCE),
        expectations=(
            Expectation(
                name="line_total_is_the_unit_price_times_the_quantity",
                constraint="line_total = unit_price * qty",
                why=(
                    "with the arithmetic checked, orders.total is a number a "
                    "reviewer can re-derive; without it, 'prices computed from "
                    "the catalogue rather than invented' is a claim"
                ),
            ),
            Expectation(
                name="orders_at_least_one_of_something",
                constraint="qty > 0",
                why="a line for nothing is a line that should not exist",
            ),
        ),
        comment=(
            "Order lines, keyed on (order_id, line_number) so one order can "
            "hold two of the same item built differently. item_name is carried "
            "from the real catalogue by the join that PROVES the item exists — "
            "this table cannot be built at all if a line references food "
            "Chipotle does not sell. demo_id is carried from the order, which "
            "is what makes a line visitor-scoped. Conformed by gh-34."
        ),
    ),
    Table(
        name="loyalty_ledger",
        stream="synthetic",
        source="loyalty_ledger",
        identity=("entry_id",),
        required=("entry_id", "demo_id", "delta", "reason", "created_at"),
        references=(
            _VISITOR_REFERENCE,
            Reference(
                column="order_id",
                stream="synthetic",
                table="orders",
                key="order_id",
                carries=(("placed_at", "order_placed_at"),),
                optional=True,
                why=(
                    "issue #27 reconciles earned points against the orders that "
                    "earned them, and that should be a join rather than a "
                    "regeneration"
                ),
            ),
            Reference(
                column="reward_name",
                stream="harvested",
                table="rewards",
                key="name",
                carries=(("point_cost", "reward_point_cost"),),
                optional=True,
                why=(
                    "issue #27 requires every redemption to trace to a real "
                    "published reward, and a redemption that records only what "
                    "it cost traces to nothing — two rewards may be priced the "
                    "same, and a cost is not an identity"
                ),
            ),
        ),
        expectations=(
            Expectation(
                name="records_a_published_kind_of_movement",
                constraint=(
                    "reason IN ("
                    f"'{SEED_REASON}', '{EARN_REASON}', "
                    f"'{REDEEM_REASON}', '{EXPIRY_REASON}')"
                ),
                why=(
                    "the reason column is what says whether a null order_id is "
                    "an opening balance or a dangling key, so an unknown reason "
                    "makes the next expectation undecidable"
                ),
            ),
            Expectation(
                name="references_a_real_order_or_a_real_reward",
                constraint=(
                    f"(reason = '{EARN_REASON}' AND order_id IS NOT NULL "
                    "AND reward_name IS NULL) OR "
                    f"(reason = '{REDEEM_REASON}' AND reward_name IS NOT NULL "
                    "AND order_id IS NULL) OR "
                    f"(reason IN ('{SEED_REASON}', '{EXPIRY_REASON}') "
                    "AND order_id IS NULL AND reward_name IS NULL)"
                ),
                why=(
                    "the issue's expectation, made decidable. Points are "
                    "earned on an order or spent on a reward; the only two "
                    "movements that reference neither are an opening balance "
                    "and an expiry, and they are named rather than left as a "
                    "hole the check falls through"
                ),
            ),
            Expectation(
                name="moves_the_balance_the_right_way",
                constraint=(
                    f"(reason IN ('{SEED_REASON}', '{EARN_REASON}') AND delta > 0)"
                    f" OR (reason IN ('{REDEEM_REASON}', '{EXPIRY_REASON}') "
                    "AND delta < 0)"
                ),
                why=(
                    "a redemption that added points is a ledger that does not "
                    "balance, and a balance is what the account lane quotes"
                ),
            ),
        ),
        comment=(
            "Signed point movements. order_placed_at and reward_point_cost are "
            "carried from the real order and the real published reward by the "
            "joins that prove each exists; both are null for an opening "
            "balance or an expiry, which are the only two movements that "
            "reference neither. Conformed by gh-34."
        ),
    ),
    Table(
        name="population_manifest",
        stream="synthetic",
        source="population_manifest",
        identity=("population_version",),
        required=("population_version", "seed", "catalog_content_version"),
        comment=(
            "One row: which seed, which catalogue and which published rewards "
            "terms produced this population, and the digest of every table in "
            "it. The provenance record a mart that looks wrong is traced back "
            "through. Conformed by gh-34."
        ),
    ),
)

TABLES: Final[tuple[Table, ...]] = _HARVESTED + _SYNTHETIC
"""Every conformed silver table, harvested stream first.

The order matters to a reader and not to the pipeline: Lakeflow resolves the
dependency graph from the reads themselves, so ``order_items`` finds
``menu_items`` whether or not it is declared after it.
"""


def tables_for(stream: str) -> Iterator[Table]:
    """Yield every table belonging to ``stream``, in declaration order.

    Args:
        stream: One of :data:`STREAMS`.

    Yields:
        The matching tables.

    Raises:
        ValueError: If ``stream`` is unknown.
    """
    schema_name(stream)
    for candidate in TABLES:
        if candidate.stream == stream:
            yield candidate


def table(name: str) -> Table:
    """Return the conformed table called ``name``.

    Args:
        name: An unqualified silver table name.

    Returns:
        The table.

    Raises:
        KeyError: If no silver table has that name.
    """
    for candidate in TABLES:
        if candidate.name == name:
            return candidate
    raise KeyError(f"no silver table is called {name!r}")


# --- What the pipeline does with a declaration -------------------------------

DEDUP_RANK: Final = "_dedup_rank"
"""Where the deduplication window puts its answer, before it is dropped."""


def latest_row(*identity: str) -> str:
    """Return the window that picks one row per ``identity``, latest first.

    The lower half of :func:`dedup_expression`, exposed on its own because the
    corpus deduplicates two bronze tables that are not conformed by declaration
    — the fetch-once cache's pointers and its bodies — and they must be
    deduplicated the same way as everything else rather than a second way that
    happens to agree today.

    Args:
        identity: The columns that name a row. At least one.

    Returns:
        A SQL ``ROW_NUMBER() OVER (...)`` expression. The pipeline keeps the
        rows where it equals one.

    Raises:
        ValueError: If no identity is given. Partitioning by nothing would rank
            the entire table and keep exactly one row of it, which is a
            catastrophe that looks like a successful update.
    """
    if not identity:
        raise ValueError("a deduplication window needs at least one identity column")
    return (
        f"ROW_NUMBER() OVER (PARTITION BY {', '.join(identity)} "
        f"ORDER BY {INGESTED_AT} DESC, {SOURCE_PATH} DESC)"
    )


def dedup_expression(candidate: Table) -> str:
    """Return the window that picks one row per published key.

    The duplicate this removes is a landing-zone artefact rather than a
    modelling error. The harvest and the generator rewrite their tables under
    new file names; Auto Loader consumes both files because they are both new
    files; and bronze holds the row twice, correctly, because bronze does not
    transform. The latest arrival wins, and ``_source_path`` breaks the tie
    when two files landed in the same update — a deterministic order matters
    more than which of two identical rows is chosen.

    **The partition is the published key and never the display name.** That is
    the whole of #34's warning about deduplication: two Chipotle items share a
    name across categories, and partitioning on ``name`` would silently keep
    one of them and delete a menu item nobody removed.
    ``test_silver.py`` asserts that no identity in this module contains a
    ``name`` column.

    Args:
        candidate: The table.

    Returns:
        A SQL ``ROW_NUMBER() OVER (...)`` expression. The pipeline keeps the
        rows where it equals one.
    """
    return latest_row(*candidate.identity)


def select_expressions(candidate: Table) -> tuple[str, ...]:
    """Return the projection that turns a deduplicated bronze row into silver.

    ``* EXCEPT (...)`` rather than a column list, because the column list is
    the landing zone's and this module refuses to restate it: a field added to
    a harvested table should arrive in silver without a code change, exactly as
    it arrives in bronze under ``addNewColumns``. What is named here is only
    what changes — the bronze columns that do not survive, and the columns that
    change type.

    A cast column moves to the end of the row, because it is excluded from the
    star and re-added. Column order is not a promise this layer makes.

    Args:
        candidate: The table.

    Returns:
        ``selectExpr`` clauses, in order.
    """
    excluded = (*DROPPED, *(cast.column for cast in candidate.casts))
    return (
        f"* EXCEPT ({', '.join(excluded)})",
        *(cast.expression for cast in candidate.casts),
        f"current_timestamp() AS {CONFORMED_AT}",
    )


def expectations(candidate: Table) -> tuple[Expectation, ...]:
    """Return every constraint applied to ``candidate``, derived ones included.

    Three sources, in this order: one per required column, one per reference,
    and then whatever the table declared for itself. All of them are applied
    with ``expect_all_or_fail`` — see the module docstring for why there is no
    other kind.

    Args:
        candidate: The table.

    Returns:
        The expectations, with unique names.

    Raises:
        ValueError: If two expectations end up sharing a name, which would
            leave one of them silently unreported in the event log.
    """
    derived = [
        Expectation(
            name=f"{column}_is_present",
            constraint=f"{column} IS NOT NULL",
            why=(
                f"{column} identifies or scopes the row, and a null there is a "
                "row nothing downstream can key on"
            ),
        )
        for column in candidate.required
    ]
    derived += [reference.expectation for reference in candidate.references]
    derived += list(candidate.expectations)
    names = [item.name for item in derived]
    if len(names) != len(set(names)):
        raise ValueError(
            f"{candidate.name} declares two expectations with one name: "
            f"{sorted(name for name in names if names.count(name) > 1)}"
        )
    return tuple(derived)


# --- The corpus: what boilerplate is, and how it is removed ------------------
#
# The harvested corpus arrives in bronze as bytes -- `raw_bodies` -- with a
# pointer beside it carrying the two columns RFC-001 §08 needs to cite it. What
# comes out here is prose: the visible text of each document, split at the
# boundaries the document itself published, with the furniture removed.
#
# The rule is structural on purpose. A frequency heuristic would strip whatever
# happened to repeat, which on a menu site includes the allergen caveat that
# must never be stripped; a machine-learned readability model would be a cluster
# library and an unarguable decision. A tag list can be read, argued with, and
# extended by somebody who has looked at the page.

HTML_CONTENT_TYPE: Final = "text/html"
"""The only content type this module extracts prose from.

The harvest also caches JSON endpoints -- the menu and locator APIs -- and
PDFs. Neither is prose. The JSON is already parsed into the catalogue above,
where it is a table rather than a wall of text, and the PDFs are read by
Document Intelligence, whose extraction :func:`analysis_table_rows` keeps as
rows. Running an HTML stripper over either would produce a chunk that is a
worse copy of a table this lakehouse already has.
"""

MAIN_TAG: Final = "main"
"""The element that means "the rest of this page is furniture".

Where a document has one, nothing outside it is read at all. That is a much
blunter instrument than the tag list below and a much more reliable one, and it
is the same call ``chip_chat.harvest.sources.chipotle.sections`` makes.
"""

BOILERPLATE_TAGS: Final[frozenset[str]] = frozenset(
    {
        "head",
        "title",
        "nav",
        "header",
        "footer",
        "aside",
        "script",
        "style",
        "noscript",
        "form",
        "button",
        "select",
        "option",
        "textarea",
        "svg",
        "iframe",
        "dialog",
        "template",
        "label",
    }
)
"""Elements whose entire subtree is furniture, skipped wherever they appear.

Every one of them has a closing tag. That is a requirement rather than a
coincidence: the skip is bounded by nesting depth, so a *void* element here
would open a subtree that never closes and silence the rest of the document.
:data:`_VOID` is handled before this list is consulted, for that reason.

``head`` is here because a page's ``<title>`` is not prose. It is the same
sentence as the ``<h1>`` on most pages and a truncated version of it on the
rest, so keeping it would deduplicate as a second fact saying nearly the same
thing — which is worse than either keeping it once or dropping it.

``header`` is on this list and it is the one worth defending: inside an
``<article>`` a ``<header>`` is the article's own title, and dropping it loses a
heading. On the pages this corpus is made of it is the site chrome every time,
and the headings that matter are ``<h1>`` to ``<h6>``, which are collected
whether or not they sit in one. Losing a duplicate title is cheaper than
keeping the global navigation on all two hundred documents.
"""

BOILERPLATE_ROLES: Final[frozenset[str]] = frozenset(
    {"navigation", "banner", "contentinfo", "search", "dialog", "alertdialog"}
)
"""ARIA landmark roles that say "furniture" in so many words.

These are the accessibility tree's own names for the site header, the site
footer, the navigation and a modal. A page that labels them is a page that has
told us what to skip, and taking it at its word costs nothing.
"""

BOILERPLATE_CLASS_HINTS: Final[tuple[str, ...]] = (
    "cookie",
    "consent",
    "onetrust",
    "gdpr",
    "privacy-banner",
    "skip-link",
    "skip-to",
    "breadcrumb",
    "sr-only",
    "visually-hidden",
    "screen-reader",
    "newsletter",
    "social-share",
    "back-to-top",
)
"""Substrings that condemn an element by its ``class`` or ``id``.

Deliberately short, deliberately specific, and deliberately not tuned to one
site's markup beyond the consent vendor everybody uses. A hint here is a
promise that no *content* element on any page in this corpus carries the
substring, which is why ``banner`` alone is not on the list and
``privacy-banner`` is: the bare word appears on hero images that hold real
copy.

The screen-reader classes are here for a subtler reason than the rest. Their
text is real English, invisible to a reader, and repeated on every page — "skip
to main content", "opens in a new window". It is the single best way to poison
a chunk embedding with words no visitor ever saw.
"""

MAXIMUM_DOCUMENT_SHARE: Final = 0.5
"""How much of the corpus one block of prose may appear in.

This is not part of the stripper; it is the check on it. Boilerplate is, by
definition, the text that is on nearly every page, so a block that survived
extraction and still appears in more than half the documents is furniture the
tag list missed. The verify job asserts against it, which is what makes #34's
third acceptance criterion something that runs rather than something somebody
eyeballs.

Half is generous on purpose. A genuinely shared fact — the allergen caveat, the
same nutrition figure on three pages — is exactly what deduplication is
*supposed* to collapse into one row with several citations, and the threshold
must not turn that success into a failure. Furniture does not appear on half a
site; it appears on all of it.
"""

_HEADINGS: Final[frozenset[str]] = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_UNITS: Final[frozenset[str]] = (
    frozenset({"p", "li", "dt", "dd", "td", "th", "figcaption", "blockquote", "pre"})
    | _HEADINGS
)
_VOID: Final[frozenset[str]] = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
"""Elements HTML closes for you.

:class:`html.parser.HTMLParser` reports a bare ``<img>`` as a start tag and
never as an end tag, so counting it against the nesting depth would leave the
count one too high for the rest of the document — and every depth comparison
after it, including the one that ends a skipped subtree, would be wrong. Real
pages are full of them. This is checked by
``test_a_void_element_does_not_swallow_the_rest_of_the_page``.
"""

_VOID_BREAKS: Final[frozenset[str]] = frozenset({"br", "hr"})
_WHITESPACE: Final = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Block:
    """One block of published prose: a heading and the text under it.

    This is the unit the corpus deduplicates on and the unit #35 chunks. It is
    a *published* boundary rather than a chosen one — a heading the document
    itself wrote — for the reason RFC-001 §08 gives about fixed windows.

    Attributes:
        position: Where the block falls in its document, from zero. Kept so a
            deduplicated block can still say where it sat in each of the
            documents that published it.
        heading: The heading that opened it, or ``None`` for the prose before
            the first one.
        text: The visible text under that heading, paragraph breaks kept as
            newlines and nothing else altered.
    """

    position: int
    heading: str | None
    text: str


def _attribute(attrs: list[tuple[str, str | None]], name: str) -> str:
    """Return one attribute's value, lower-cased, or the empty string."""
    for key, value in attrs:
        if key == name and value:
            return value.lower()
    return ""


def _is_furniture(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    """Return whether this element and everything inside it should be skipped.

    Four tests, cheapest first: the tag itself, the ARIA landmark role, the
    ``aria-hidden`` and ``hidden`` attributes, and finally a substring of the
    ``class`` or ``id``. See :data:`BOILERPLATE_TAGS`,
    :data:`BOILERPLATE_ROLES` and :data:`BOILERPLATE_CLASS_HINTS` for what each
    list costs and why it is as short as it is.

    Args:
        tag: The lower-cased tag name.
        attrs: The attributes as :class:`html.parser.HTMLParser` reports them.

    Returns:
        Whether to skip the subtree.
    """
    if tag in BOILERPLATE_TAGS:
        return True
    if _attribute(attrs, "role") in BOILERPLATE_ROLES:
        return True
    if _attribute(attrs, "aria-hidden") == "true":
        return True
    if any(key == "hidden" for key, _ in attrs):
        return True
    marks = f"{_attribute(attrs, 'class')} {_attribute(attrs, 'id')}"
    return any(hint in marks for hint in BOILERPLATE_CLASS_HINTS)


class _BlockCollector(HTMLParser):
    """Reads a page's visible prose into heading-delimited blocks.

    Three pieces of state carry the work. ``_skip_depth`` is the depth at which
    a furniture subtree opened, and is ``None`` when nothing is being skipped —
    one integer rather than a stack, because a furniture element nested inside
    another one changes nothing. ``_unit`` holds the paragraph being read.
    ``_body`` holds the paragraphs collected since the last heading.

    ``_main_depth`` records whether the text now being read is inside a
    ``<main>``. Which of the two collections to keep can only be decided once
    the whole document has been read — a page opens its navigation long before
    its ``<main>`` — so both are collected and :meth:`blocks` chooses at the
    end.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._collected: list[tuple[bool, str | None, str]] = []
        self._depth = 0
        self._skip_depth: int | None = None
        self._main_depth: int | None = None
        self._unit: list[str] = []
        self._body: list[str] = []
        self._heading: str | None = None

    # -- structure ----------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            if tag in _VOID_BREAKS and self._skip_depth is None:
                self._end_unit()
            return
        if self._skip_depth is None and _is_furniture(tag, attrs):
            self._skip_depth = self._depth
            self._depth += 1
            return
        if self._skip_depth is None:
            if tag == MAIN_TAG and self._main_depth is None:
                self._flush()
                self._main_depth = self._depth
            if tag in _HEADINGS:
                self._flush()
            elif tag in _UNITS:
                self._end_unit()
        self._depth += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID_BREAKS and self._skip_depth is None:
            self._end_unit()

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        self._depth = max(self._depth - 1, 0)
        if self._skip_depth is not None:
            if self._depth <= self._skip_depth:
                self._skip_depth = None
            return
        if tag in _HEADINGS:
            self._heading = self._end_unit(as_heading=True) or None
        elif tag in _UNITS:
            self._end_unit()
        if self._main_depth is not None and tag == MAIN_TAG:
            self._flush()
            self._main_depth = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth is None:
            self._unit.append(data)

    # -- buffers ------------------------------------------------------------

    def _end_unit(self, *, as_heading: bool = False) -> str:
        """Close the paragraph being read and return its text.

        A heading's text is returned and *not* added to the body: it opens the
        next block rather than being the first line of it, and a heading that
        appeared in both would deduplicate as two facts saying the same thing.
        """
        text = _WHITESPACE.sub(" ", "".join(self._unit)).strip()
        self._unit = []
        if text and not as_heading:
            self._body.append(text)
        return text

    def _flush(self) -> None:
        """Close the current block, if there is anything in it."""
        self._end_unit()
        body = "\n".join(self._body).strip()
        self._body = []
        heading, self._heading = self._heading, None
        if heading or body:
            self._collected.append((self._main_depth is not None, heading, body))

    def blocks(self) -> tuple[Block, ...]:
        """Return the document's blocks, in published order.

        Where the page has a ``<main>`` with anything in it, only what was
        inside it is returned. See :data:`MAIN_TAG`.
        """
        self._flush()
        inside = [entry for entry in self._collected if entry[0]]
        chosen = inside or self._collected
        return tuple(
            Block(position=index, heading=heading, text=text)
            for index, (_, heading, text) in enumerate(chosen)
        )


def extract_blocks(document: str) -> tuple[Block, ...]:
    """Return the visible prose of an HTML document, furniture removed.

    Args:
        document: The document's text. Bronze holds it as bytes, and the
            pipeline decodes before calling this — an undecodable body is a
            fact about the document rather than something to guess at.

    Returns:
        The blocks, in the order the page published them. A page whose entire
        content was furniture returns nothing, which is a legitimate answer:
        the harvest caches redirect stubs and consent interstitials, and a
        corpus that keeps them keeps noise with citations attached.
    """
    collector = _BlockCollector()
    collector.feed(document)
    collector.close()
    return collector.blocks()


# --- Identity: what makes two facts the same fact ----------------------------


def normalise(text: str) -> str:
    """Return ``text`` in the form two copies of one fact both reduce to.

    Unicode is normalised to NFKC, every run of whitespace — the non-breaking
    spaces a CMS sprinkles through published copy included — becomes one space,
    and the result is stripped.

    Case is deliberately **not** folded. A heading set in capitals on one page
    and in sentence case on another is arguably the same fact, but folding case
    is the kind of normalisation that eventually merges two things that differ
    only in a proper noun. The whitespace rules are safe because whitespace
    carries no meaning here; case sometimes does.

    Args:
        text: Any published text.

    Returns:
        Its normal form.
    """
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def block_digest(heading: str | None, text: str) -> str:
    """Return the identity of one block of prose.

    This is what "the same nutrition figure published on three pages is one
    fact with three citations" means operationally: three blocks whose
    normalised heading and text agree are one row, and the three pages become
    three entries in that row's citations.

    The heading is part of the identity rather than metadata beside it. The
    same sentence under "Allergens" and under "Nutrition" is being said about
    two different things, and merging the two would produce a fact whose
    citations point at pages that were not making the same claim.

    Args:
        heading: The block's heading, or ``None``.
        text: The block's text.

    Returns:
        A hex SHA-256 digest.
    """
    running = hashlib.sha256()
    running.update(normalise(heading or "").encode("utf-8"))
    running.update(b"\x00")
    running.update(normalise(text).encode("utf-8"))
    return running.hexdigest()


def text_digest(blocks: tuple[Block, ...]) -> str:
    """Return the identity of a whole document, as its blocks make it.

    Taken over the *extracted* text rather than over the bytes, which is the
    point: two responses that differ only in a cache-busting query parameter, a
    build hash in a script tag or an A/B test's class name are byte-different
    and fact-identical. Bronze content-addresses the bytes and so keeps both;
    silver addresses the prose and keeps one, with both URLs cited.

    Args:
        blocks: The document's blocks, in order.

    Returns:
        A hex SHA-256 digest. A document with no blocks left after extraction
        digests to the digest of nothing, which is stable and shared by every
        other empty document — they are, for this layer's purposes, the same
        absence of a document.
    """
    running = hashlib.sha256()
    for block in blocks:
        running.update(block_digest(block.heading, block.text).encode("ascii"))
        running.update(b"\n")
    return running.hexdigest()


# --- The PDFs: a table row is present whole or it is not present -------------
#
# The allergen and nutrition PDFs are read by Document Intelligence at harvest
# time and land in bronze as the service's own `analyzeResult` JSON, verbatim.
# Silver reads two things out of it and refuses to read a third.
#
# It reads the paragraphs, which are prose and join the corpus like any other
# block. It reads the tables as ROWS, each carrying the column headings that
# give its numbers meaning. It does not flatten a table into text, and it does
# not emit a cell on its own -- RFC-001 §08 is explicit that a nutrition table
# split across a boundary produces exactly the confident wrong answers allergen
# questions cannot tolerate, and a lone cell is that boundary at its worst.

COLUMN_HEADER: Final = "columnHeader"
"""The ``kind`` Document Intelligence gives a cell it read as a column heading.

The service omits ``kind`` entirely for an ordinary cell, which is why the
default below is a content cell rather than an error.
"""


def analysis_paragraphs(result: str) -> tuple[str, ...]:
    """Return the prose paragraphs of one Document Intelligence result.

    Args:
        result: The ``analyzeResult`` object as JSON text, exactly as bronze
            landed it.

    Returns:
        Each paragraph's content in reading order, empty ones dropped. The
        prose around a table — the footnote saying the chart does not reflect
        cross-contact — matters as much as the table, which is why this is
        collected rather than skipped as PDF furniture.

    Raises:
        ValueError: If ``result`` is not a JSON object. A row that reached
            silver has already passed bronze's quarantine, so this is a
            contract violation rather than a bad record, and silence about it
            would be a corpus quietly missing its PDFs.
    """
    payload = json.loads(result)
    if not isinstance(payload, dict):
        raise ValueError(f"an analyzeResult must be an object, got {type(payload)}")
    paragraphs = payload.get("paragraphs") or ()
    return tuple(
        text
        for raw in paragraphs
        if isinstance(raw, dict) and (text := str(raw.get("content") or "").strip())
    )


def _headers(raw: dict[str, Any], column_count: int) -> tuple[str | None, ...]:
    """Return one heading per column, or ``None`` where the table has none.

    A heading merged across columns names every column it covers, because a
    figure under the right-hand half of a merged "Total Fat" heading is still a
    total fat figure.
    """
    headings: list[str | None] = [None] * column_count
    for cell in raw.get("cells") or ():
        if not isinstance(cell, dict) or cell.get("kind") != COLUMN_HEADER:
            continue
        column = int(cell.get("columnIndex", 0))
        for offset in range(int(cell.get("columnSpan") or 1)):
            position = column + offset
            if 0 <= position < column_count and headings[position] is None:
                headings[position] = str(cell.get("content") or "").strip()
    return tuple(headings)


def analysis_table_rows(result: str) -> tuple[dict[str, Any], ...]:
    """Return every extracted table row, whole, with its column headings.

    A row is the smallest unit anything downstream is allowed to take. It is
    either here with the headings that give its numbers meaning, or it is not
    here.

    Header rows are not emitted as rows of their own: they are carried on every
    row of their table instead, which is where they are useful.

    Args:
        result: The ``analyzeResult`` object as JSON text.

    Returns:
        One mapping per row, with ``table_index``, ``row_index``, ``caption``,
        ``page_number``, ``column_headers`` and ``cells``. ``cells`` has one
        entry per column, ``None`` where no cell covered that position — a hole
        the service reported rather than an empty string invented here.

    Raises:
        ValueError: If ``result`` is not a JSON object, or a cell omits its row
            or column index. A number with no column to belong to is not a case
            to paper over with a default.
    """
    payload = json.loads(result)
    if not isinstance(payload, dict):
        raise ValueError(f"an analyzeResult must be an object, got {type(payload)}")
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(payload.get("tables") or ()):
        if not isinstance(raw, dict):
            continue
        row_count = int(raw.get("rowCount") or 0)
        column_count = int(raw.get("columnCount") or 0)
        caption = raw.get("caption")
        grid: list[list[str | None]] = [[None] * column_count for _ in range(row_count)]
        header_rows: set[int] = set()
        for cell in raw.get("cells") or ():
            if not isinstance(cell, dict):
                continue
            if "rowIndex" not in cell or "columnIndex" not in cell:
                raise ValueError(
                    f"table {index} has a cell with no position: {sorted(cell)}"
                )
            row_index = int(cell["rowIndex"])
            column_index = int(cell["columnIndex"])
            content = str(cell.get("content") or "").strip()
            if cell.get("kind") == COLUMN_HEADER:
                header_rows.add(row_index)
            for row_offset in range(int(cell.get("rowSpan") or 1)):
                for column_offset in range(int(cell.get("columnSpan") or 1)):
                    row = row_index + row_offset
                    column = column_index + column_offset
                    if 0 <= row < row_count and 0 <= column < column_count:
                        grid[row][column] = content
        headings = _headers(raw, column_count)
        for row_index, cells in enumerate(grid):
            if row_index in header_rows or not any(cells):
                continue
            rows.append(
                {
                    "table_index": index,
                    "row_index": row_index,
                    "caption": (
                        str(caption.get("content") or "").strip()
                        if isinstance(caption, dict)
                        else None
                    ),
                    "page_number": _page_number(raw.get("boundingRegions")),
                    "column_headers": list(headings),
                    "cells": cells,
                }
            )
    return tuple(rows)


def _page_number(regions: Any) -> int | None:
    """Return the page of the first bounding region, if the service gave one."""
    if not regions or not isinstance(regions, list):
        return None
    first = regions[0]
    if not isinstance(first, dict):
        return None
    page = first.get("pageNumber")
    return int(page) if isinstance(page, int) else None


# --- The corpus tables -------------------------------------------------------

TEXT_SHA256: Final = "text_sha256"
"""A document's identity in silver: the digest of its extracted prose.

Not the digest of its bytes. Bronze already content-addresses the bytes, and
that is the right identity for a landing zone and the wrong one for a corpus —
two responses differing only in a build hash are two files and one document.
"""

BLOCK_SHA256: Final = "block_sha256"
"""A fact's identity: the digest of one block's heading and text."""

CITATION: Final = "citations"
"""The array that makes deduplication safe.

Every row the corpus collapses adds an entry here rather than disappearing. A
fact published on three pages is one row with three citations, and the verify
job asserts that the citation count after deduplication equals the occurrence
count before it — which is the difference between removing a duplicate and
losing a source.
"""

DOCUMENT_FREQUENCY: Final = "document_frequency"
"""How many distinct documents a block appears in. See
:data:`MAXIMUM_DOCUMENT_SHARE`: this column is the evidence that boilerplate
removal worked, and it is checked rather than admired."""


@dataclass(frozen=True, slots=True)
class Corpus:
    """One silver corpus table.

    Separate from :class:`Table` because these three are not a cast-and-dedup
    of a bronze table — they are built by extracting prose from bytes, and the
    pipeline defines each one explicitly rather than in a loop.

    Attributes:
        name: The silver table name, unqualified.
        comment: What it holds, for the Unity Catalog comment.
        expectations: Constraints, all of them fatal.
    """

    name: str
    comment: str
    expectations: tuple[Expectation, ...]

    @property
    def stream(self) -> str:
        """Always ``harvested``. There is no synthetic corpus."""
        return "harvested"

    @property
    def schema(self) -> str:
        """The unqualified schema: ``silver_harvested``."""
        return schema_name(self.stream)


_CORPUS_CITATION: Final = Expectation(
    name="carries_its_citation",
    constraint="source_url IS NOT NULL AND harvested_at IS NOT NULL",
    why=(
        "the issue's fourth expectation, verbatim: no corpus chunk without "
        "source_url and harvested_at. Both are captured at fetch time because "
        "there is nowhere downstream to recover them from"
    ),
)

CORPUS: Final[tuple[Corpus, ...]] = (
    Corpus(
        name="documents",
        comment=(
            "The harvested corpus, cleaned and deduplicated: one row per "
            "distinct piece of published prose, keyed on the digest of the "
            "prose rather than of the bytes. Navigation, footers, cookie "
            "banners and screen-reader-only text are gone — stripped "
            "structurally, by a tag and role list that is written down in "
            "chip_chat.databricks.silver rather than inferred. Every URL that "
            "served this text is in `citations`; source_url and harvested_at "
            "are the most recent of them, promoted so a chunk can cite itself "
            "with one field. Built by gh-34."
        ),
        expectations=(
            _CORPUS_CITATION,
            Expectation(
                name="says_something",
                constraint="character_count > 0",
                why=(
                    "a document that extracted to nothing is a redirect stub "
                    "or a consent interstitial, and a corpus that keeps it "
                    "keeps noise with a citation attached"
                ),
            ),
            Expectation(
                name="keeps_every_url_that_served_it",
                constraint=f"size({CITATION}) > 0",
                why=(
                    "deduplication conserves citations; a row that lost them "
                    "is a fact that can no longer say where it came from"
                ),
            ),
        ),
    ),
    Corpus(
        name="document_blocks",
        comment=(
            "The corpus as facts rather than as pages: one row per distinct "
            "block of prose, with every document that published it in "
            "`citations`. This is 'the same nutrition figure on three pages is "
            "one fact with three citations, not three facts'. Blocks are split "
            "at the headings the documents themselves published, never at a "
            "fixed window — RFC-001 §08. `document_frequency` is how many "
            "documents carry the block, and is the evidence that boilerplate "
            "removal worked: furniture is the text that is on every page. "
            "Built by gh-34."
        ),
        expectations=(
            _CORPUS_CITATION,
            Expectation(
                name="says_something",
                constraint="character_count > 0",
                why="an empty block is not a fact",
            ),
            Expectation(
                name="is_not_furniture_the_stripper_missed",
                constraint=(
                    f"{DOCUMENT_FREQUENCY} <= corpus_documents * {MAXIMUM_DOCUMENT_SHARE}"
                ),
                why=(
                    "boilerplate is by definition the text on nearly every "
                    "page, so a block that survived extraction and still "
                    "appears in more than half the corpus is furniture the tag "
                    "list did not know about — and it would dominate every "
                    "chunk embedding built on top of this"
                ),
            ),
        ),
    ),
    Corpus(
        name="document_tables",
        comment=(
            "Rows of the tables Document Intelligence read out of the "
            "harvested PDFs, each carrying the column headings that give its "
            "numbers meaning. A row is here whole or it is not here: nothing "
            "in this lakehouse emits a cell on its own, because a nutrition "
            "figure separated from its column is the confident wrong answer "
            "RFC-001 §08 exists to prevent. Header rows are not rows; they are "
            "on every row of their table. Built by gh-34."
        ),
        expectations=(
            _CORPUS_CITATION,
            Expectation(
                name="knows_which_column_each_figure_is_under",
                constraint="size(column_headers) = size(cells)",
                why=(
                    "one heading per column, hole or not — a row with fewer "
                    "headings than cells has figures whose column is a guess"
                ),
            ),
            Expectation(
                name="says_which_reading_it_is",
                constraint="model_id IS NOT NULL AND api_version IS NOT NULL",
                why=(
                    "the same bytes analysed a year later may not extract the "
                    "same tables, and a row that cannot say which version read "
                    "it cannot explain the difference"
                ),
            ),
        ),
    ),
)
"""The three corpus tables, coarsest first."""


def corpus(name: str) -> Corpus:
    """Return the corpus table called ``name``.

    Args:
        name: An unqualified silver table name.

    Returns:
        The corpus table.

    Raises:
        KeyError: If no corpus table has that name.
    """
    for candidate in CORPUS:
        if candidate.name == name:
            return candidate
    raise KeyError(f"no silver corpus table is called {name!r}")
