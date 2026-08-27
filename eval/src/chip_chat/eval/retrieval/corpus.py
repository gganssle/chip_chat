"""Resolving a label against a corpus, and what an unresolved one means.

A label names a place. This module is where a place becomes a set of chunk ids,
and it is the reason #50's fourth acceptance criterion -- *the ablation is
repeatable after any chunking change, so a chunking regression is caught
immediately* -- is a mechanism rather than an intention.

**A run resolves before it queries.** Not because the scorer needs the ids to
judge a passage -- it does not, a selector matches a passage's own fields
perfectly well -- but because a recall number computed without knowing what the
corpus contains is a number about the *questions* rather than about the
retriever. If the corpus never held Chipotle's allergen caveat, a retriever that
did not return it did nothing wrong, and a report that scored it as a miss would
be blaming a model for a harvest.

So resolution produces three verdicts rather than two, exactly as
:mod:`chip_chat.eval.golden` does for its checks:

* a label that resolves is **scored** -- it is in a denominator, and whether it
  came back is a fact about the retriever;
* a label that resolves to nothing is **unscored** -- in no numerator and no
  denominator, and printed by name above the rates;
* a question none of whose labels resolve is unscored entirely.

**A chunking regression is the difference between two runs of this.** Nothing
here can tell "the corpus never had it" apart from "the corpus stopped having
it" -- both are an empty set today. What tells them apart is
``eval/retrieval/BASELINE.md``, which records the resolution alongside the
numbers: a label that resolved in the committed baseline and does not resolve
now is a regression, and it is one line of a diff.

**Skew is counted, not assumed away.** The index is built from the corpus, so
every passage it returns should be a chunk the corpus export also holds. Where
one is not -- a passage that satisfies a label but carries an id the export
never mentioned -- the index and the export disagree, which is the same class of
defect as :attr:`chip_chat.search.retrieve.Retrieval.uncitable` and is reported
the same way: as a count, above the numbers it would otherwise quietly move.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from chip_chat.eval.retrieval.questions import Label, Question, RetrievalSet
from chip_chat.search import chunks
from chip_chat.search.client import SearchService
from chip_chat.search.corpus import ChunkSet
from chip_chat.search.retrieve import Passage

__all__ = [
    "PAGE",
    "Place",
    "Resolution",
    "fields_of",
    "from_index",
    "resolve",
]

PAGE: Final = 1000
"""Documents per request when reading a corpus back off an index.

