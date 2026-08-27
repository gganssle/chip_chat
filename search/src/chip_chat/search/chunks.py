"""What a chunk carries, restated here so the index can be built from it.

Issue #35 fixed the chunk metadata schema and put it in
``chip_chat.databricks.gold_chunks.FIELDS`` — name, type, and what a search index
may do with each column — precisely so that #48 would not invent a second one.
This module is that tuple again, minus the prose, and
``search/tests/test_chunk_contract.py`` asserts the two are the same list.

**Why a copy rather than an import**, since the obvious reading of "one copy" is
that this file should not exist. ``gold_chunks.py`` is a *Spark driver* module:
a Lakeflow pipeline runs a notebook in the workspace rather than an installed
wheel, so Terraform uploads that exact file beside the notebook and it imports
two ways — as ``chip_chat.databricks.gold_chunks`` under pytest and as a flat
top-level ``gold_chunks`` on the driver. Its own docstring is explicit that the
constants it shares with ``silver.py``, ``catalog.py`` and ``chip_chat.data_gen``
are "spelled out again below and asserted equal to theirs in the tests rather
than imported". This is that same convention applied one layer further out, and
the test is what makes it a convention rather than a duplicate.

There is a second reason, and it is the one that would matter even if the driver
constraint went away. An index schema and a table schema are allowed to
disagree, and the interesting cases are the ones where they should: ``calories``
is ``DECIMAL(8,2)`` in Delta and ``Edm.Double`` here because Azure AI Search has
no decimal type; ``allergens`` is filterable in both and searchable in neither,
for a reason worth writing down. A direct import would hide those decisions
behind a loop. Here they are :data:`SEARCHABLE` and :data:`EDM`, and each one is
a line somebody can argue with.

The index adds one field that no chunk carries — the vector — and that lives in
:mod:`chip_chat.search.schema`, because it belongs to the index rather than to
the corpus.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = [
    "ALLERGENS",
    "ALLERGEN_DISCLOSURE",
    "CALORIES",
    "CHUNK_ID",
    "EDM",
    "FIELDS",
    "HARVESTED_AT",
    "HEADING",
    "KIND",
    "KINDS",
    "SEARCHABLE",
    "SOURCE_URL",
    "TEXT",
    "ChunkField",
    "edm_type_of",
    "facetable",
    "field",
    "filterable",
    "names",
    "retrievable",
]

# --- The kinds, copied from gold_chunks.KINDS -------------------------------

MENU_ITEM: Final = "MENU_ITEM"
POLICY_SECTION: Final = "POLICY_SECTION"
FAQ_ENTRY: Final = "FAQ_ENTRY"
ALLERGEN_CAVEAT: Final = "ALLERGEN_CAVEAT"
DOCUMENT_BLOCK: Final = "DOCUMENT_BLOCK"
NUTRITION_ROW: Final = "NUTRITION_ROW"

KINDS: Final[tuple[str, ...]] = (
    MENU_ITEM,
    POLICY_SECTION,
    FAQ_ENTRY,
    ALLERGEN_CAVEAT,
    DOCUMENT_BLOCK,
    NUTRITION_ROW,
)
"""Every kind of chunk. Asserted equal to ``gold_chunks.KINDS``."""

# --- Field names that are referenced by name elsewhere in this package ------

CHUNK_ID: Final = "chunk_id"
KIND: Final = "kind"
TEXT: Final = "text"
HEADING: Final = "heading"
CATEGORY: Final = "category"
ITEM_TYPE: Final = "item_type"
PRIMARY_FILLING: Final = "primary_filling"
ALLERGENS: Final = "allergens"
ALLERGEN_DISCLOSURE: Final = "allergen_disclosure"
CALORIES: Final = "calories"
SOURCE_URL: Final = "source_url"
HARVESTED_AT: Final = "harvested_at"
CITATIONS: Final = "citations"


@dataclass(frozen=True, slots=True)
class ChunkField:
    """One column of the chunk table, and what the index may do with it.

    The four flags are ``gold_chunks.Field``'s, unchanged and in the same
    sense.

    Attributes:
        name: The column name. The same string in Delta and in the index.
        sql_type: The Delta type, kept verbatim so the contract test can
            compare strings rather than a mapping's opinion of them.
        retrievable: Whether a hit brings the field back with it.
        filterable: Whether a query may constrain on it.
        facetable: Whether the index may count it.
        kinds: The chunk kinds that populate it, empty for the universal ones.
    """

    name: str
    sql_type: str
    retrievable: bool = True
    filterable: bool = False
    facetable: bool = False
    kinds: tuple[str, ...] = ()

    @property
    def universal(self) -> bool:
        """Whether every chunk carries this field, whatever its kind."""
        return not self.kinds


FIELDS: Final[tuple[ChunkField, ...]] = (
    ChunkField(CHUNK_ID, "STRING", filterable=True),
    ChunkField(KIND, "STRING", filterable=True, facetable=True),
    ChunkField(TEXT, "STRING"),
    ChunkField(HEADING, "STRING"),
    ChunkField("item_id", "STRING", filterable=True, kinds=(MENU_ITEM,)),
    ChunkField(CATEGORY, "STRING", filterable=True, facetable=True, kinds=(MENU_ITEM,)),
    ChunkField(ITEM_TYPE, "STRING", filterable=True, facetable=True, kinds=(MENU_ITEM,)),
    ChunkField(
        PRIMARY_FILLING, "STRING", filterable=True, facetable=True, kinds=(MENU_ITEM,)
    ),
    ChunkField(
        ALLERGENS,
        "ARRAY<STRING>",
        filterable=True,
        facetable=True,
        kinds=(MENU_ITEM,),
    ),
    ChunkField(ALLERGEN_DISCLOSURE, "STRING", filterable=True, kinds=(MENU_ITEM,)),
    ChunkField(CALORIES, "DECIMAL(8,2)", filterable=True, kinds=(MENU_ITEM,)),
    ChunkField("is_composed", "BOOLEAN", filterable=True, kinds=(MENU_ITEM,)),
    ChunkField("document_id", "STRING", filterable=True, kinds=(POLICY_SECTION,)),
    ChunkField(
        "document_kind",
        "STRING",
        filterable=True,
        facetable=True,
        kinds=(POLICY_SECTION,),
    ),
    ChunkField("position", "INT", kinds=(POLICY_SECTION, ALLERGEN_CAVEAT, FAQ_ENTRY)),
    ChunkField("column_headers", "ARRAY<STRING>", kinds=(NUTRITION_ROW,)),
    ChunkField("cells", "ARRAY<STRING>", kinds=(NUTRITION_ROW,)),
    ChunkField("page_number", "INT", kinds=(NUTRITION_ROW,)),
    ChunkField(SOURCE_URL, "STRING", filterable=True),
    ChunkField(HARVESTED_AT, "TIMESTAMP", filterable=True),
    ChunkField(
        CITATIONS,
        "ARRAY<STRUCT<harvested_at: TIMESTAMP, source_url: STRING>>",
        kinds=(DOCUMENT_BLOCK,),
    ),
    ChunkField("character_count", "INT"),
    ChunkField("chunked_at", "TIMESTAMP"),
)
"""The chunk metadata schema, in ``gold_chunks.FIELDS``' order.

