"""Resolving the shipped labels against the committed corpus.

This is #50's fourth acceptance criterion under test: the labels name places
rather than chunk ids, so a re-chunk moves every id in the corpus and every
label still resolves. The last test in this file is that property, driven by
re-chunking the fixture.
"""

import hashlib
from dataclasses import replace

import pytest

from chip_chat.eval.retrieval import corpus as corpus_module
from chip_chat.eval.retrieval.corpus import fields_of, from_index, resolve
from chip_chat.eval.retrieval.questions import Label, RetrievalSet
from chip_chat.eval.retrieval.testing import OfflineIndex
from chip_chat.search.corpus import ChunkSet
from chip_chat.search.retrieve import Passage
from chip_chat.search.schema import ALIAS


def rechunked(corpus: ChunkSet) -> ChunkSet:
    """The same corpus with every chunk id changed, as a re-chunk would.

    The text is left alone and the ids are re-derived from a different salt,
    which is exactly what a chunking change does to a corpus whose sources have
    not moved: the same published sentences, sliced by a pipeline that hashes
    them differently.
    """
    return replace(
        corpus,
        rows=tuple(
            {
                **row,
                "chunk_id": hashlib.sha256(f"v2:{row['chunk_id']}".encode()).hexdigest(),
            }
            for row in corpus.rows
        ),
    )


def test_the_committed_corpus_resolves_all_but_the_two_it_does_not_hold(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    # Named rather than counted. The fixture is a slice of the published pages,
    # so two labels naming nothing in it is the expected state -- and a THIRD
    # one appearing is the regression this test exists to catch.
    unresolved = {
        place.question_id
        for place in resolve(retrieval_questions, corpus_fixture).unresolved()
    }
    assert unresolved == {"ing-barbacoa", "alg-caveat"}


def test_an_unresolved_label_is_unscored_rather_than_missing(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    # The third verdict. A question whose only label names nothing has an empty
    # denominator, which the scorer reads as unscored -- never as a miss.
    resolution = resolve(retrieval_questions, corpus_fixture)
    caveat = next(q for q in retrieval_questions if q.question_id == "alg-caveat")
    assert caveat.relevant
    assert resolution.scored_labels(caveat) == ()
    assert caveat in resolution.unscorable(retrieval_questions.questions)


def test_a_negative_question_is_not_unscorable(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    # It carries no labels by construction, so resolution has nothing to say
    # about it -- and it is scored on restraint rather than on recall.
    resolution = resolve(retrieval_questions, corpus_fixture)
    unscorable = {
        q.question_id for q in resolution.unscorable(retrieval_questions.questions)
    }
    assert "neg-halal" not in unscorable


def test_every_resolved_label_names_at_least_one_chunk(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    resolution = resolve(retrieval_questions, corpus_fixture)
    for place in resolution.places:
        if place.resolved:
            assert place.chunk_ids


def test_a_passage_and_a_chunk_are_matched_by_the_same_selector(
    corpus_fixture: ChunkSet,
) -> None:
    # `fields_of` is the only place in the package that knows the two shapes are
    # the same shape. If it drifted, a label could resolve against the corpus
    # and never match the passage the index returned for it -- which would look
    # exactly like a retriever that never found anything.
    row = next(r for r in corpus_fixture.rows if r.get("item_id") == "CMG-5252")
    label = Label(kind="MENU_ITEM", fields={"item_id": "CMG-5252"}, why="x")
    passage = Passage(
        id=str(row["chunk_id"]),
        text=str(row["text"]),
        heading=str(row["heading"]),
        kind=str(row["kind"]),
        source_url=str(row["source_url"]),
        harvested_at=str(row["harvested_at"]),
        score=1.0,
        published={"item_id": row["item_id"]},
    )
    assert label.matches(row)
    assert label.matches(fields_of(passage))


def test_the_labels_survive_a_rechunk(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    # The whole design, as one assertion. Every chunk id in the corpus changes;
    # the resolution does not. A set keyed on chunk ids would resolve nothing
    # here, which is the failure mode #50's fourth acceptance criterion is
    # about -- and it would be indistinguishable from a corpus that lost every
    # passage.
    before = resolve(retrieval_questions, corpus_fixture)
    after = resolve(retrieval_questions, rechunked(corpus_fixture))
    assert before.ids().isdisjoint(after.ids())
    assert [place.resolved for place in before.places] == [
        place.resolved for place in after.places
    ]
    assert [len(place.chunk_ids) for place in before.places] == [
        len(place.chunk_ids) for place in after.places
    ]


def test_a_place_that_leaves_the_corpus_stops_resolving(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    # The other half: the set detects a real regression rather than merely
    # surviving a harmless one. Drop the cheese row -- the one chunk carrying a
    # published dairy mark -- and the allergen question anchored to it goes
    # unresolved and is named.
    without = replace(
        corpus_fixture,
        rows=tuple(r for r in corpus_fixture.rows if r.get("item_id") != "CMG-5252"),
    )
    unresolved = {
        place.question_id for place in resolve(retrieval_questions, without).unresolved()
    }
    assert "alg-cheese-dairy" in unresolved


# --- Reading a corpus back off the index that serves it ----------------------


def test_the_index_can_hand_back_the_corpus_it_is_serving(
    corpus_fixture: ChunkSet,
) -> None:
    # What a measured sweep resolves against. `search: "*"` is a simple query,
    # so it spends no semantic request.
    index = OfflineIndex(corpus_fixture)
    read_back = from_index(index, ALIAS, "live-index")
    assert {str(row["chunk_id"]) for row in read_back.rows} == {
        str(row["chunk_id"]) for row in corpus_fixture.rows
    }
    assert read_back.run_id == "live-index"


def test_reading_it_back_resolves_the_same_labels(
    retrieval_questions: RetrievalSet, corpus_fixture: ChunkSet
) -> None:
    # Where the export and the index agree, `--from-index` changes nothing. It
    # earns its place only where they disagree, and it must not move anything
    # where they do not.
    index = OfflineIndex(corpus_fixture)
    export = resolve(retrieval_questions, corpus_fixture)
    live = resolve(retrieval_questions, from_index(index, ALIAS, "live"))
    assert [p.chunk_ids for p in export.places] == [p.chunk_ids for p in live.places]


def test_a_corpus_larger_than_one_page_is_read_whole(
    corpus_fixture: ChunkSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reader that silently returned the first page would make every label past
    # it look like a chunking regression. The published corpus is two orders of
    # magnitude under one page today, which is exactly why this needs a test
    # rather than a run.
    monkeypatch.setattr(corpus_module, "PAGE", 7)
    read_back = from_index(OfflineIndex(corpus_fixture), ALIAS, "live")
    assert len(read_back.rows) == len(corpus_fixture.rows)


def test_the_service_fields_do_not_travel_into_the_corpus(
    corpus_fixture: ChunkSet,
) -> None:
    # `@search.score` and its neighbours are properties of a query, not of a
    # chunk, and a label that could select on one would be a label about a rank.
    read_back = from_index(OfflineIndex(corpus_fixture), ALIAS, "live")
    for row in read_back.rows:
        assert not [name for name in row if name.startswith("@")]
