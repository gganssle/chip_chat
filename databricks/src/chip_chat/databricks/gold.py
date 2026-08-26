"""What a chunk is, and the one decision that makes allergen answers trustworthy.

Issue #35 asks for "structure-aware chunking of the corpus for retrieval", and
RFC-001 §08 is unusually prescriptive about what that means:

    Chunking follows structure, not length. One menu item is one chunk, carrying
    its nutrition and allergen fields as metadata. Policy and FAQ documents chunk
    by section. Fixed-window chunking splits nutrition tables across boundaries
    and produces exactly the confident wrong answers that allergen questions
    cannot tolerate.

This module is that paragraph, written down so it can be run.
``databricks/notebooks/gold_chunk.py`` is the pipeline and, like #33's and
#34's, it is almost empty: it maps :data:`SOURCES` over the silver tables and
lets this file decide what comes out. ``databricks/tests/test_gold.py`` runs the
renderers over the real harvest fixtures without a cluster, which is what makes
the issue's two required tests tests rather than intentions.

**This module imports nothing but the standard library**, for the third time and
for the same reason: a Lakeflow pipeline runs a notebook in the workspace, not
an installed wheel, so Terraform uploads this exact file beside the notebook.
The handful of constants it shares with ``silver.py`` and ``catalog.py`` are
spelled out again below and asserted equal to theirs in the tests.

## A chunk is a published unit, never a slice of one

There is no window size in this file. There is no overlap, no target length, no
``split_at``, and there is deliberately nowhere for one to go: every renderer
takes **one whole structural unit** and returns one chunk. A menu item is a
chunk. A policy section is a chunk. A FAQ question and its answer are one chunk.
A row of an extracted nutrition table — with the column headings that give its
numbers meaning — is a chunk.

The failure this avoids is concrete, and ``docs/decisions/pdf-tables.md`` walks
through it. A window ends after ``Cheese | 1 oz | 110 | 8``. The next begins
``| 5 | 260 | Guacamole | 4 oz | 230``. Retrieval scores the second highly for a
question about guacamole and returns it. It contains 260 — Cheese's sodium —
sitting immediately before the word Guacamole under no heading at all. Every
ingredient of a confident wrong answer is present, and nothing downstream can
detect it, because by then there is no column left to check against.

:func:`nutrition_row_chunk` cannot produce that chunk. Not because it is careful
about boundaries, but because it never sees a boundary: it is handed a row and
its headings, and a row is what silver guarantees it. The check is
``test_no_nutrition_table_is_split_across_a_chunk_boundary``, and its companion
``test_fixed_window_chunking_splits_a_nutrition_row`` runs the same assertion
over a fixed-window chunker and asserts that it fails — which is the issue's
"would fail if fixed-window chunking were substituted", executed rather than
promised.

## Nutrition and allergens are metadata, not prose

A menu item's calorie figure is :data:`CALORIES`-typed column on the chunk, and
its allergens are an array of published codes. They are also in the text,
because the text is what gets embedded and a chunk whose prose never mentions
dairy will not be retrieved for a question about dairy. But the *authoritative*
copy is the column, and the answer path reads the column.

That distinction is the whole of the issue's "as metadata, not as prose to be
re-parsed by a model". Re-parsing "230 calories" out of a sentence is a step
that can go wrong; reading a ``DECIMAL(8,2)`` is not. The prose exists to be
found; the metadata exists to be quoted.

The same rule is why :data:`ALLERGEN_DISCLOSURE` travels on every menu chunk.
Chipotle publishes three states and not two — ``CONTAINS``, ``NOT_LISTED`` and
``NOT_PUBLISHED`` — and ``docs/decisions/allergen-absence.md`` is about what
collapsing them costs. A chunk that carried only a list of allergens would have
collapsed them: an empty list would mean both "we publish marks for this item
and none of them is dairy" and "we publish nothing about this item at all", and
the second of those read as the first is a wrong allergen answer given to a
stranger on the open internet.

## Every chunk carries its citation, and its citation is an identifier

``source_url`` and ``harvested_at`` are columns on the chunk because RFC-001 §08
says citations are part of the payload rather than reconstructed afterwards, and
because there is nowhere downstream to recover them from. Silver already
promoted them out of its citation array for exactly this hand-off.

:data:`CHUNK_ID` is the other half, and it is the half RFC-001's D9 turns into a
safety property: "citations survive as identifiers, not as prose... the response
envelope carries a citations array referencing ids the ``retriever.search`` span
actually returned on that turn." A model cannot mint an id that retrieval did
not return, so citation presence is a comparison rather than a second model's
opinion.

The id is taken from **what the chunk is about**, not from what it currently
says. ``MENU_ITEM:CMG-101`` is the Chicken Bowl chunk this week and next week,
through a re-harvest that changes its calorie figure. A content-addressed id
would change with the wording, and since #48 rebuilds the index rather than
patching it, every rebuild would retire every id — including the ones a
conversation two turns old is still citing. See :func:`chunk_id`.
"""

import hashlib
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "ALLERGENS",
    "ALLERGEN_CAVEAT",
    "ALLERGEN_DISCLOSURE",
    "CALORIES",
    "CATEGORY",
    "CHARACTER_COUNT",
    "CHUNKED_AT",
    "CHUNK_COMMENT",
    "CHUNK_ID",
    "CHUNK_TABLE",
    "CITATION",
    "COLUMN_HEADERS",
    "CONTAINS",
    "DISCLOSURES",
    "DOCUMENT_BLOCK",
    "EMBEDDING_CHARACTER_BUDGET",
    "FAQ_ENTRY",
    "FIELDS",
    "HARVESTED_AT",
    "HEADING",
    "ITEM_ID",
    "KIND",
    "KINDS",
    "LAYER",
    "MENU_ITEM",
    "NOT_PUBLISHED",
    "NUTRITION_ROW",
    "POLICY_SECTION",
    "SOURCES",
    "SOURCE_URL",
    "STREAM",
    "TEXT",
    "Chunk",
    "Expectation",
    "Field",
    "Source",
    "allergen_caveat_chunk",
    "chunk_id",
    "columns_for",
    "document_block_chunk",
    "expectations",
    "facetable",
    "faq_entry_chunk",
    "field",
    "filterable",
    "menu_item_chunk",
    "normalise",
    "nutrition_row_chunk",
    "policy_section_chunk",
    "retrievable",
    "schema_name",
    "source",
]

LAYER: Final = "gold"
"""The medallion layer this module writes to.

``chip_chat.databricks.catalog.LAYERS`` is the definition and this is a copy,
for the reason the module docstring gives. ``test_gold.py`` asserts they agree.
"""