Azure AI Search caps ``top`` at 1,000 per request, so a corpus larger than that
arrives in pages. The published corpus is two orders of magnitude smaller than
one page today; the loop is here because a reader that silently returned the
first thousand chunks would make every label past them look like a chunking
regression.
"""


def fields_of(passage: Passage) -> Mapping[str, Any]:
    """Return one retrieved passage as the fields a label selects on.

    :attr:`~chip_chat.search.retrieve.Passage.published` deliberately omits the
    handful of fields the retrieval layer lifts onto the passage itself, so this
    puts them back -- and it is the only place in this package that knows the
    two shapes are the same shape. Everything downstream compares a chunk row
    and a passage with the same :meth:`~chip_chat.eval.retrieval.questions.Label.matches`
    call, which is what makes "did the index return the place the corpus has"
    a question with one answer rather than two.

    Args:
        passage: A passage the retriever returned.

    Returns:
        Its published fields, keyed as the chunk export keys them.
    """
    return {
        **passage.published,
        chunks.CHUNK_ID: passage.id,
        chunks.KIND: passage.kind,
        chunks.HEADING: passage.heading,
        chunks.TEXT: passage.text,
        chunks.SOURCE_URL: passage.source_url,
        chunks.HARVESTED_AT: passage.harvested_at,
    }


@dataclass(frozen=True, slots=True)
class Place:
    """One label, and the chunks in this corpus that are it.

    Attributes:
        question_id: Which question it belongs to.
        label: The label.
        chunk_ids: The ids satisfying it, sorted. Empty means unresolved.
    """

    question_id: str
    label: Label
    chunk_ids: tuple[str, ...]

    @property
    def resolved(self) -> bool:
        """Whether this label names anything in the corpus under test."""
        return bool(self.chunk_ids)


@dataclass(frozen=True, slots=True)
class Resolution:
    """Every label in the set, against one corpus.

    Attributes:
        places: One per label, in set order.
        run_id: The corpus release resolved against. Recorded because two
            reports taken against two harvests are not comparable, and this is
            the only way to know which is which.
        chunks: How many chunks that release holds.
    """

    places: tuple[Place, ...]
    run_id: str
    chunks: int

    def for_question(self, question: Question) -> tuple[Place, ...]:
        """The places of one question, in label order."""
        return tuple(
            place for place in self.places if place.question_id == question.question_id
        )

    def scored_labels(self, question: Question) -> tuple[Label, ...]:
        """The labels of one question that resolve. The recall denominator."""
        return tuple(
            place.label for place in self.for_question(question) if place.resolved
        )

    def unresolved(self) -> tuple[Place, ...]:
        """Every label that names nothing in this corpus, in set order."""
        return tuple(place for place in self.places if not place.resolved)

    def ids(self) -> frozenset[str]:
        """Every chunk id any label resolved to. What a skew check compares against."""
        return frozenset(
            chunk_id for place in self.places for chunk_id in place.chunk_ids
        )

    def unscorable(self, questions: Sequence[Question]) -> tuple[Question, ...]:
        """Questions with labels, none of which resolve. Scored in no direction.

        Args:
            questions: The questions to check, in the order to report them.

        Returns:
            The ones the corpus under test cannot support. A question with no
            labels at all -- the negative set, and the constraint-only question
            -- is not here: it is scored on something other than recall, and
            resolution has nothing to say about it.
        """
        return tuple(
            question
            for question in questions
            if question.relevant and not self.scored_labels(question)
        )


def resolve(questions: RetrievalSet, corpus: ChunkSet) -> Resolution:
    """Resolve every label in the set against a corpus release.

    Args:
        questions: The labeled set.
        corpus: The chunk export. The same thing
            :func:`chip_chat.search.build.build` indexed, read the same way --
            through :mod:`chip_chat.search.corpus` rather than through a reader
            of this package's own, so that "what the index holds" and "what the
            labels are resolved against" cannot come from two files.

    Returns:
        The :class:`Resolution`. Never raises on an unresolved label: an
        incomplete corpus is a fact to report beside the numbers, not a reason
        to refuse to compute them -- the same call
        :func:`chip_chat.eval.photos.coverage.coverage` makes.
    """
    rows = corpus.rows
    places = tuple(
        Place(
            question_id=question.question_id,
            label=label,
            chunk_ids=tuple(
                sorted(
                    str(row[chunks.CHUNK_ID])
                    for row in rows
                    if chunks.CHUNK_ID in row and label.matches(row)
                )
            ),
        )
        for question, label in questions.labels()
    )
    return Resolution(places=places, run_id=corpus.run_id, chunks=len(rows))


def from_index(service: SearchService, alias: str, run_id: str) -> ChunkSet:
    """Read a corpus back off the index that is serving it.

    The export under the release pointer is the corpus the index was *built
    from*, and that is the right thing to resolve against whenever it is to
    hand. This is for when it is not, and for the one case where it is
    strictly better: a measured sweep.

    The question a resolution answers is *can the retriever return this place*,
    and that is a question about what the **index** holds. Where the two agree
    the answer is the same either way. Where they disagree -- a document the
    builder rejected, a rebuild that landed short -- resolving against the
    export would score the retriever as missing a passage nothing could have
    returned, which is the failure this module exists to prevent, arriving
    through the back door.

    It costs no semantic request: ``search: "*"`` is a simple query, and the
    ranker is never asked for.

    Args:
        service: The search service.
        alias: The alias to read. The application knows the alias and never an
            index name, here as everywhere.
        run_id: What to call this corpus in the report. The live index's name is
            the honest answer, and ``chip_chat.search.status`` prints it.

    Returns:
        The chunks the index holds, in the order it returned them.

    Raises:
        chip_chat.search.errors.SearchError: If the service refuses.
    """
    rows: list[Mapping[str, Any]] = []
    while True:
        response = service.search(alias, {"search": "*", "top": PAGE, "skip": len(rows)})
        page = [
            {name: value for name, value in hit.items() if not name.startswith("@")}
            for hit in response.get("value", [])
        ]
        rows.extend(page)
        if len(page) < PAGE:
            return ChunkSet(
                run_id=run_id, rows=tuple(rows), origin=f"index alias {alias}"
            )
