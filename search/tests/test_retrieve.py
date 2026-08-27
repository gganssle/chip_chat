"""Hybrid retrieval over the committed corpus, in CI.

The same 31 chunks the live build indexed, built end to end into
``fakes.FakeSearchService`` and then asked questions. What the fake models is
the pair of properties the retrieval layer is written against: something comes
back for *every* query, because the vector half always has neighbours, and the
fused score barely moves between a good query and a hopeless one. Those two
together are the near-miss RFC-001 §08 warns about, and they are what
:class:`~chip_chat.search.retrieve.Confidence` exists to separate.
"""

from pathlib import Path
from typing import Any

import pytest
from fakes import FakeEmbedder, FakeSearchService

from chip_chat.search import chunks, schema
from chip_chat.search.allowance import InMemoryAllowanceStore, SemanticAllowance
from chip_chat.search.build import build
from chip_chat.search.client import ServiceError
from chip_chat.search.corpus import from_path
from chip_chat.search.embedding import EmbeddingDeployment
from chip_chat.search.retrieve import (
    MEASURED_GOOD_RERANKER_SCORE,
    PROVISIONAL_RERANKER_FLOOR,
    Confidence,
    Retriever,
    is_semantic_refusal,
)

FIXTURE = Path(__file__).parent / "fixtures" / "chunks.jsonl"
RUN_ID = "20260827T053000Z"
DEPLOYMENT = EmbeddingDeployment(
    endpoint="https://aif-example.cognitiveservices.azure.com/", dimensions=8
)

ANSWERABLE = "how do rewards points work"
"""A question the corpus really answers -- there are eleven FAQ entries in it."""

UNANSWERABLE = "do you sell sushi"
"""A question it does not. Every chunk is a burrito shop's published pages, so
the vector half still returns its nearest neighbours and none of them is an
answer -- which is the case #49 asks to be legible at the tool boundary."""


def service() -> FakeSearchService:
    """Return a service holding the committed corpus under the alias."""
    fake = FakeSearchService()
    build(
        fake,
        from_path(FIXTURE, RUN_ID),
        DEPLOYMENT,
        FakeEmbedder(DEPLOYMENT),
        vectorizer_key="a-key",
        settle=0.0,
    )
    return fake


def retriever(fake: FakeSearchService | None = None, **kwargs: Any) -> Retriever:
    """Return a retriever over ``fake``, with a fresh month's allowance."""
    return Retriever(service() if fake is None else fake, **kwargs)


# --- The floor ---------------------------------------------------------------


def test_the_floor_sits_below_the_only_good_hit_anybody_has_measured() -> None:
    # A floor above it would reject the one real answer on record, and would do
    # so silently: the lane would answer "the published data does not cover it"
    # about a passage that covers it. The number is provisional until #50
    # measures one; this is what keeps a placeholder from doing harm.
    assert PROVISIONAL_RERANKER_FLOOR < MEASURED_GOOD_RERANKER_SCORE


# --- Citations ---------------------------------------------------------------


def test_every_returned_passage_carries_a_resolvable_source_and_a_clock() -> None:
    # #49's second acceptance criterion, and PRD K2's zero target beneath it.
    result = retriever().search(ANSWERABLE)
    assert result.passages
    for passage in result.passages:
        assert passage.source_url.startswith("https://")
        assert passage.harvested_at.endswith("+00:00")


def test_citations_are_keyed_by_the_id_the_envelope_will_reference() -> None:
    # D9: the model names ids, the renderer resolves them here, and an id that
    # was not retrieved has nothing to resolve against.
    result = retriever().search(ANSWERABLE)
    citations = result.citations()
    assert set(citations) == {passage.id for passage in result.passages}
    for chunk_id, citation in citations.items():
        assert citation["id"] == chunk_id
        assert set(citation) == {"id", "label", "source_url", "harvested_at"}


def test_a_label_says_which_kind_of_page_a_passage_came_from() -> None:
    result = retriever().search("what is in a steak burrito")
    labels = [passage.label for passage in result.passages]
    assert any(label.startswith(("Menu · ", "FAQ · ", "Policy")) for label in labels)


def test_a_passage_that_cannot_be_cited_is_not_returned() -> None:
    # documents.py refuses to index a chunk without a source, so a hit that
    # arrives without one means the live index and the build disagree. Passing
    # it on uncited is the one thing that must not happen.
    fake = service()
    index = fake.aliases[schema.ALIAS]
    victim = next(iter(fake.docs[index]))
    fake.docs[index][victim] = {
        name: value
        for name, value in fake.docs[index][victim].items()
        if name != chunks.SOURCE_URL
    }

    result = retriever(fake, top=len(fake.docs[index])).search(ANSWERABLE)
    assert result.uncitable == 1
    assert victim not in {passage.id for passage in result.passages}
    assert any("without a source" in note for note in result.notes)