STREAM: Final = "harvested"
"""The only stream a chunk can come from.

There is no synthetic corpus and there must not be one. RFC-001 §04 keeps the
real catalogue and the invented account data apart, and a chunk is text a model
will read back to a visitor as fact. An invented order that reached the
retrieval index would be a fabricated fact with a real-looking citation on it,
which is the single worst thing this lakehouse could produce.
"""

CHUNK_TABLE: Final = "corpus_chunks"
"""The one gold table #35 produces, and the one #48 indexes."""

CHUNK_COMMENT: Final = (
    "The corpus as retrievable units, chunked at the boundaries its publishers "
    "drew and never at a length. One menu item is one chunk, carrying its "
    "calories and its allergen marks as typed columns rather than as prose to "
    "be re-parsed; a policy document chunks at its own sections; a FAQ chunks "
    "at its own questions; a row of an extracted nutrition table is one chunk "
    "with the column headings that give its numbers meaning. There is no "
    "window size anywhere in chip_chat.databricks.gold — RFC-001 §08 says a "
    "fixed window splits nutrition tables across boundaries and produces "
    "exactly the confident wrong answers allergen questions cannot tolerate. "
    "Every chunk carries source_url, harvested_at and a chunk_id stable across "
    "a rebuild, because citations are part of the payload and survive as "
    "identifiers. Built by gh-35; indexed by gh-48."
)
"""The Unity Catalog comment on the chunk table.

Long, and it earns it: the catalogue browser is where somebody meets this table
without having read the issue, and the one thing they have to leave knowing is
that the boundaries are published rather than chosen.
"""

EMBEDDING_CHARACTER_BUDGET: Final = 8_000
"""How long a chunk can get before somebody should look at it.

This is **not** a window and nothing in this module splits on it. It is a
number the verify job reports against, because a policy section that runs to
twenty thousand characters is a document whose publisher stopped using headings
— a fact about the source worth knowing, and an argument for asking the harvest
for a finer published boundary. It is never an argument for inventing one here.

The distinction is the whole issue in one constant. A length that *splits* is a
fixed window with better manners. A length that *reports* leaves the boundary
where the publisher put it and tells a person that the publisher put it
somewhere awkward.
"""


def schema_name() -> str:
    """Return the unqualified schema chunks are written to: ``gold_harvested``."""
    return f"{LAYER}_{STREAM}"


# --- The kinds: one per published structure ---------------------------------
#
# Six, because the corpus has six kinds of published unit and not because six
# was a convenient number. Each one names a boundary somebody else drew -- the
# menu's item, the terms page's section, the FAQ's question, the allergen page's
# paragraph, the article's heading, the nutrition sheet's row -- and this module
# draws none of its own.

MENU_ITEM: Final = "MENU_ITEM"
"""One orderable thing, whole, with its nutrition and allergens as columns.

The issue's first rule, and the one that is load-bearing for the product's
commonest question. "How many calories in a chicken bowl" is answered from one
chunk or it is answered from a paraphrase of several.
"""

POLICY_SECTION: Final = "POLICY_SECTION"
"""One section of a published policy document, at the heading the page wrote.

The boundary comes out of ``silver_harvested.policy_sections``, which took it
from the harvest, which took it from the page. It is never computed here — a
boundary lost upstream cannot be recovered downstream, which is why #34's own
notes say the same thing about its own layer.
"""

FAQ_ENTRY: Final = "FAQ_ENTRY"
"""One published question and its answer, together.

A FAQ's structure is its questions, so the question is the section heading and
the answer is the section. Splitting them would produce a chunk that is an
answer to a question it does not contain, which retrieval cannot match and a
reader cannot use.
"""

ALLERGEN_CAVEAT: Final = "ALLERGEN_CAVEAT"
"""One paragraph of Chipotle's own prose about the limits of its allergen chart.

These are chunks in their own right rather than a footer appended to the menu
chunks, and that is a deliberate, arguable call. Appending them would put the
cross-contact caveat inside every one of hundreds of item chunks, where it would
dominate the embeddings and be retrieved for questions that are not about
allergens at all. Keeping them as chunks means the agent has to retrieve them,
which is what PRD K3's allergen path is for and what the eval set measures.
"""

DOCUMENT_BLOCK: Final = "DOCUMENT_BLOCK"
"""One block of harvested HTML prose, split at the heading the document wrote.

Silver already did this split, in ``extract_blocks``, and already collapsed the
same block published on three pages into one row with three citations. What is
added here is the metadata and the id; the boundary is not revisited.
"""

NUTRITION_ROW: Final = "NUTRITION_ROW"
"""One row of a table Document Intelligence read out of a harvested PDF.

Whole, with its column headings beside it. This is the kind the issue's first
required test is about, and the kind that cannot exist under fixed-window
chunking — a window cuts a table by character offset, which is a boundary the
table does not have.
"""

KINDS: Final[tuple[str, ...]] = (
    MENU_ITEM,
    POLICY_SECTION,
    FAQ_ENTRY,
    ALLERGEN_CAVEAT,
    DOCUMENT_BLOCK,
    NUTRITION_ROW,
)
"""Every kind of chunk, in the order :data:`SOURCES` builds them."""

CONTAINS: Final = "CONTAINS"
"""The published allergen state that means the item is marked with it.

Copied from ``chip_chat.harvest``'s ``AllergenStatus`` for the reason every
constant here is copied, and asserted equal to it in ``test_gold.py``.
"""

NOT_PUBLISHED: Final = "NOT_PUBLISHED"
"""The disclosure state that means nothing at all is published for the item.

Distinct from an item that publishes marks none of which is dairy. See
``docs/decisions/allergen-absence.md``; the difference is the difference between
"we did not list it" and "we said nothing", and a reader owed a safety answer is
owed the second sentence rather than a guess at the first.
"""

DISCLOSURES: Final[tuple[str, ...]] = ("PUBLISHED", NOT_PUBLISHED)
"""The two states ``menu_items.allergen_disclosure`` may hold."""


# --- The metadata schema, fixed and named -----------------------------------
#
# The issue's first acceptance criterion is "chunk table produced, with the
# metadata schema FIXED and DOCUMENTED", and #48's first scope bullet needs the
# same list with retrievability, filterability and facetability attached. So the
# schema is a tuple of records rather than a `CREATE TABLE` in a notebook: the
# index builder reads it, the pipeline reads it, `docs/corpus-chunking.md`
# renders it, and there is one copy.
#
# `retrievable` is true for very nearly everything, and that is #48's own
# sentence: "these fields are retrievable and not merely filterable", because a
# citation the application cannot read back is not a citation.