#48's first scope bullet is that the index carries *every* chunk metadata field
and that the citation fields are "retrievable and not merely filterable". That
is a property of this tuple rather than of the index builder: every entry here
becomes a field of the index, and :func:`retrievable` is what the builder marks
retrievable.
"""


# --- What the index does that the table does not ----------------------------

SEARCHABLE: Final[frozenset[str]] = frozenset(
    {TEXT, HEADING, CATEGORY, ITEM_TYPE, PRIMARY_FILLING}
)
"""The fields BM25 matches on. Five, and the shape of the list is the argument.

:data:`TEXT` and :data:`HEADING` are the obvious two. RFC-001 §08 explains why
there are three more: *"keyword recall matters here more than usual, because
item names are proper nouns that embeddings handle poorly."* ``barbacoa``,
``sofritas`` and ``lifestyle bowl`` are exactly the tokens a general-purpose
embedding places badly and a lexical index places perfectly, and they live in
``heading``, ``item_type`` and ``primary_filling``. Each is a short closed
vocabulary, so the cost of making it searchable is a rounding error against the
50 MB the Free tier allows.

**:data:`ALLERGENS` is deliberately not here, and that is a safety decision
rather than a size one.** A searchable ``allergens`` field lets *"something
without dairy"* score the dairy items highest, because "dairy" is the one token
they all contain and free-text search has no idea the query negated it. The
product question is a filter — ``allergens/any(a: a eq 'DAIRY')`` — and a filter
is exact, so the only way to answer it wrongly is to answer a different
question. ``allergen_disclosure`` stays out for the same reason: it is read
alongside an empty ``allergens`` array to tell "nothing is published about this
item" apart from "this item is marked with nothing", and neither sentence is one
a scorer should be blending into a relevance number. See
``docs/decisions/allergen-absence.md``.
"""

EDM: Final[Mapping[str, str]] = {
    "STRING": "Edm.String",
    "ARRAY<STRING>": "Collection(Edm.String)",
    "INT": "Edm.Int32",
    "BOOLEAN": "Edm.Boolean",
    "TIMESTAMP": "Edm.DateTimeOffset",
    "DECIMAL(8,2)": "Edm.Double",
    "ARRAY<STRUCT<harvested_at: TIMESTAMP, source_url: STRING>>": (
        "Collection(Edm.ComplexType)"
    ),
}
"""Delta type to Azure AI Search EDM type. Two entries are not a translation.