def test_the_model_is_not_handed_a_url_it_could_paste_into_prose() -> None:
    # D9's mechanism is that the model names ids and the app draws the
    # citation. The URL reaches the visitor through `citations()`, where it can
    # be checked against what was retrieved.
    result = retriever().search(ANSWERABLE)
    payload = result.as_tool_result()
    for entry in payload["passages"]:
        assert "source_url" not in entry
        assert "source_url" not in entry["published"]
        assert "citations" not in entry["published"]
    assert all(
        citation["source_url"].startswith("https://")
        for citation in result.citations().values()
    )


# --- Scores ------------------------------------------------------------------


def test_every_score_that_ranked_a_passage_travels_with_it() -> None:
    # #49: retrieval debugging without scores is guesswork -- and #50 has to be
    # able to move the floor over recorded runs rather than by re-querying.
    result = retriever().search(ANSWERABLE)
    for passage in result.passages:
        assert passage.score > 0.0
        assert passage.reranker_score is not None
        assert 0.0 <= passage.overlap <= 1.0


def test_passages_come_back_best_first() -> None:
    result = retriever().search(ANSWERABLE)
    scores = [passage.ranking_score for passage in result.passages]
    assert scores == sorted(scores, reverse=True)


def test_the_ranking_score_is_the_reranker_s_when_there_is_one() -> None:
    reranked = retriever().search(ANSWERABLE)
    assert reranked.passages[0].ranking_score == reranked.passages[0].reranker_score

    degraded = retriever().search(ANSWERABLE, rerank=False)
    assert degraded.passages[0].ranking_score == degraded.passages[0].score


# --- Confidence --------------------------------------------------------------


def test_a_question_the_corpus_answers_is_grounded() -> None:
    result = retriever().search(ANSWERABLE)
    assert result.confidence is Confidence.GROUNDED
    assert result.grounded
    assert result.notes == ()


def test_a_question_the_corpus_cannot_answer_is_low_confidence() -> None:
    # Passages still come back -- the vector half always has neighbours -- and
    # that is precisely why the confidence has to say so. "The nearest thing in
    # the corpus" is not an answer.
    result = retriever().search(UNANSWERABLE)
    assert result.passages
    assert result.confidence is Confidence.LOW
    assert not result.grounded
    assert any("does not cover it" in note for note in result.notes)


def test_a_near_miss_is_low_confidence_on_the_degrade_path_too() -> None:
    # The fused score cannot make this distinction: reciprocal rank fusion
    # scores rank, so the top hit of a hopeless query scores almost exactly
    # what the top hit of a perfect one does. The lexical floor can.
    result = retriever().search(UNANSWERABLE, rerank=False)
    assert result.passages
    assert result.passages[0].overlap == 0.0
    assert result.confidence is Confidence.LOW


def test_the_fused_score_would_not_have_separated_them() -> None:
    # The claim the module docstring makes, asserted rather than argued.
    good = retriever().search(ANSWERABLE, rerank=False).passages[0].score
    bad = retriever().search(UNANSWERABLE, rerank=False).passages[0].score
    assert abs(good - bad) < 0.01


def test_an_empty_corpus_is_reported_as_nothing_rather_than_as_a_low_score() -> None:
    fake = FakeSearchService()
    fake.create_index({"name": "corpus-empty"})
    fake.set_alias(schema.ALIAS, "corpus-empty")
    result = retriever(fake).search(ANSWERABLE)
    assert result.passages == ()
    assert result.confidence is Confidence.NONE
    assert any("Nothing in the published corpus" in note for note in result.notes)


def test_an_empty_filtered_answer_is_a_finding_rather_than_a_gap() -> None:
    fake = FakeSearchService()
    fake.create_index({"name": "corpus-empty"})
    fake.set_alias(schema.ALIAS, "corpus-empty")
    result = retriever(fake).search("anything under 100 calories")
    assert result.constraints.filtered
    assert any("with the filter applied" in note for note in result.notes)


def test_the_floor_a_result_was_judged_against_travels_with_it() -> None:
    assert retriever(floor=3.9).search(ANSWERABLE).floor == 3.9
    assert retriever(floor=3.9).search(ANSWERABLE).confidence is Confidence.LOW


# --- Constraints -------------------------------------------------------------


def test_a_constrained_question_reaches_the_service_as_a_filter() -> None:
    fake = service()
    retriever(fake).search("a bowl under 500 calories with no dairy")
    assert fake.queries[-1]["filter"] == (
        "calories lt 500 "
        "and allergen_disclosure eq 'PUBLISHED' "
        "and not allergens/any(a: a eq 'dair')"
    )


def test_a_constraint_the_index_cannot_express_is_reported_to_the_agent() -> None:
    # The passages are unfiltered, so the agent has to be told that -- or it
    # reads an unfiltered answer as a filtered one and presents a burrito as
    # vegetarian because retrieval returned it.
    fake = service()
    result = retriever(fake).search("what is vegetarian")
    assert "filter" not in fake.queries[-1]
    assert any("NOT filtered" in note for note in result.notes)