@dataclass(frozen=True, slots=True)
class Field:
    """One column of the chunk table, and what an index may do with it.

    Attributes:
        name: The column name. The same string in Delta and in the search
            index — #48 builds its index schema from this tuple, and a rename
            on one side only is the kind of drift that surfaces as an empty
            filter rather than as an error.
        sql_type: The Delta type.
        why: What the column is for, one line, for the catalogue browser and
            for ``docs/corpus-chunking.md``.
        retrievable: Whether the search index returns it on a hit. Default
            true: a field the application cannot read back cannot become part
            of a citation, and RFC-001 §08 puts citations in the payload.
        filterable: Whether a query may constrain on it. The true ones are
            chosen for the questions the product must answer — fewer calories,
            vegetarian, without dairy — rather than for completeness; every
            filterable field costs index size and build time.
        facetable: Whether the index may count it. A subset of the filterable
            ones: faceting is only meaningful over a small, closed vocabulary,
            and faceting a free-text field is a way to spend money on a
            histogram of sentences.
        kinds: The chunk kinds that populate it, or empty for the fields every
            chunk has. This is what makes "null here is expected" a statement
            in the schema rather than a thing you learn from a query.
    """

    name: str
    sql_type: str
    why: str
    retrievable: bool = True
    filterable: bool = False
    facetable: bool = False
    kinds: tuple[str, ...] = ()

    @property
    def universal(self) -> bool:
        """Whether every chunk carries this field, whatever its kind."""
        return not self.kinds


CHUNK_ID: Final = "chunk_id"
"""The identifier a citation references. See :func:`chunk_id`."""

KIND: Final = "kind"
"""Which published structure this chunk is one of. One of :data:`KINDS`."""

TEXT: Final = "text"
"""What is embedded, retrieved and read back. The only free-text field."""

HEADING: Final = "heading"
"""The published heading this chunk sits under, or null where none was published.

The issue lists "section heading" among the required metadata fields, and this
is it under a name that is honest for all six kinds: a menu item's heading is
its name, a FAQ entry's is its question, a nutrition row's is its table caption.
"""

ITEM_ID: Final = "item_id"
"""Chipotle's own published item identifier, on the chunks that are about one."""

CATEGORY: Final = "category"
"""``Entree``, ``Side``, ``Drink``, ``Non Food Items``, or null.

Null means the item is never orderable on its own — the black beans inside a
burrito. That is the catalogue's own convention, carried through unchanged; see
``chip_chat.catalog.records.MenuItem``.
"""

ALLERGENS: Final = "allergens"
"""The allergen codes published as ``CONTAINS`` for this item, sorted.

Filterable and facetable, because "without dairy" is a filter and not a
paraphrase. Read :data:`ALLERGEN_DISCLOSURE` before rendering an *absence* from
this array as a sentence: an empty array under ``NOT_PUBLISHED`` says nothing
about the food, and a filter that treated it as "free of everything" would
recommend an item to somebody it could hurt.
"""

ALLERGEN_DISCLOSURE: Final = "allergen_disclosure"
"""Whether anything at all is published about this item's allergens."""

CALORIES: Final = "calories"
"""The published total-calorie figure, exact, or null where none was published.

``DECIMAL(8,2)`` and never a float, matching silver: a published figure is
exact, and an allergen-adjacent number that acquires a rounding error is the
kind of wrong this project cannot have. Null is not zero, and a comparison
should say so.
"""

COLUMN_HEADERS: Final = "column_headers"
"""The headings of the table row this chunk is, one per cell, holes included.

Non-null only on :data:`NUTRITION_ROW`. Alongside ``cells`` it is the whole
row — the thing a fixed window destroys — carried onto the chunk so that the
"row arrived whole" property is checkable at the chunk rather than only at the
table it came from.
"""

SOURCE_URL: Final = "source_url"
"""The page or endpoint this chunk's text was published on."""

HARVESTED_AT: Final = "harvested_at"
"""When that page was fetched.

Rendered beside an allergen claim without interaction, per RFC-001's citation
decision: published allergen data goes stale and the corpus is re-harvested
weekly, so how old the answer is is part of the answer.
"""

CITATION: Final = "citations"
"""Every source that published this chunk's text, not only the most recent one.

Carried through from silver, where deduplication conserved it. Null on the
kinds that come from a single row of the catalogue, which have exactly one
source and say so in :data:`SOURCE_URL`.
"""

CHARACTER_COUNT: Final = "character_count"
"""The length of :data:`TEXT`. Reported, never acted on.

See :data:`EMBEDDING_CHARACTER_BUDGET` for why a length may exist in this
module at all.
"""

CHUNKED_AT: Final = "chunked_at"
"""When the gold update ran. The fourth clock, after ingest, conform and harvest."""

