"""The index definition, built from the chunk schema and from three decisions.

Everything here is a pure function of :mod:`chip_chat.search.chunks` and an
:class:`~chip_chat.search.embedding.EmbeddingDeployment`, so the whole index
definition can be asserted in CI without a search service. That matters more
than usual for this issue: the properties #48 asks for — *every* chunk field
retrievable, the citation fields among them, the filters the comparative
questions need — are properties of a JSON document, and a test that reads the
JSON is a stronger check than one that creates an index and queries it.

Three things the index does that the chunk table does not, each of which is a
decision rather than a translation.

**The vector field is not stored, and it is quantized.** ``text_vector`` is
``searchable`` and neither ``retrievable`` nor ``stored``: the caller wants the
chunk, never the 1536 floats that found it. ``stored: false`` drops the JSON
copy the service otherwise keeps in order to be able to return one — about 30 KB
of text per chunk that nothing reads — and it is irreversible without a rebuild,
which on this index is a normal Tuesday. Scalar quantization to ``int8`` then
takes the resident vector index to a quarter of its size, with
``rerankWithOriginalVectors`` so the top candidates are re-scored against the
full-precision copy and recall is not what pays for it.

Both exist because of the Free tier's **50 MB**, and the alias pattern is what
makes that number tighter than it looks: a live index and a rebuilding index are
resident at the same time, so every byte per chunk is charged twice for the
length of a build. That is the same 3-index budget :mod:`chip_chat.search.build`
spends — live, rebuilding, and one spare — and it is why the third is headroom
rather than a second feature.

**Five fields are searchable and ``allergens`` is not.** See
:data:`chip_chat.search.chunks.SEARCHABLE`; the short version is that "without
dairy" is a filter, and a searchable allergen field answers it backwards.

**The heading outweighs the body.** RFC-001 §08 keeps keyword recall in the
design because *"item names are proper nouns that embeddings handle poorly"*,
and a menu item's name is its heading. :data:`SCORING_PROFILE` is that sentence
as a weight, applied to the BM25 half of a hybrid query; the semantic reranker
#49 puts on top re-orders what this recalls, and cannot recall what this missed.
"""

from typing import Any, Final

from chip_chat.search import chunks
from chip_chat.search.embedding import EmbeddingDeployment

__all__ = [
    "ALIAS",
    "API_VERSION",
    "HNSW_ALGORITHM",
    "SCORING_PROFILE",
    "SEMANTIC_CONFIGURATION",
    "VECTORIZER",
    "VECTOR_COMPRESSION",
    "VECTOR_FIELD",
    "VECTOR_PROFILE",
    "complex_subfields",
    "index",
    "index_name",
    "run_id_of",
]

API_VERSION: Final = "2025-08-01-preview"
"""The search REST version. A **preview** one, and not by preference.

Everywhere else in this repository an Azure API version is pinned to the newest
GA — ``chip_chat.agent.foundry`` pins ``2024-10-21`` and says why: a silently
newer API version is a silently different response shape. The same argument
applies here and loses, because on this service **index aliases do not exist
outside preview**. Probed against ``srch-chip-chat-4cy39i`` on 2026-08-27:

    for v in 2023-11-01 2024-07-01 2025-09-01 \
             2024-09-01-preview 2024-11-01-preview 2025-08-01-preview; do
      curl -s -o /dev/null -w "$v %{http_code}\n" \
        -H "Authorization: Bearer $TOKEN" \
        "$AZURE_SEARCH_ENDPOINT/aliases?api-version=$v"
    done

Every GA version answers ``/indexes`` with 200 and ``/aliases`` with **400,
"The version indicated by the api-version query string parameter does not
exist"**. Every preview version in that range answers both with 200. So the
choice is not "GA or preview", it is "an alias or no alias" — and RFC-001 §08
does not leave that open: *the index is rebuilt, never patched*, and without an
alias the application would have to be told an index name that changes every
week. That is worth a preview contract, and it is worth knowing it is one.

Two consequences to hold onto. The retrieval lane is the one place in this
estate where a Microsoft-side change can break a deployment without anybody
editing a file, so this constant is the first thing to check when a build starts
failing for no local reason. And the exposure is bounded by the same property
the rest of the design turns on: an index is rebuilt rather than migrated, so
moving to a different API version is a build and a swap, not a data migration.
"""

ALIAS: Final = "corpus"
"""The one name the application ever uses. See :mod:`chip_chat.search.build`."""

