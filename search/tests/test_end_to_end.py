"""The committed corpus, built end to end, in CI.

#48's first acceptance criterion is *"index built from the chunk table end to
end, reproducibly"*. ``make search-build`` is the run that does it against the
live service; this is the same path over the same 31 chunks against
``fakes.FakeSearchService``, so a change that breaks the build breaks
``make ci`` rather than surfacing the next time somebody has a credential.

The fixture is real published text. It was rendered from the parsed tables in
``abfss://raw@stchipchat4cy39i/parsed/chipotle/`` on 2026-08-27 — real item
names, real FAQ questions, real ``source_url`` values that resolve — and it is
what the live build and the live verification actually ran against.
"""

from pathlib import Path

from fakes import FakeEmbedder, FakeSearchService

from chip_chat.search import chunks, schema
from chip_chat.search.build import BuildReport, build
from chip_chat.search.corpus import from_path
from chip_chat.search.embedding import EmbeddingDeployment

FIXTURE = Path(__file__).parent / "fixtures" / "chunks.jsonl"
RUN_ID = "20260827T053000Z"
DEPLOYMENT = EmbeddingDeployment(
    endpoint="https://aif-example.cognitiveservices.azure.com/", dimensions=8
)


def built() -> tuple[FakeSearchService, BuildReport]:
    service = FakeSearchService()
    report = build(
        service,
        from_path(FIXTURE, RUN_ID),
        DEPLOYMENT,
        FakeEmbedder(DEPLOYMENT),
        vectorizer_key="a-key",
        settle=0.0,
    )
    return service, report


def test_the_whole_corpus_reaches_the_index_under_the_alias() -> None:
    service, report = built()
    assert report.documents == 31
    assert service.aliases[schema.ALIAS] == report.index


def test_every_document_carries_a_resolvable_source_and_a_clock() -> None:
    # #48's second acceptance criterion, over the real corpus rather than over
    # a chunk written to pass it.
    service, report = built()
    documents = service.docs[report.index].values()
    assert documents
    for document in documents:
        assert str(document[chunks.SOURCE_URL]).startswith("https://")
        assert str(document[chunks.HARVESTED_AT]).endswith("+00:00")


def test_every_document_carries_a_vector_of_the_declared_length() -> None:
    service, report = built()
    for document in service.docs[report.index].values():
        assert len(document[schema.VECTOR_FIELD]) == DEPLOYMENT.dimensions


def test_the_menu_chunks_carry_what_the_comparative_questions_filter_on() -> None:
    service, report = built()
    menu = [
        document
        for document in service.docs[report.index].values()
        if document[chunks.KIND] == "MENU_ITEM"
    ]
    assert menu
    for document in menu:
        assert chunks.ALLERGENS in document
        assert chunks.ALLERGEN_DISCLOSURE in document


def test_a_rebuild_of_the_same_corpus_indexes_the_same_documents() -> None:
    # Reproducibly, in the issue's word: same chunks in, same keys out, and the
    # keys are the ones a two-turn-old conversation is still citing.
    first_service, first = built()
    second_service, second = built()
    assert set(first_service.docs[first.index]) == set(second_service.docs[second.index])