FIELDS: Final[tuple[Field, ...]] = (
    Field(
        name=CHUNK_ID,
        sql_type="STRING",
        why=(
            "the identifier a response envelope cites; stable across a rebuild "
            "because it names what the chunk is about rather than what it says"
        ),
        filterable=True,
    ),
    Field(
        name=KIND,
        sql_type="STRING",
        why="which published structure this is one of; the retriever weights on it",
        filterable=True,
        facetable=True,
    ),
    Field(
        name=TEXT,
        sql_type="STRING",
        why="what integrated vectorization embeds and what a person reads back",
    ),
    Field(
        name=HEADING,
        sql_type="STRING",
        why=(
            "the published heading this chunk sits under, which is also what "
            "keyword recall matches on for item names"
        ),
    ),
    Field(
        name=ITEM_ID,
        sql_type="STRING",
        why="the item this chunk is about, for joining a retrieved chunk to the menu",
        filterable=True,
        kinds=(MENU_ITEM,),
    ),
    Field(
        name=CATEGORY,
        sql_type="STRING",
        why="Entree, Side, Drink or null for a component; a facet the UI can offer",
        filterable=True,
        facetable=True,
        kinds=(MENU_ITEM,),
    ),
    Field(
        name="item_type",
        sql_type="STRING",
        why="Burrito, Bowl, Chips; the finer published type the vessel vocabulary uses",
        filterable=True,
        facetable=True,
        kinds=(MENU_ITEM,),
    ),
    Field(
        name="primary_filling",
        sql_type="STRING",
        why=(
            "the protein an entree is built around; half of how a described meal resolves"
        ),
        filterable=True,
        facetable=True,
        kinds=(MENU_ITEM,),
    ),
    Field(
        name=ALLERGENS,
        sql_type="ARRAY<STRING>",
        why="the codes published as CONTAINS; 'without dairy' is this filter",
        filterable=True,
        facetable=True,
        kinds=(MENU_ITEM,),
    ),
    Field(
        name=ALLERGEN_DISCLOSURE,
        sql_type="STRING",
        why=(
            "whether anything is published for this item at all; without it an "
            "empty allergens array means two different things and reads as one"
        ),
        filterable=True,
        kinds=(MENU_ITEM,),
    ),
    Field(
        name=CALORIES,
        sql_type="DECIMAL(8,2)",
        why="the published figure, exact; 'fewer calories' is a filter on this",
        filterable=True,
        kinds=(MENU_ITEM,),
    ),
    Field(
        name="is_composed",
        sql_type="BOOLEAN",
        why=(
            "whether calories is the whole meal or one component of it; a "
            "comparison that ignored it would rank a burrito's tortilla "
            "against a bowl"
        ),
        filterable=True,
        kinds=(MENU_ITEM,),
    ),
    Field(
        name="document_id",
        sql_type="STRING",
        why="which policy document this section belongs to",
        filterable=True,
        kinds=(POLICY_SECTION,),
    ),
    Field(
        name="document_kind",
        sql_type="STRING",
        why=(
            "TERMS or OVERVIEW, so retrieval can prefer the contract over the "
            "page explaining it when asked what the rules are"
        ),
        filterable=True,
        facetable=True,
        kinds=(POLICY_SECTION,),
    ),
    Field(
        name="position",
        sql_type="INT",
        why="where this unit falls in its document, in the order the source published",
        kinds=(POLICY_SECTION, ALLERGEN_CAVEAT, FAQ_ENTRY),
    ),
    Field(
        name=COLUMN_HEADERS,
        sql_type="ARRAY<STRING>",
        why=(
            "one heading per cell of the row this chunk is; the structure that "
            "makes the row's numbers mean anything, kept beside the prose "
            "rather than only inside it"
        ),
        kinds=(NUTRITION_ROW,),
    ),
    Field(
        name="cells",
        sql_type="ARRAY<STRING>",
        why="the row's own values, positionally aligned with column_headers",
        kinds=(NUTRITION_ROW,),
    ),
    Field(
        name="page_number",
        sql_type="INT",
        why=(
            "which page of the PDF the row was read from, for a citation "
            "somebody can check"
        ),
        kinds=(NUTRITION_ROW,),
    ),
    Field(
        name=SOURCE_URL,
        sql_type="STRING",
        why="RFC-001 §08: citations are part of the payload, not reconstructed after",
        filterable=True,
    ),
    Field(
        name=HARVESTED_AT,
        sql_type="TIMESTAMP",
        why=(
            "how old the answer is, which for an allergen claim renders without "
            "interaction because published allergen data goes stale"
        ),
        filterable=True,
    ),
    Field(
        name=CITATION,
        sql_type="ARRAY<STRUCT<harvested_at: TIMESTAMP, source_url: STRING>>",
        why=(
            "every source that published this text, where silver deduplicated "
            "several into one; the row's own source_url is the most recent"
        ),
        kinds=(DOCUMENT_BLOCK,),
    ),
    Field(
        name=CHARACTER_COUNT,
        sql_type="INT",
        why="reported so an over-long published section is visible; never split on",
    ),
    Field(
        name=CHUNKED_AT,
        sql_type="TIMESTAMP",
        why="when the gold update ran; the fourth clock, and not one of the other three",
    ),
)
"""The chunk metadata schema, fixed. #35's first acceptance criterion.

Ordered identity first, then text, then the per-kind metadata in the order the
kinds are declared, then the citation, then the clocks. #48 builds the search
index schema from this tuple and from nothing else.
"""


def field(name: str) -> Field:
    """Return the chunk field called ``name``.

    Args:
        name: A column name.

    Returns:
        The field.

    Raises:
        KeyError: If the chunk table has no such column.
    """
    for candidate in FIELDS:
        if candidate.name == name:
            return candidate
    raise KeyError(f"no chunk field is called {name!r}")


def retrievable() -> tuple[str, ...]:
    """Return every field an index hit brings back with it."""
    return tuple(entry.name for entry in FIELDS if entry.retrievable)


def filterable() -> tuple[str, ...]:
    """Return every field a query may constrain on."""
    return tuple(entry.name for entry in FIELDS if entry.filterable)


def facetable() -> tuple[str, ...]:
    """Return every field an index may count."""
    return tuple(entry.name for entry in FIELDS if entry.facetable)


# --- Identity ----------------------------------------------------------------

_WHITESPACE: Final = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Return ``text`` in the form used for identity and for length.

    NFKC, every run of whitespace to one space, stripped. Case is deliberately
    not folded, which is :func:`chip_chat.databricks.silver.normalise`'s rule
    and this module asserts it holds the same one.

    Args:
        text: Any published text.

    Returns:
        Its normal form.
    """
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def chunk_id(kind: str, *parts: object) -> str:
    """Return the identifier a citation references.

    Taken over the chunk's **key** — what it is about — and never over its
    text. ``MENU_ITEM:CMG-101`` is the Chicken Bowl's chunk this week and next
    week, through a re-harvest that changes the item's calorie figure.

    That choice is worth defending, because content-addressing is the habit
    everywhere else in this repository: bronze content-addresses the bytes,
    silver content-addresses the prose. Both are landing-zone questions — "have
    I seen this before". A chunk id answers a different one: RFC-001's D9 has
    the response envelope citing ids that the ``retriever.search`` span returned
    on that turn, and #48 rebuilds the index rather than patching it. Under
    content-addressing every weekly rebuild would retire every id whose page
    changed a word, and a conversation two turns old would be citing chunks that
    no longer exist. The wording moving under a stable id is the correct
    behaviour: the citation points at the guacamole chunk, and the guacamole
    chunk now says 230 calories rather than 240.

    A digest rather than the parts joined with a colon, because the parts
    include published headings and URLs — text with colons, slashes and spaces
    in it — and an id that has to be quoted is an id something will eventually
    fail to quote.

    Args:
        kind: One of :data:`KINDS`.
        *parts: The key, in a fixed order per kind. Each is normalised as text;
            ``None`` is distinct from the empty string, because "this document
            published no heading" and "this document published an empty
            heading" are different documents.

    Returns:
        A hex SHA-256 digest.

    Raises:
        ValueError: If ``kind`` is not one of :data:`KINDS`, or if no parts
            were given. An id over the kind alone would make every chunk of
            that kind the same chunk.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown chunk kind {kind!r}; expected one of {KINDS}")
    if not parts:
        raise ValueError(f"a {kind} chunk id needs a key; the kind alone is not one")
    running = hashlib.sha256()
    running.update(kind.encode("utf-8"))
    for part in parts:
        running.update(b"\x00")
        running.update(b"~" if part is None else normalise(str(part)).encode("utf-8"))
    return running.hexdigest()