VECTOR_FIELD: Final = "text_vector"
"""The embedding of :data:`chip_chat.search.chunks.TEXT`, and the only field of
the index that is not a chunk field."""

VECTOR_PROFILE: Final = "corpus-hnsw"
HNSW_ALGORITHM: Final = "hnsw"
VECTOR_COMPRESSION: Final = "int8"
VECTORIZER: Final = "corpus-aoai"
SEMANTIC_CONFIGURATION: Final = "corpus-semantic"
SCORING_PROFILE: Final = "heading-weighted"

_SUBFIELDS: Final[dict[str, tuple[tuple[str, str], ...]]] = {
    chunks.CITATIONS: (
        ("source_url", "Edm.String"),
        ("harvested_at", "Edm.DateTimeOffset"),
    ),
}
"""Members of the complex types, spelled out rather than parsed.

``citations`` is ``ARRAY<STRUCT<harvested_at: TIMESTAMP, source_url: STRING>>``
in Delta. Deriving these two lines by parsing that string would be a small Spark
type parser written to save two lines, and the contract test in
``test_chunk_contract.py`` compares the Delta type verbatim, so a member added
upstream fails there rather than being silently dropped here.
"""


def complex_subfields(name: str) -> tuple[dict[str, Any], ...]:
    """Return the index sub-fields of the complex chunk field ``name``.

    Args:
        name: A chunk field whose type is a complex collection.

    Returns:
        The sub-field definitions.

    Raises:
        KeyError: If the field has no declared members.
    """
    return tuple({"name": member, "type": edm} for member, edm in _SUBFIELDS[name])


def _field(entry: chunks.ChunkField) -> dict[str, Any]:
    """Return the index field for one chunk field."""
    edm = chunks.edm_type_of(entry)
    definition: dict[str, Any] = {
        "name": entry.name,
        "type": edm,
        "key": entry.name == chunks.CHUNK_ID,
        "retrievable": entry.retrievable,
        "searchable": entry.name in chunks.SEARCHABLE,
        "filterable": entry.filterable,
        "facetable": entry.facetable,
        "sortable": False,
    }
    if edm == "Collection(Edm.ComplexType)":
        # A complex collection is `name`, `type` and `fields`, and nothing
        # else: the flags are properties of its members. Its members here are
        # left at their defaults, which are not searchable and retrievable --
        # a citation the application cannot read back is not a citation.
        for flag in (
            "key",
            "retrievable",
            "searchable",
            "filterable",
            "facetable",
            "sortable",
        ):
            definition.pop(flag)
        definition["fields"] = list(complex_subfields(entry.name))
    elif edm.startswith("Collection("):
        # A collection is never sortable — there is no one value to sort on —
        # and the service rejects `sortable: false` on one rather than ignoring
        # it, which is the more helpful of the two behaviours and the reason
        # this branch is here rather than in a comment.
        definition.pop("sortable")
    return definition


def _vector_field(deployment: EmbeddingDeployment) -> dict[str, Any]:
    """Return the vector field definition."""
    return {
        "name": VECTOR_FIELD,
        "type": "Collection(Edm.Single)",
        "searchable": True,
        "retrievable": False,
        "stored": False,
        "dimensions": deployment.dimensions,
        "vectorSearchProfile": VECTOR_PROFILE,
    }


def _vector_search(
    deployment: EmbeddingDeployment, api_key: str | None
) -> dict[str, Any]:
    """Return the ``vectorSearch`` section."""
    profile: dict[str, Any] = {
        "name": VECTOR_PROFILE,
        "algorithm": HNSW_ALGORITHM,
        "compression": VECTOR_COMPRESSION,
    }
    section: dict[str, Any] = {
        "algorithms": [
            {
                "name": HNSW_ALGORITHM,
                "kind": "hnsw",
                "hnswParameters": {
                    "metric": "cosine",
                    "m": 4,
                    "efConstruction": 400,
                    "efSearch": 500,
                },
            }
        ],
        "compressions": [
            {
                "name": VECTOR_COMPRESSION,
                "kind": "scalarQuantization",
                "rerankWithOriginalVectors": True,
                "defaultOversampling": 10,
                "scalarQuantizationParameters": {"quantizedDataType": "int8"},
            }
        ],
        "profiles": [profile],
    }
    if api_key is not None:
        section["vectorizers"] = [deployment.vectorizer(VECTORIZER, api_key)]
        profile["vectorizer"] = VECTORIZER
    return section


