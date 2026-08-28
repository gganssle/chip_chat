"""A search service and an embedding deployment, in memory.

The fake is not a mock. It keeps indexes, documents and aliases in dicts and
enforces the four service behaviours the build depends on, so that a test which
passes against it is saying something about the build rather than about a
recording:

* an index name must be free before it can be created, and an index that does
  not exist cannot be written to;
* a document whose key is empty fails the **whole request** with an
  ``InvalidName`` error, and the documents beside it in that request do not
  land. That is not what the first draft of this fake did and it is not what the
  obvious reading of the API suggests -- it was measured on 2026-08-27, when
  ``make search-verify`` returned
  ``400 ... actions : 30: Document key cannot be missing or empty``. Documents
  in *earlier* requests are already in, which is how a build fails halfway;
* other rejections are reported per document, with HTTP 207 and a status per
  key, which is what :class:`~chip_chat.search.client.UploadError` carries.
  ``reject`` models that half;
* an alias write replaces the whole target list in one call and never leaves the
  alias unset;
* an alias may not point at an index that does not exist.

Everything the fake refuses, the real service refuses — ``make search-verify``
is the run that says so. What the fake deliberately does *not* model is the
propagation delay after an alias write, because a model of a delay would only
ever confirm the number that was typed into it.

**Querying** was added for #49, and it models the two properties the retrieval
layer's behaviour actually turns on. Results are fused from *two* orders — a
lexical one and a stand-in for the vector half — with reciprocal rank fusion, so
that (a) something comes back for every query, including one the corpus cannot
answer, which is the near-miss the confidence rule exists to catch, and (b) the
fused ``@search.score`` is a rank score with almost no spread, which is why
:mod:`chip_chat.search.retrieve` refuses to threshold it. A semantic query
additionally carries ``@search.rerankerScore`` on 0-4 and an extractive caption.

It models the *shape* of the request in one more respect, for #50: a body with
no ``search`` key ranks by the vector order alone and a body with no
``vectorQueries`` ranks by the lexical order alone, so the ablation's four arms
are four different orders here rather than four labels on one. What it cannot
model is which of them is *better*, because the vector order is a stand-in --
see :func:`_ranked`, and see ``eval/retrieval/BASELINE.md``, which says so where
its numbers are.

It does **not** evaluate ``filter``. An OData evaluator here would be a small
parser nobody asked for, and it would be marking its own homework: what the
filter has to be right about is its *text*, which ``test_query.py`` asserts
directly, string for string.
"""

import re
import struct
from collections.abc import Mapping, Sequence
from typing import Any

from chip_chat.search import chunks as chunk_schema
from chip_chat.search.client import ServiceError, UploadError
from chip_chat.search.embedding import EmbeddingDeployment

__all__ = ["FakeEmbedder", "FakeSearchService", "chunk", "chunk_set"]

RRF_K = 60
"""Reciprocal rank fusion's smoothing constant, as Azure AI Search uses it.

Worth having the real number here rather than a made-up one: it is what makes
the fused scores of a good query and a hopeless query differ in the third
decimal place, which is the property the retrieval layer is written against."""

_WORD = re.compile(r"[a-z0-9]+")


def _single_precision(value: float) -> float:
    """Narrow a score the way the service's own JSON does, and not by rounding.

    Azure AI Search answers in single precision: the live alias sends ``1/60``
    as ``0.01666666753590107``, a float32 widened back to a double. This fake
    used to round to six places instead, which sends ``0.016667`` — a number
    *above* ``1/60`` rather than at it, so a response with the vector half
    dropped read as one where two rankers contributed.

    That is worth the four lines. The fake's tidiness would have hidden exactly
    the defect :mod:`chip_chat.search.fusion` exists to find, in the test suite
    written to prove it is found.
    """
    return float(struct.unpack("<f", struct.pack("<f", value))[0])


def _words(text: str) -> set[str]:
    """Return the lower-cased words of ``text`` that are three characters or more."""
    return {word for word in _WORD.findall(text.casefold()) if len(word) >= 3}


def _matched(query_words: set[str], document: Mapping[str, Any]) -> float:
    """Return the fraction of ``query_words`` present in the searchable fields."""
    if not query_words:
        return 0.0
    haystack: set[str] = set()
    for name in chunk_schema.SEARCHABLE:
        haystack |= _words(str(document.get(name, "") or ""))
    return len({word for word in query_words if word in haystack}) / len(query_words)