# --- What a chunk is ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable unit, and everything an answer needs to quote it.

    Every field of :data:`FIELDS` appears here, and ``test_gold.py`` asserts
    that the two lists are the same list. The renderers below return this and
    the pipeline writes it; nothing composes one by hand.

    Attributes:
        chunk_id: See :func:`chunk_id`.
        kind: One of :data:`KINDS`.
        text: What is embedded and read back.
        heading: The published heading, or ``None``.
        item_id: Menu chunks only.
        category: Menu chunks only. ``None`` means "not orderable alone".
        item_type: Menu chunks only.
        primary_filling: Menu chunks only.
        allergens: Menu chunks only. Codes published as ``CONTAINS``, sorted.
        allergen_disclosure: Menu chunks only. One of :data:`DISCLOSURES`.
        calories: Menu chunks only. ``None`` is not zero.
        is_composed: Menu chunks only.
        document_id: Policy sections only.
        document_kind: Policy sections only.
        position: Where the unit falls in its document, where it has one.
        column_headers: Nutrition rows only, one per cell.
        cells: Nutrition rows only, positionally aligned with the headings.
        page_number: Nutrition rows only.
        source_url: Where the text was published.
        harvested_at: When that was fetched. Carried as whatever the caller
            holds — a string from a JSONL fixture, a ``datetime`` from Spark —
            because this module may not import anything to parse it with and a
            timestamp it re-parsed would be a timestamp it could get wrong.
        citations: Document blocks only, where silver collapsed several
            sources into one row.
        character_count: The length of ``text``.
        chunked_at: When the update ran. Set by the pipeline, ``None`` here.
    """

    chunk_id: str
    kind: str
    text: str
    heading: str | None = None
    item_id: str | None = None
    category: str | None = None
    item_type: str | None = None
    primary_filling: str | None = None
    allergens: tuple[str, ...] | None = None
    allergen_disclosure: str | None = None
    calories: str | None = None
    is_composed: bool | None = None
    document_id: str | None = None
    document_kind: str | None = None
    position: int | None = None
    column_headers: tuple[str | None, ...] | None = None
    cells: tuple[str | None, ...] | None = None
    page_number: int | None = None
    source_url: str | None = None
    harvested_at: Any = None
    citations: tuple[Mapping[str, Any], ...] | None = None
    character_count: int = 0
    chunked_at: Any = None

    def as_row(self) -> dict[str, Any]:
        """Return the chunk as the mapping the pipeline writes.

        Keys are exactly :data:`FIELDS`' names, in order, so a Spark struct
        built from the schema accepts it without a positional guess.
        """
        return {entry.name: getattr(self, entry.name) for entry in FIELDS}


def _chunk(kind: str, text: str, key: Sequence[object], **rest: Any) -> Chunk:
    """Build one chunk, with the two things every chunk computes for itself.

    The id and the character count are derived rather than passed, so there is
    nowhere for a caller to hand in an id that does not match the key or a
    length that does not match the text.

    The text is passed through :func:`normalise`, which collapses the harvest's paragraph
    newlines into single spaces. That is deliberate and is the same rule
    identity uses, for the same reason it is the same rule in silver: **the
    structure that carries meaning lives in fields, not in whitespace.** A
    nutrition row's columns are ``column_headers``; a section's subject is
    ``heading``; an item's allergens are an array. Nothing in this corpus means
    anything by a line break, an embedding means nothing by one either, and one
    rule for what two pieces of text are is cheaper than two.
    """
    body = normalise(text)
    return Chunk(
        chunk_id=chunk_id(kind, *key),
        kind=kind,
        text=body,
        character_count=len(body),
        **rest,
    )


def _sentence(*parts: str | None) -> str:
    """Join the non-empty parts with a space. Nothing here invents a word."""
    return " ".join(part for part in parts if part)


def _figure(value: object) -> str:
    """Return a published number as the source published it, not as SQL holds it.

    Silver casts calories to ``DECIMAL(8,2)`` because a published figure is
    exact and a float would not be — see
    :data:`chip_chat.databricks.silver.CALORIES`. That is the right type and
    the wrong *sentence*: the row arrives here holding ``Decimal("230.00")``
    and "230.00 calories" is a number no page printed and no visitor would say.

    So the trailing zeros come off for the prose, and only for the prose. The
    authoritative copy is still the typed column, which is the whole point of
    carrying nutrition as metadata rather than as text: what is trimmed here
    changes what a chunk *reads* like and nothing about what it is quoted for.

    A figure with real precision keeps it — ``1.5`` stays ``1.5`` — because the
    trim stops at the last significant digit rather than at a fixed number of
    places.

    Args:
        value: A number as silver holds it, or the string a fixture holds.

    Returns:
        Its published spelling.
    """
    rendered = str(value)
    if "." not in rendered:
        return rendered
    return rendered.rstrip("0").rstrip(".") or "0"


def _headed(heading: object, text: str) -> str:
    """Return ``text`` with its heading in front, unless it is already there.

    A heading belongs in the chunk's text: it is what the section is *about*,
    and a chunk retrieved without it is a paragraph with no subject. But some
    of the harvest's rows already carry the heading as the first line of their
    own text — Chipotle's "GLUTEN INTOLERANCE & CELIAC DISEASE" caveat is one —
    and prepending it there produces the same words twice in a row.

    That is not merely untidy. It is the failure
    ``test_a_heading_is_not_also_the_first_line_of_its_own_block`` guards
    against one layer down, arriving one layer up: a duplicated heading is a
    fact stated twice, it doubles the weight of those words in the embedding,
    and it reads to a visitor as a stutter in an answer about celiac disease.

    Compared on the normalised forms, because the duplicate differs from the
    heading by exactly the newline the harvest kept.

    Args:
        heading: The published heading, or ``None``.
        text: The section's text.

    Returns:
        The two joined, or ``text`` alone where the heading is already its
        opening.
    """
    body = normalise(text)
    if not heading:
        return body
    opening = normalise(str(heading))
    if opening and body.startswith(opening):
        return body
    return _sentence(f"{opening}.", body)


# --- One menu item is one chunk ----------------------------------------------


def _allergen_sentence(
    codes: Sequence[str], names: Mapping[str, str | None], disclosure: str
) -> str:
    """Return what this item's allergen marks say, in the source's own terms.

    Three states and not two, which is the point. ``NOT_PUBLISHED`` produces a
    sentence that says nothing was published rather than a sentence that says
    nothing — a chunk that stayed silent would be retrieved for an allergen
    question and answer it with an absence, which reads as reassurance.

    The published label is used where the vocabulary has one and the bare code
    where it does not. Two of Chipotle's codes have no published label, and
    inventing one would put a word in the source's mouth in the one place this
    project can least afford it.
    """
    if disclosure == NOT_PUBLISHED:
        return "Chipotle publishes no allergen information for this item."
    if not codes:
        return (
            "Chipotle publishes allergen marks for this item and marks none of "
            "them. That is not a statement that the item is free of them; see "
            "the published allergen caveats."
        )
    rendered = ", ".join(names.get(code) or code for code in codes)
    return f"Marked as containing: {rendered}."


def menu_item_chunk(
    item: Mapping[str, Any],
    allergen_names: Mapping[str, str | None] | None = None,
) -> Chunk:
    """Return the one chunk for one menu item.

    The issue's first rule. Everything the catalogue publishes about the item
    arrives on one row and leaves as one chunk: the prose says what it is, what
    it is built around and what it is marked as containing, and the same facts
    ride alongside as typed columns for the answer path to quote.

    Nothing is looked up, joined or inferred here — ``item`` is a row of
    ``silver_harvested.menu_items`` and the allergen codes are already on it,
    because silver resolved them. This function's whole job is to decide what
    the sentence says and what the columns hold.

    Args:
        item: One row of ``silver_harvested.menu_items``, as a mapping.
        allergen_names: The published vocabulary, ``allergen_code`` to label,
            from ``silver_harvested.allergens``. A code missing from it renders
            as the bare code.

    Returns:
        One :data:`MENU_ITEM` chunk, keyed on the published ``item_id``.

    Raises:
        ValueError: If the row has no ``item_id``, or its
            ``allergen_disclosure`` is not one of :data:`DISCLOSURES`. Both are
            silver expectations, so a violation here means this function was
            handed something that did not come out of silver — and a menu chunk
            with no item id is a chunk about food nobody can name.
    """
    item_id = item.get("item_id")
    if not item_id:
        raise ValueError("a menu chunk needs the published item_id and this row has none")
    disclosure = item.get("allergen_disclosure")
    if disclosure not in DISCLOSURES:
        raise ValueError(
            f"{item_id} has allergen_disclosure {disclosure!r}; silver permits "
            f"only {DISCLOSURES}, and the two silences must not merge"
        )
    codes = tuple(sorted(item.get("allergens") or ()))
    name = str(item.get("name") or "")
    category = item.get("category")
    calories = item.get("calories")

    # "Type: Toppings" rather than "A Toppings", which is what the first draft
    # said and what the hand review of docs/corpus-chunking.md §6 threw out.
    # `item_type` is the menu's own label and several of them are plural or
    # camel-cased -- Toppings, ExtraPortion -- so an article in front of one
    # produces a sentence no publisher wrote and no reader would.
    identity = _sentence(
        f"{name}.",
        f"Type: {item['item_type']}." if item.get("item_type") else None,
        f"Built around {item['primary_filling']}."
        if item.get("primary_filling")
        else None,
        f"Category: {category}." if category else "Only served as part of another item.",
    )
    described = str(item.get("description") or "").strip()
    if calories is None:
        nutrition = "Chipotle publishes no calorie figure for this item."
    elif item.get("is_composed"):
        nutrition = (
            f"{_figure(calories)} calories for this component alone; a full "
            "order adds the calories of whatever is chosen in it."
        )
    else:
        nutrition = f"{_figure(calories)} calories."

    return _chunk(
        MENU_ITEM,
        _sentence(
            identity,
            described,
            nutrition,
            _allergen_sentence(codes, allergen_names or {}, str(disclosure)),
        ),
        key=(item_id,),
        heading=name or None,
        item_id=str(item_id),
        category=category,
        item_type=item.get("item_type"),
        primary_filling=item.get("primary_filling"),
        allergens=codes,
        allergen_disclosure=str(disclosure),
        calories=None if calories is None else str(calories),
        is_composed=bool(item.get("is_composed")),
        source_url=item.get("source_url"),
        harvested_at=item.get("harvested_at"),
    )


# --- Policy and FAQ documents chunk by section -------------------------------


def policy_section_chunk(section: Mapping[str, Any]) -> Chunk:
    """Return the one chunk for one section of a published policy document.

    The issue's second rule. The boundary is the document's own, taken from
    ``silver_harvested.policy_sections``, which took it from the harvest, which
    took it from the page's heading structure. This function does not look at
    the length of the text it is given, and there is deliberately no branch
    here that could.

    Args:
        section: One row of ``silver_harvested.policy_sections``, carrying
            ``document_kind`` and ``document_title`` from silver's join.

    Returns:
        One :data:`POLICY_SECTION` chunk, keyed on the document and the
        published position.

    Raises:
        ValueError: If the row names no document.
    """
    document_id = section.get("document_id")
    if not document_id:
        raise ValueError("a policy section chunk needs the document it is a section of")
    heading = section.get("heading")
    title = section.get("document_title")
    return _chunk(
        POLICY_SECTION,
        _sentence(
            f"{title}." if title else None,
            _headed(heading, str(section.get("text") or "")),
        ),
        key=(document_id, section.get("position")),
        heading=heading or title,
        document_id=str(document_id),
        document_kind=section.get("document_kind"),
        position=section.get("position"),
        source_url=section.get("source_url"),
        harvested_at=section.get("harvested_at"),
    )


def faq_entry_chunk(entry: Mapping[str, Any]) -> Chunk:
    """Return the one chunk for one published question and its answer.

    A FAQ's sections are its questions, so the question is the heading and the
    answer is the section, and the two are never separated: an answer retrieved
    without its question is an answer to something the reader has to guess.

    The published category and subcategory are prefixed to the text rather than
    dropped. They are the FAQ's own two-level table of contents, and "Rewards >
    Points" in front of an answer about expiry is the difference between a
    chunk that matches "do my Chipotle points expire" and one that matches only
    the word "expire".

    Args:
        entry: One row of ``silver_harvested.faq_entries``.

    Returns:
        One :data:`FAQ_ENTRY` chunk, keyed on the published category,
        subcategory and rank — which is the row's silver identity.

    Raises:
        ValueError: If the row carries no question.
    """
    question = str(entry.get("question") or "").strip()
    if not question:
        raise ValueError("a FAQ chunk needs the published question; this row has none")
    category = entry.get("category")
    subcategory = entry.get("subcategory")
    trail = " > ".join(str(part) for part in (category, subcategory) if part)
    return _chunk(
        FAQ_ENTRY,
        _sentence(
            f"{trail}." if trail else None,
            question if question.endswith("?") else f"{question}.",
            str(entry.get("answer") or ""),
        ),
        key=(category, subcategory, entry.get("rank")),
        heading=question,
        position=entry.get("rank"),
        source_url=entry.get("source_url"),
        harvested_at=entry.get("harvested_at"),
    )


def allergen_caveat_chunk(caveat: Mapping[str, Any]) -> Chunk:
    """Return the one chunk for one paragraph of Chipotle's allergen caveats.

    These travel as chunks rather than as a footer on every menu chunk. The
    argument is in :data:`ALLERGEN_CAVEAT`: appended, the cross-contact caveat
    would sit inside hundreds of item chunks and dominate their embeddings; as
    chunks, the agent has to retrieve them, which is what the allergen path is
    for and what the eval set measures.

    Args:
        caveat: One row of ``silver_harvested.caveats``.

    Returns:
        One :data:`ALLERGEN_CAVEAT` chunk, keyed on the published position.

    Raises:
        ValueError: If the paragraph is empty. Silver's ``required`` already
            forbids it; a violation here means the row did not come from
            silver.
    """
    text = str(caveat.get("text") or "").strip()
    if not text:
        raise ValueError("an allergen caveat chunk with no text is not a caveat")
    heading = caveat.get("heading")
    return _chunk(
        ALLERGEN_CAVEAT,
        _headed(heading, text),
        key=(caveat.get("position"),),
        heading=heading,
        position=caveat.get("position"),
        source_url=caveat.get("source_url"),
        harvested_at=caveat.get("harvested_at"),
    )


def document_block_chunk(block: Mapping[str, Any]) -> Chunk:
    """Return the one chunk for one deduplicated block of harvested prose.

    The split already happened, in ``silver.extract_blocks``, at the headings
    the documents themselves published — and the same block published on three
    pages is already one row with three citations. Nothing about the boundary is
    revisited here. What is added is the id, the heading in the text, and the
    citation array carried onto the chunk so the payload holds every source and
    not only the most recent.

    Args:
        block: One row of ``silver_harvested.document_blocks``.

    Returns:
        One :data:`DOCUMENT_BLOCK` chunk, keyed on silver's ``block_sha256``
        — which is the one kind whose key *is* its content, because a block of
        anonymous prose has no published identifier to name it by.

    Raises:
        ValueError: If the row carries no ``block_sha256``.
    """
    digest = block.get("block_sha256")
    if not digest:
        raise ValueError("a document block chunk is keyed on silver's block_sha256")
    heading = block.get("heading")
    citations = block.get("citations")
    return _chunk(
        DOCUMENT_BLOCK,
        _headed(heading, str(block.get("text") or "")),
        key=(digest,),
        heading=heading,
        citations=tuple(citations) if citations else None,
        source_url=block.get("source_url"),
        harvested_at=block.get("harvested_at"),
    )


# --- A nutrition row is a chunk, and a window cannot make one ----------------


def nutrition_row_chunk(row: Mapping[str, Any]) -> Chunk:
    """Return the one chunk for one row of an extracted nutrition table.

    This is the function #35 is really about. It is handed a row — the whole
    row, with one heading per cell, exactly as
    :func:`chip_chat.databricks.silver.analysis_table_rows` guarantees — and it
    renders every cell beside the heading that gives it meaning. It has no
    access to a character offset and no concept of a boundary, so it cannot
    produce the chunk ``docs/decisions/pdf-tables.md`` describes: a sodium
    figure sitting next to the wrong item's name under no heading at all.

    The headings ride on the chunk as :data:`COLUMN_HEADERS` and ``cells`` as
    well as inside the prose. That redundancy is deliberate. The prose is what
    gets embedded and matched; the arrays are what
    ``test_no_nutrition_table_is_split_across_a_chunk_boundary`` checks, and
    what an answer quotes when it needs the figure rather than the sentence.

    A hole — a position the service reported no cell for — is rendered as
    "not published" rather than skipped or blanked. Skipping it would shift
    every heading after it onto the wrong number.

    Args:
        row: One row of ``silver_harvested.document_tables``.

    Returns:
        One :data:`NUTRITION_ROW` chunk, keyed on the analysed document, the
        table and the row index.

    Raises:
        ValueError: If the row has more cells than headings or fewer. Silver
            expects one heading per cell; a row that arrives here without one
            has figures whose column is a guess, and guessing is the failure
            this whole kind exists to prevent.
    """
    headers = tuple(row.get("column_headers") or ())
    cells = tuple(row.get("cells") or ())
    if len(headers) != len(cells):
        raise ValueError(
            f"a nutrition row needs one heading per cell; got {len(headers)} "
            f"headings for {len(cells)} cells, which leaves a figure whose "
            "column is a guess"
        )
    if not cells:
        raise ValueError("a nutrition row with no cells is not a row")

    caption = row.get("caption")
    pairs = ", ".join(
        f"{header or 'unlabelled column'}: {'not published' if cell is None else cell}"
        for header, cell in zip(headers, cells, strict=True)
    )
    page = row.get("page_number")
    return _chunk(
        NUTRITION_ROW,
        _sentence(
            f"{caption}." if caption else None,
            f"{pairs}.",
            f"Published figures, read from page {page} of the source document."
            if page is not None
            else "Published figures, read from the source document.",
        ),
        key=(row.get("content_sha256"), row.get("table_index"), row.get("row_index")),
        heading=caption,
        column_headers=headers,
        cells=cells,
        page_number=page,
        source_url=row.get("source_url"),
        harvested_at=row.get("harvested_at"),
    )


# --- The sources, as data ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Source:
    """One silver table, and the kind of chunk it becomes.

    The pipeline is a loop over these.

    Attributes:
        kind: One of :data:`KINDS`.
        table: The silver table read, unqualified. Always in
            ``silver_harvested``; see :data:`STREAM`.
        renderer: The module-level function that turns one row into one chunk.
            Named here so ``test_gold.py`` can assert that every kind has one
            and that no renderer is unreachable.
        columns: The silver columns the renderer reads, in the order the
            renderer's row is assembled from them. The pipeline projects
            exactly these, which keeps a rename in silver a failure at the
            pipeline's first select rather than a null in a chunk.
        why: What this kind of chunk answers, one line.
        vocabulary: A silver table of published labels the renderer needs
            beside the row, or ``None``. Only :data:`MENU_ITEM` has one —
            ``allergens``, nine rows of code-to-label — and the pipeline
            collects it to the driver rather than joining it, because a
            published vocabulary of nine rows is a broadcast and a join here
            would multiply the menu by it.
    """

    kind: str
    table: str
    renderer: str
    columns: tuple[str, ...]
    why: str
    vocabulary: str | None = None


SOURCES: Final[tuple[Source, ...]] = (
    Source(
        kind=MENU_ITEM,
        table="menu_items",
        renderer="menu_item_chunk",
        columns=(
            "item_id",
            "name",
            "category",
            "item_type",
            "primary_filling",
            "description",
            "calories",
            "is_composed",
            "allergens",
            "allergen_disclosure",
            "source_url",
            "harvested_at",
        ),
        why=(
            "what an item is, what it costs the reader in calories, what it is "
            "marked as containing"
        ),
        vocabulary="allergens",
    ),
    Source(
        kind=POLICY_SECTION,
        table="policy_sections",
        renderer="policy_section_chunk",
        columns=(
            "document_id",
            "document_kind",
            "document_title",
            "position",
            "heading",
            "text",
            "source_url",
            "harvested_at",
        ),
        why="the published rules, at the boundaries the document drew",
    ),
    Source(
        kind=FAQ_ENTRY,
        table="faq_entries",
        renderer="faq_entry_chunk",
        columns=(
            "category",
            "subcategory",
            "rank",
            "question",
            "answer",
            "source_url",
            "harvested_at",
        ),
        why="a question Chipotle already answered, with its answer attached",
    ),
    Source(
        kind=ALLERGEN_CAVEAT,
        table="caveats",
        renderer="allergen_caveat_chunk",
        columns=("position", "heading", "text", "source_url", "harvested_at"),
        why="what the allergen chart does not cover, in Chipotle's own words",
    ),
    Source(
        kind=DOCUMENT_BLOCK,
        table="document_blocks",
        renderer="document_block_chunk",
        columns=(
            "block_sha256",
            "heading",
            "text",
            "citations",
            "source_url",
            "harvested_at",
        ),
        why="everything else published as prose, deduplicated, every source kept",
    ),
    Source(
        kind=NUTRITION_ROW,
        table="document_tables",
        renderer="nutrition_row_chunk",
        columns=(
            "content_sha256",
            "table_index",
            "row_index",
            "caption",
            "page_number",
            "column_headers",
            "cells",
            "source_url",
            "harvested_at",
        ),
        why="one row of a published nutrition table, whole, with its headings",
    ),
)
"""Every silver table that becomes chunks, in the order :data:`KINDS` names them.