def test_constraints_may_be_supplied_instead_of_read() -> None:
    # The eval harness wants the query held fixed while the filter varies.
    from chip_chat.search.query import Bound, Constraints

    fake = service()
    retriever(fake).search(
        "anything at all", constraints=Constraints(max_calories=Bound(300.0, True))
    )
    assert fake.queries[-1]["filter"] == "calories le 300"


# --- The degrade path --------------------------------------------------------


def test_a_reranked_query_spends_one_semantic_request() -> None:
    allowance = SemanticAllowance(limit=10)
    fake = service()
    retriever(fake, allowance=allowance).search(ANSWERABLE)
    assert allowance.report().spent == 1
    assert fake.queries[-1]["queryType"] == "semantic"


def test_a_query_that_asks_for_no_reranking_spends_nothing() -> None:
    allowance = SemanticAllowance(limit=10)
    fake = service()
    retriever(fake, allowance=allowance).search(ANSWERABLE, rerank=False)
    assert allowance.report().spent == 0
    assert fake.queries[-1]["queryType"] == "simple"


def test_a_spent_allowance_degrades_rather_than_declining() -> None:
    # RFC-001 section 10 sanctions declining when AI Search is unavailable. A
    # spent reranker allowance is not that, and weaker ranking is far better
    # than no answer.
    allowance = SemanticAllowance(limit=1000, store=InMemoryAllowanceStore("", 0))
    allowance.exhaust("spent earlier this month")
    fake = service()
    result = retriever(fake, allowance=allowance).search(ANSWERABLE)

    assert result.answered
    assert result.passages
    assert not result.reranked
    assert fake.queries[-1]["queryType"] == "simple"
    assert any("Reranking is off" in note for note in result.notes)


def test_a_billing_refusal_mid_query_degrades_and_stops_asking() -> None:
    # The service is the authority on its own allowance. When it refuses while
    # the counter still says there is room, the rest of the month is hybrid.
    fake = service()
    fake.semantic_refusal = (
        "POST /indexes/corpus/docs/search returned 403: Semantic search is not "
        "available: the free semantic ranking quota for this subscription has "
        "been exceeded."
    )
    allowance = SemanticAllowance(limit=1000)
    result = retriever(fake, allowance=allowance).search(ANSWERABLE)

    assert result.answered
    assert result.passages
    assert not result.reranked
    assert allowance.report().exhausted
    assert [query["queryType"] for query in fake.queries[-2:]] == ["semantic", "simple"]

    # And the next turn does not spend a request finding out again.
    fake.queries.clear()
    retriever(fake, allowance=allowance).search(ANSWERABLE)
    assert [query["queryType"] for query in fake.queries] == ["simple"]


def test_an_outage_is_not_mistaken_for_a_spent_allowance() -> None:
    fake = service()
    fake.semantic_refusal = "POST /indexes/corpus/docs/search returned 503: unavailable"
    with pytest.raises(ServiceError):
        retriever(fake).search(ANSWERABLE)


@pytest.mark.parametrize(
    "message",
    [
        "403: Semantic search quota exceeded for this service.",
        "Semantic ranking is not enabled on this service.",
        "The semantic search subscription is not active.",
        "Semantic Search Standard Tier is not supported on Free SKU.",
    ],
)
def test_a_semantic_refusal_is_recognised(message: str) -> None:
    assert is_semantic_refusal(message)


@pytest.mark.parametrize(
    "message",
    [
        "500: the service is unavailable",
        "400: Invalid expression: Could not find a property named 'diets'.",
        "403: the caller does not have permission to query this index",
    ],
)
def test_an_ordinary_failure_is_not_a_semantic_refusal(message: str) -> None:
    # A malformed query is a bug to fix, not a ceiling to degrade past.
    assert not is_semantic_refusal(message)


# --- The tool result ---------------------------------------------------------


def test_the_tool_result_says_how_much_the_corpus_had_to_say() -> None:
    payload = retriever().search(UNANSWERABLE).as_tool_result()
    assert payload["confidence"] == "low"
    assert payload["reranked"] is True
    assert payload["notes"]
    assert {entry["id"] for entry in payload["passages"]}


def test_the_tool_result_carries_the_published_fields_an_answer_needs() -> None:
    payload = retriever().search("what is in a chicken bowl").as_tool_result()
    published = {name for entry in payload["passages"] for name in entry["published"]}
    assert {"allergens", "allergen_disclosure", "item_type"} & published


def test_the_alias_is_the_only_name_the_retriever_knows() -> None:
    fake = service()
    retriever(fake).search(ANSWERABLE)
    assert fake.calls[-1] == f"search:{schema.ALIAS}"