class FakeSearchService:
    """An in-memory :class:`~chip_chat.search.client.SearchService`."""

    def __init__(self, batch: int = 1000, reject: set[str] | None = None) -> None:
        """Initialise the service.

        Args:
            batch: Documents per upload request, so a test can produce a load
                that fails after some of the corpus is already in.
            reject: Keys the service reports as failed **per document**, which
                is the 207 half of its behaviour.
        """
        self.indexes: dict[str, Mapping[str, Any]] = {}
        self.docs: dict[str, dict[str, Mapping[str, Any]]] = {}
        self.aliases: dict[str, str] = {}
        self.calls: list[str] = []
        self.batch = max(1, batch)
        self.reject = reject or set()
        self.queries: list[Mapping[str, Any]] = []
        """Every search body received, so a test can assert on the query itself."""
        self.semantic_refusal: str | None = None
        """Set to make semantic queries fail, as a spent monthly allowance does."""
        self.drop_vector = False
        """Set to reproduce ``chip-wez``: the vector half of every query is
        answered as though it had matched nothing, and the request otherwise
        succeeds. Not an exception and not an error key — the whole difficulty
        of that defect is that the response is a well-formed 200, so a fake that
        raised would be modelling an outage instead."""

    def index_names(self) -> list[str]:
        self.calls.append("index_names")
        return sorted(self.indexes)

    def create_index(self, definition: Mapping[str, Any]) -> None:
        name = str(definition["name"])
        self.calls.append(f"create_index:{name}")
        if name in self.indexes:
            raise ServiceError(f"index {name} already exists")
        self.indexes[name] = definition
        self.docs[name] = {}

    def delete_index(self, name: str) -> None:
        self.calls.append(f"delete_index:{name}")
        self.indexes.pop(name, None)
        self.docs.pop(name, None)

    def upload(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        self.calls.append(f"upload:{index}:{len(documents)}")
        if index not in self.indexes:
            raise ServiceError(f"no index called {index}")
        for start in range(0, len(documents), self.batch):
            request = documents[start : start + self.batch]
            for position, document in enumerate(request):
                if not str(document.get("chunk_id", "")):
                    raise ServiceError(
                        f"POST /indexes/{index}/docs/index returned 400: "
                        f"actions : {start + position}: Document key cannot be "
                        f"missing or empty."
                    )
            failures = [
                (str(document["chunk_id"]), "the service declined this document")
                for document in request
                if str(document["chunk_id"]) in self.reject
            ]
            for document in request:
                if str(document["chunk_id"]) not in self.reject:
                    self.docs[index][str(document["chunk_id"])] = document
            if failures:
                raise UploadError(failures)

    def document_count(self, index: str) -> int:
        self.calls.append(f"document_count:{index}")
        target = self.aliases.get(index, index)
        if target not in self.docs:
            raise ServiceError(f"no index called {target}")
        return len(self.docs[target])

    def alias_target(self, alias: str) -> str | None:
        self.calls.append(f"alias_target:{alias}")
        return self.aliases.get(alias)

    def set_alias(self, alias: str, index: str) -> None:
        self.calls.append(f"set_alias:{alias}:{index}")
        if index not in self.indexes:
            raise ServiceError(f"cannot point {alias} at {index}: it does not exist")
        self.aliases[alias] = index

    def delete_alias(self, alias: str) -> None:
        self.calls.append(f"delete_alias:{alias}")
        self.aliases.pop(alias, None)

    def search(self, target: str, query: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append(f"search:{target}")
        self.queries.append(dict(query))
        index = self.aliases.get(target, target)
        if index not in self.docs:
            raise ServiceError(f"no index called {index}")
        semantic = query.get("queryType") == "semantic"
        if semantic and self.semantic_refusal is not None:
            raise ServiceError(self.semantic_refusal)
        documents = list(self.docs[index].values())
        lexical = "search" in query
        asked_vector = bool(query.get("vectorQueries"))
        vector = asked_vector and not self.drop_vector
        text = str(query.get("search", "*") or "*")
        if not lexical and asked_vector:
            # #50's vector-only ablation. The request carries no `search` key at
            # all, and the query text the vector half is embedding is the one on
            # the `vectorQueries` entry -- which is the same string, but reading
            # it from there is what makes this arm really run without a lexical
            # half rather than with one nobody noticed.
            text = str(query["vectorQueries"][0].get("text", "*") or "*")
        top = int(query.get("top", 50))
        if asked_vector and not lexical and self.drop_vector:
            # A vector-only query with no vector half has no ranker left, and
            # the service reports that as `{"value": []}` with a 200.
            return {"@odata.count": 0, "value": []}
        if text == "*":
            ranked = [(1.0, document) for document in documents]
        else:
            ranked = _ranked(_words(text), documents, lexical=lexical, vector=vector)
        return {
            "@odata.count": len(ranked),
            "value": [
                _hit(document, score, _words(text) if text != "*" else set(), semantic)
                for score, document in ranked[:top]
            ],
        }


def _ranked(
    query_words: set[str],
    documents: Sequence[Mapping[str, Any]],
    *,
    lexical: bool,
    vector: bool,
) -> list[tuple[float, Mapping[str, Any]]]:
    """Return ``(score, document)`` by reciprocal rank fusion of the orders sent.

    The lexical order is by how much of the query a document contains. The
    stand-in for the vector order is by chunk id, which is deterministic and
    carries no relevance at all — which is the honest model of what the vector
    half does for a query about something the corpus has never heard of: it
    returns its nearest neighbours anyway.

    **Fusing one order is still fusion**, and that is why the two single-half
    arms of #50's ablation go through the same arithmetic rather than a branch:
    reciprocal rank fusion over one order is ``1 / (k + rank)``, which preserves
    that order exactly and keeps the score on the same scale as the hybrid arm's.
    Two arms whose scores were on different scales would look like a finding.

    The rank is **zero-based**, which is what the live service does — measured
    on 2026-08-27, where a hybrid query whose vector half had been dropped
    returned exactly ``1/60, 1/61, 1/62, 1/63, 1/64``. It changes no ordering
    and therefore no assertion about ranking; what it changes is that a
    single-half response from this fake lands on the same ceiling
    :data:`chip_chat.search.fusion.SINGLE_RANKER_CEILING` tests against, so the
    detector can be exercised here against scores of the shape it will actually
    meet.
    """
    orders: list[Sequence[Mapping[str, Any]]] = []
    if lexical:
        orders.append(
            sorted(
                documents,
                key=lambda document: (
                    -_matched(query_words, document),
                    str(document["chunk_id"]),
                ),
            )
        )
    if vector:
        orders.append(sorted(documents, key=lambda document: str(document["chunk_id"])))
    scores: dict[str, float] = {}
    for order in orders:
        for rank, document in enumerate(order):
            key = str(document["chunk_id"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    by_id = {str(document["chunk_id"]): document for document in documents}
    return sorted(
        ((scores[key], by_id[key]) for key in scores),
        key=lambda pair: (-pair[0], str(pair[1]["chunk_id"])),
    )


def _hit(
    document: Mapping[str, Any],
    score: float,
    query_words: set[str],
    semantic: bool,
) -> dict[str, Any]:
    """Return one search hit, with the scores the query asked for."""
    hit: dict[str, Any] = {**document, "@search.score": _single_precision(score)}
    if semantic:
        # The reranker's 0-4 scale, driven by how much of the query the document
        # actually contains -- so a test can produce a confident hit and a
        # near-miss without hard-coding either number.
        hit["@search.rerankerScore"] = round(4.0 * _matched(query_words, document), 3)
        hit["@search.captions"] = [
            {"text": str(document.get("text", ""))[:120], "highlights": ""}
        ]
    return hit


class FakeEmbedder:
    """Returns a deterministic vector per text, of the declared length.

    Deterministic because a test that asserts a vector reached the right
    document needs to be able to say which vector it was, and because a random
    one would make a failure unreproducible.
    """

    def __init__(self, deployment: EmbeddingDeployment) -> None:
        self._dimensions = deployment.dimensions
        self.batches: list[int] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(len(texts))
        return [
            [float(len(text) % 7) + position / 1000.0] * self._dimensions
            for position, text in enumerate(texts)
        ]


def chunk(chunk_id: str, **overrides: Any) -> dict[str, Any]:
    """Return one plausible ``MENU_ITEM`` chunk row.

    Args:
        chunk_id: The chunk's identity.
        **overrides: Fields to replace or, with ``None``, to leave unset.

    Returns:
        A chunk row, as the gold export writes one.
    """
    row: dict[str, Any] = {
        "chunk_id": chunk_id,
        "kind": "MENU_ITEM",
        "text": f"Chicken Bowl {chunk_id}. 630 calories. Contains dairy.",
        "heading": "Chicken Bowl",
        "item_id": "CMG-101",
        "category": "Entree",
        "item_type": "Bowl",
        "primary_filling": "Chicken",
        "allergens": ["DAIRY"],
        "allergen_disclosure": "PUBLISHED",
        "calories": "630.00",
        "is_composed": True,
        "source_url": "https://www.chipotle.com/nutrition-calculator",
        "harvested_at": "2026-08-26T19:58:46+00:00",
        "character_count": 52,
        "chunked_at": "2026-08-26T21:00:00+00:00",
    }
    row.update(overrides)
    return {name: value for name, value in row.items() if value is not None}


def chunk_set(count: int, run_id: str = "20260826T195844Z") -> Any:
    """Return a :class:`~chip_chat.search.corpus.ChunkSet` of ``count`` chunks."""
    from chip_chat.search.corpus import ChunkSet

    return ChunkSet(
        run_id=run_id,
        rows=tuple(chunk(f"{position:064x}") for position in range(count)),
        origin="fake",
    )