There are exactly six and there is no default branch. A silver table that is not
here produces no chunks, which is the intended answer for the ones that are
account data, price lists or manifests: RFC-001 §04's boundary says a synthetic
order must never reach retrieval, and the way to keep that true is for there to
be no code path that could take it there.
"""


def source(kind: str) -> Source:
    """Return the source for one chunk kind.

    Args:
        kind: One of :data:`KINDS`.

    Returns:
        Its source declaration.

    Raises:
        KeyError: If nothing produces that kind.
    """
    for candidate in SOURCES:
        if candidate.kind == kind:
            return candidate
    raise KeyError(f"nothing produces a {kind!r} chunk")


# --- Expectations ------------------------------------------------------------
#
# Fatal, all of them, for the reason silver's are: the issue asks for chunks a
# retrieval index can be built from, and a chunk that reaches the index without
# a citation is a claim the response envelope cannot support. There is no warn
# level here and no `expect_or_drop` -- a dropped chunk is a fact that silently
# stops being retrievable, which is the corpus quietly getting smaller.


@dataclass(frozen=True, slots=True)
class Expectation:
    """One constraint every row of the chunk table must satisfy.

    Deliberately the same shape as
    :class:`chip_chat.databricks.silver.Expectation` and deliberately not
    imported from it, for the reason the module docstring gives. ``test_gold``
    asserts the two classes carry the same fields.

    Attributes:
        name: As it appears in the event log. A statement of what is true.
        constraint: A SQL boolean over the chunk table's own columns.
        why: Why this is worth stopping a pipeline for.
    """

    name: str
    constraint: str
    why: str


def _only(kind: str, constraint: str) -> str:
    """Return ``constraint``, applied only to chunks of ``kind``.

    A per-kind expectation has to pass trivially for every other kind, and the
    obvious spelling — ``kind = 'X' AND ...`` — fails every row that is not an
    X. This is the implication, written the way SQL spells one.
    """
    return f"{KIND} <> '{kind}' OR ({constraint})"


def expectations() -> tuple[Expectation, ...]:
    """Return every constraint the chunk table is built under.

    Returns:
        The expectations, universal ones first, then the per-kind ones in the
        order :data:`KINDS` declares.
    """
    kinds = ", ".join(f"'{kind}'" for kind in KINDS)
    return (
        Expectation(
            name="carries_its_citation",
            constraint=f"{SOURCE_URL} IS NOT NULL AND {HARVESTED_AT} IS NOT NULL",
            why=(
                "#35's second required test, as a constraint: citations are part "
                "of the payload rather than reconstructed afterwards, and a "
                "chunk that reaches the index without one is a sentence the "
                "response envelope cannot attribute to anybody"
            ),
        ),
        Expectation(
            name="says_something",
            constraint=f"{CHARACTER_COUNT} > 0",
            why=(
                "an empty chunk embeds to a vector near everything and is "
                "retrieved for questions it has no answer to"
            ),
        ),
        Expectation(
            name="is_one_of_the_published_structures",
            constraint=f"{KIND} IN ({kinds})",
            why=(
                "a kind outside this list is a chunk drawn at a boundary "
                "nothing published, which is the fixed window arriving under "
                "another name"
            ),
        ),
        Expectation(
            name="knows_what_it_is_about",
            constraint=_only(MENU_ITEM, f"{ITEM_ID} IS NOT NULL"),
            why=(
                "a menu chunk that cannot name its item cannot be joined to a "
                "price, an allergen row or an order"
            ),
        ),
        Expectation(
            name="allergen_silence_says_which_kind",
            constraint=_only(
                MENU_ITEM,
                "{} IN ({})".format(
                    ALLERGEN_DISCLOSURE, ", ".join(f"'{s}'" for s in DISCLOSURES)
                ),
            ),
            why=(
                "an empty allergens array means either 'marks are published and "
                "none is this one' or 'nothing is published at all', and a "
                "chunk that cannot say which has merged the two silences — the "
                "second read as the first is a wrong allergen answer"
            ),
        ),
        Expectation(
            name="keeps_the_row_whole",
            constraint=_only(
                NUTRITION_ROW,
                f"size({COLUMN_HEADERS}) = size(cells) AND size(cells) > 0",
            ),
            why=(
                "#35's first required test, as a constraint. A nutrition row "
                "with fewer headings than cells has figures whose column is a "
                "guess, which is exactly what a fixed window produces and what "
                "an allergen or calorie answer cannot survive"
            ),
        ),
    )


def columns_for(kind: str) -> Iterator[Field]:
    """Yield every field a chunk of ``kind`` populates, universal ones included.

    Args:
        kind: One of :data:`KINDS`.

    Yields:
        The fields, in :data:`FIELDS` order.

    Raises:
        ValueError: If ``kind`` is not one of :data:`KINDS`.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown chunk kind {kind!r}; expected one of {KINDS}")
    for entry in FIELDS:
        if entry.universal or kind in entry.kinds:
            yield entry