def _semantic() -> dict[str, Any]:
    """Return the ``semantic`` section.

    The reranker is #49's, not this issue's, but the *configuration* is part of
    the index and an index is rebuilt rather than altered — so leaving it out
    would mean #49 could not turn reranking on without rebuilding the corpus.
    ``semanticSearch`` is set to ``free`` on the service (``search.tf``), which
    grants 1,000 reranked queries a month and then stops; #49's
    degrade-to-hybrid path is what that ceiling buys.
    """
    return {
        "defaultConfiguration": SEMANTIC_CONFIGURATION,
        "configurations": [
            {
                "name": SEMANTIC_CONFIGURATION,
                "prioritizedFields": {
                    "titleField": {"fieldName": chunks.HEADING},
                    "prioritizedContentFields": [{"fieldName": chunks.TEXT}],
                    "prioritizedKeywordsFields": [
                        {"fieldName": chunks.ITEM_TYPE},
                        {"fieldName": chunks.PRIMARY_FILLING},
                        {"fieldName": chunks.CATEGORY},
                    ],
                },
            }
        ],
    }


def _scoring_profiles() -> list[dict[str, Any]]:
    """Return the scoring profiles. One, and it weights the heading."""
    return [
        {
            "name": SCORING_PROFILE,
            "text": {
                "weights": {
                    chunks.HEADING: 3.0,
                    chunks.ITEM_TYPE: 2.0,
                    chunks.PRIMARY_FILLING: 2.0,
                    chunks.TEXT: 1.0,
                    chunks.CATEGORY: 1.0,
                }
            },
        }
    ]


def index(
    name: str,
    deployment: EmbeddingDeployment,
    vectorizer_key: str | None,
) -> dict[str, Any]:
    """Return the whole index definition.

    Args:
        name: The index name. Not the alias — see :func:`index_name`.
        deployment: The embedding deployment, which fixes both the vector
            length and what a query-time vectorization would call.
        vectorizer_key: A Foundry API key for the service to embed queries
            with, or ``None`` to build an index with **no** query-time
            vectorizer. ``None`` is a deliberate choice a caller has to make
            rather than a default that quietly degrades: without it the
            application must embed its own queries, which is the one thing
            integrated vectorization exists to stop it doing.

    Returns:
        A JSON-ready index definition.
    """
    fields = [_field(entry) for entry in chunks.FIELDS]
    fields.append(_vector_field(deployment))
    return {
        "name": name,
        "fields": fields,
        "scoringProfiles": _scoring_profiles(),
        "defaultScoringProfile": SCORING_PROFILE,
        "semantic": _semantic(),
        "vectorSearch": _vector_search(deployment, vectorizer_key),
    }


def index_name(run_id: str, alias: str = ALIAS) -> str:
    """Return the index name for the corpus release ``run_id``.

    The build's whole identity argument is in this function. The corpus is
    already published by a pointer — ``corpus/current.json`` names one
    ``corpus/runs/<run_id>/`` and one write of it moves the whole corpus
    (``docs/corpus-freshness.md``). Naming the index after the same run id makes
    the alias swap and the release swap *the same swap*: given a live alias you
    can read which harvest is being served, and given a harvest you can name the
    index that would serve it, without a lookup table that could be wrong.

    Args:
        run_id: A release run id, ``20260826T195844Z``.
        alias: The alias the index will eventually be swapped under.

    Returns:
        The index name, lower-cased — Azure AI Search names may hold only
        lowercase letters, digits and dashes, and a run id carries a ``T`` and
        a ``Z``.

    Raises:
        ValueError: If ``run_id`` is empty or holds a character that cannot
            appear in an index name.
    """
    if not run_id:
        raise ValueError("an index is named after a corpus release; run_id is empty")
    candidate = f"{alias}-{run_id}".lower()
    if not all(character.isalnum() or character == "-" for character in candidate):
        raise ValueError(
            f"{candidate!r} is not a legal index name: only letters, digits and dashes"
        )
    return candidate


def run_id_of(name: str, alias: str = ALIAS) -> str | None:
    """Return the corpus release an index name was built from, if it says.

    The inverse of :func:`index_name`, and the reason that function is worth
    having: ``build`` retires old indexes in release order, and an index that
    does not name a release is not one of ours to retire.

    Args:
        name: An index name.
        alias: The alias whose indexes are being read.

    Returns:
        The run id in its original case, or ``None`` if ``name`` was not built
        by :func:`index_name`.
    """
    prefix = f"{alias}-"
    if not name.startswith(prefix) or len(name) == len(prefix):
        return None
    return name[len(prefix) :].upper()
