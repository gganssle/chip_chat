"""An index in memory over a committed corpus, so the sweep runs for nothing.

**Read this before reading a number this produced.** What follows is a fixture
for the harness, not a retriever, and the distinction is the same one
:mod:`chip_chat.eval.photos.testing` draws about its coloured rectangles: it is
right by construction about the things it models and says nothing at all about
the things it does not.

What it models, and models honestly:

* the **lexical half**, as the fraction of the query's content words a chunk
  contains. That is cruder than BM25 -- no term weighting, no analyzer, no
  heading boost -- and it is a real lexical retriever over the real corpus, so a
  keyword arm run against it is a keyword arm doing a keyword arm's job badly
  rather than a stub;
* **reciprocal rank fusion**, at Azure's own smoothing constant, so a hybrid arm
  really is two orders fused and its scores really do sit within a hair of each
  other -- which is the property the confidence rule is written against;
* the **shape of every response**: retrievable fields, ``@search.score``,
  ``@search.rerankerScore`` and captions on a semantic query, and nothing on a
  simple one.

What it does not model, and cannot:

* **the vector half.** The stand-in order is by chunk id, which is deterministic
  and carries no relevance whatsoever. That is a faithful model of what a vector
  index does for a question it has never heard of -- it returns its nearest
  neighbours anyway -- and it is not a model of what one does for a question it
  has. So the *vector only* arm's numbers here are a floor and nothing else, and
  every arm containing a vector half is scored partly against noise;
* **the ``filter``.** It is not evaluated, for the reason
  ``search/tests/fakes.py`` gives about the same omission: an OData evaluator
  here would be a small parser nobody asked for, and it would be marking its own
  homework — what a filter has to be right about is its *text*, which
  ``search/tests/test_query.py`` asserts string for string. So an offline sweep
  cannot say whether a constrained question was answered within its constraint,
  and :attr:`EVALUATES_FILTERS` is how the report knows to print that table
  unscored rather than to print a violation count that is really a count of
  the fixture;
* **the reranker.** Its score here is four times the same lexical fraction, so
  the reranked arm cannot reorder anything the keyword arm did not already know.
  A ``hybrid + reranker`` column produced offline is therefore not evidence that
  reranking helps, and ``eval/retrieval/BASELINE.md`` says so where its numbers
  are printed rather than in a footnote.

So the offline sweep is worth exactly two things, and they are both worth
having. It exercises the whole harness at full size against a real corpus, which
is how the arithmetic gets driven without a credential. And it produces the
**resolution** -- which of the set's labels name a place this corpus actually
holds -- which is #50's chunking-regression check and needs no model at all.

It is not, and does not substitute for, the credentialed run.
"""

import re
import struct
from collections.abc import Mapping, Sequence
from typing import Any, Final

from chip_chat.search import chunks, fusion
from chip_chat.search.client import ServiceError
from chip_chat.search.corpus import ChunkSet
from chip_chat.search.schema import ALIAS

__all__ = ["EVALUATES_FILTERS", "RRF_K", "OfflineIndex"]

EVALUATES_FILTERS: Final = False
"""Whether this index applies a query's ``filter``. It does not; see above.

A constant rather than a comment because
:func:`chip_chat.eval.retrieval.report.build_report` takes it: the constrained
questions are scored on a filter, so a run against something that ignores
filters has to print that table as unscored. A violation count taken here would
be a count of the fixture's omission wearing a safety number's clothes.
"""

RRF_K: Final = fusion.RRF_K
"""Reciprocal rank fusion's smoothing constant, as Azure AI Search uses it.

The real number rather than a convenient one: it is what makes the fused score
of a good query and a hopeless one differ in the third decimal place, which is
the property :mod:`chip_chat.search.retrieve` refuses to threshold and the
reason the degrade path has a lexical floor instead.

Re-exported from :data:`chip_chat.search.fusion.RRF_K` rather than restated, so
that the fixture and the detector cannot come to hold different constants — and
the detector's threshold is a function of this one. The rank it is added to is
**zero-based**, matching the live service; see :func:`_ranked`.
"""

_WORD: Final = re.compile(r"[a-z0-9]+")

_NOT_A_BUILD_TARGET: Final = (
    "chip_chat.eval.retrieval.testing.OfflineIndex answers queries over a chunk "
    "export and cannot be built into. Building it would be a second "
    "implementation of chip_chat.search.build; search/tests builds this same "
    "corpus end to end against a fake that models one."
)