``DECIMAL(8,2)`` becomes ``Edm.Double`` because **Azure AI Search has no decimal
type**; ``Edm.Double`` is the widest numeric it offers. Silver casts calories to
``DECIMAL(8,2)`` on purpose — a published figure is exact and an
allergen-adjacent number that acquires a rounding error is the kind of wrong
this project cannot have — so it is worth saying exactly what is and is not lost
here. Chipotle publishes calories as whole numbers; every integer up to
2**53 is exactly representable as a double, so no published figure changes
value. What the cast loses is the *guarantee*, not any current number: a source
that started publishing halves would still be exact, and one that published
thirds would not. And ``calories`` is filterable rather than arithmetic — the
index answers "fewer than 500", it never adds two of these together — so a
comparison here cannot accumulate error even in the case the guarantee is gone.

``ARRAY<STRUCT<...>>`` becomes ``Collection(Edm.ComplexType)``, whose subfields
are spelled out by :func:`chip_chat.search.schema.complex_subfields` rather than
parsed out of that string. Parsing a Spark type expression to build an index
schema would be a small parser nobody asked for, and the struct has two members.
"""


def field(name: str) -> ChunkField:
    """Return the chunk field called ``name``.

    Args:
        name: A column name.

    Returns:
        The field.

    Raises:
        KeyError: If the chunk schema has no such column.
    """
    for candidate in FIELDS:
        if candidate.name == name:
            return candidate
    raise KeyError(f"no chunk field is called {name!r}")


def names() -> tuple[str, ...]:
    """Return every chunk field name, in schema order."""
    return tuple(entry.name for entry in FIELDS)


def retrievable() -> tuple[str, ...]:
    """Return every field an index hit brings back with it."""
    return tuple(entry.name for entry in FIELDS if entry.retrievable)


def filterable() -> tuple[str, ...]:
    """Return every field a query may constrain on."""
    return tuple(entry.name for entry in FIELDS if entry.filterable)


def facetable() -> tuple[str, ...]:
    """Return every field the index may count."""
    return tuple(entry.name for entry in FIELDS if entry.facetable)


def edm_type_of(entry: ChunkField) -> str:
    """Return the Azure AI Search type for ``entry``.

    Args:
        entry: A chunk field.

    Returns:
        The EDM type name.

    Raises:
        KeyError: If the field's Delta type has no mapping. Raised rather than
            defaulted to ``Edm.String``: a new column whose type nobody
            considered should stop a build, not arrive silently as text.
    """
    try:
        return EDM[entry.sql_type]
    except KeyError:
        raise KeyError(
            f"{entry.name} is {entry.sql_type}, which has no Azure AI Search "
            f"equivalent declared in chip_chat.search.chunks.EDM"
        ) from None