class OfflineIndex:
    """A :class:`~chip_chat.search.client.SearchService` over a chunk export.

    It is a complete :class:`~chip_chat.search.client.SearchService` in the type
    sense and a **query-only** one in every other: the five calls a rebuild makes
    are present and refuse. That is deliberate rather than lazy. A fixture that
    could build an index would be a second implementation of
    :mod:`chip_chat.search.build`, free to disagree with the first about the
    order of operations the four failure properties follow from — and
    ``search/tests`` already builds this same corpus end to end against a fake
    that models those. A sweep queries; anything else here is a refusal with the
    reason attached.
    """

    __slots__ = ("_alias", "_drop_vector", "_rows", "queries")

    def __init__(
        self,
        corpus: ChunkSet,
        *,
        alias: str = ALIAS,
        drop_vector: bool = False,
    ) -> None:
        """Point an index at a corpus release.

        Args:
            corpus: The chunks, read through :mod:`chip_chat.search.corpus` so
                that the fixture and the real build cannot disagree about what
                a corpus is.
            alias: The alias the retriever will ask for.
            drop_vector: Reproduce ``chip-wez``. Every ``vectorQueries`` entry
                is answered as if it had matched nothing, and the request is
                otherwise handled normally and returns HTTP 200 — which is
                exactly what the Free tier does and exactly why nothing
                downstream noticed for three sweeps. The one thing this fixture
                can offer that the live service cannot is doing it *on demand*,
                so the harness's unscored path is covered by a test rather than
                by waiting for a bad afternoon.
        """
        self._rows = tuple(corpus.rows)
        self._alias = alias
        self._drop_vector = drop_vector
        self.queries: list[Mapping[str, Any]] = []
        """Every request body received, so a test can assert on the query itself."""

    @property
    def run_id(self) -> str:
        """Nothing reads this; it is here so a repr in a debugger is useful."""
        return self._alias

    def alias_target(self, alias: str) -> str | None:
        """Return the index the alias points at."""
        return self._alias if alias == self._alias else None

    def index_names(self) -> list[str]:
        """Return the one index this holds."""
        return [self._alias]

    def create_index(self, definition: Mapping[str, Any]) -> None:
        """Refuse. See the class docstring."""
        raise ServiceError(_NOT_A_BUILD_TARGET)

    def delete_index(self, name: str) -> None:
        """Refuse. See the class docstring."""
        raise ServiceError(_NOT_A_BUILD_TARGET)

    def upload(self, index: str, documents: Sequence[Mapping[str, Any]]) -> None:
        """Refuse. See the class docstring."""
        raise ServiceError(_NOT_A_BUILD_TARGET)

    def set_alias(self, alias: str, index: str) -> None:
        """Refuse. See the class docstring."""
        raise ServiceError(_NOT_A_BUILD_TARGET)

    def delete_alias(self, alias: str) -> None:
        """Refuse. See the class docstring."""
        raise ServiceError(_NOT_A_BUILD_TARGET)

    def document_count(self, index: str) -> int:
        """Return how many chunks this index holds."""
        return len(self._rows)

    def search(self, target: str, query: Mapping[str, Any]) -> dict[str, Any]:
        """Answer one search request.

        Args:
            target: The alias.
            query: The request body :func:`chip_chat.search.query.body` built.

        Returns:
            A response in the service's shape.

        Raises:
            chip_chat.search.client.ServiceError: If ``target`` is not this
                index's alias.
        """
        if target != self._alias:
            raise ServiceError(f"no index called {target}")
        self.queries.append(dict(query))

        lexical = "search" in query
        asked_vector = bool(query.get("vectorQueries"))
        vector = asked_vector and not self._drop_vector
        text = str(query.get("search", "") or "")
        if not lexical and asked_vector:
            text = str(query["vectorQueries"][0].get("text", "") or "")
        if asked_vector and not lexical and self._drop_vector:
            # A vector-only query whose vector half was dropped has no ranker
            # left. The service returns `{"value": []}` with HTTP 200 rather
            # than an error, and reproducing the status code is the entire
            # point of the flag -- an exception here would be caught by the
            # sweep's own error path and reported as an outage, which is the
            # one thing this defect is not.
            return {"@odata.count": 0, "value": []}
        semantic = query.get("queryType") == "semantic"
        top = int(query.get("top", 50))
        # `skip` is honoured because `chip_chat.eval.retrieval.corpus.from_index`
        # pages with it, and a fixture that ignored it would return the first
        # page forever -- which is a loop rather than a wrong answer, and so the
        # kind of omission a test finds at three in the morning.
        skip = int(query.get("skip", 0))

        words = _words(text)
        ranked = _ranked(words, self._rows, lexical=lexical, vector=vector)
        window = ranked[skip : skip + top]
        return {
            "@odata.count": len(ranked),
            "value": [_hit(row, score, words, semantic) for score, row in window],
        }


def _single_precision(value: float) -> float:
    """Narrow a score the way the service's own JSON does, and not by rounding.

    Azure AI Search answers in single precision: the live alias sends ``1/60``
    as ``0.01666666753590107``, a float32 widened back to a double. Rounding to
    six places instead sends ``0.016667`` — a number *above* ``1/60`` rather
    than at it, which is enough to make a response with the vector half dropped
    read as one where both rankers contributed. See
    :mod:`chip_chat.search.fusion`.
    """
    return float(struct.unpack("<f", struct.pack("<f", value))[0])


def _words(text: str) -> set[str]:
    """The lower-cased words of ``text`` that are three characters or more."""
    return {word for word in _WORD.findall(text.casefold()) if len(word) >= 3}


def _matched(query_words: set[str], row: Mapping[str, Any]) -> float:
    """The fraction of ``query_words`` present in the searchable fields.

    Over :data:`chip_chat.search.chunks.SEARCHABLE` rather than over the whole
    row, which is the one thing that keeps this a model of *this* index: a
    lexical half that matched on ``allergens`` would score the dairy items
    highest for *"without dairy"*, and the argument for leaving that field out
    of the searchable set is exactly that it must not.
    """
    if not query_words:
        return 0.0
    haystack: set[str] = set()
    for name in chunks.SEARCHABLE:
        haystack |= _words(str(row.get(name, "") or ""))
    return len({word for word in query_words if word in haystack}) / len(query_words)


def _ranked(
    query_words: set[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    lexical: bool,
    vector: bool,
) -> list[tuple[float, Mapping[str, Any]]]:
    """Fuse whichever orders the request asked for. See the module docstring."""
    orders: list[Sequence[Mapping[str, Any]]] = []
    if lexical:
        orders.append(
            sorted(
                rows,
                key=lambda row: (
                    -_matched(query_words, row),
                    str(row.get(chunks.CHUNK_ID, "")),
                ),
            )
        )
    if vector:
        orders.append(sorted(rows, key=lambda row: str(row.get(chunks.CHUNK_ID, ""))))
    if not orders:
        return [(1.0, row) for row in rows]
    scores: dict[str, float] = {}
    for order in orders:
        # Zero-based, which is what the live service does -- measured on
        # 2026-08-27, where a degraded hybrid query returned exactly
        # `1/60, 1/61, 1/62, 1/63, 1/64`. It changes no ordering and therefore
        # no number in a report; what it changes is that this fixture's single-
        # ranker ceiling is now the same value as the real one, so
        # `chip_chat.search.fusion` can be exercised offline against scores that
        # are the shape it will meet in production rather than one step below.
        for rank, row in enumerate(order):
            key = str(row.get(chunks.CHUNK_ID, ""))
            scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
    by_id = {str(row.get(chunks.CHUNK_ID, "")): row for row in rows}
    return sorted(
        ((scores[key], by_id[key]) for key in scores),
        key=lambda pair: (-pair[0], str(pair[1].get(chunks.CHUNK_ID, ""))),
    )


def _hit(
    row: Mapping[str, Any], score: float, query_words: set[str], semantic: bool
) -> dict[str, Any]:
    """One search hit, with the scores the query asked for and nothing else."""
    hit: dict[str, Any] = {**row, "@search.score": _single_precision(score)}
    if semantic:
        hit["@search.rerankerScore"] = round(4.0 * _matched(query_words, row), 3)
        hit["@search.captions"] = [
            {"text": str(row.get(chunks.TEXT, ""))[:120], "highlights": ""}
        ]
    return hit
