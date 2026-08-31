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
from chip_chat.search.fusion import SINGLE_RANKER_CEILING, VectorArm, fused_by_both
from chip_chat.search.query import Halves
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
        assert set(citation) == {
            "id",
            "label",
            "source_url",
            "public_url",
            "harvested_at",
        }


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


# --- The ablation's halves ---------------------------------------------------


def test_a_single_half_run_says_which_half_it_was() -> None:
    # The label travels on the result rather than being remembered by whoever
    # started the sweep, so a table of four arms cannot mislabel one of them.
    for halves in Halves:
        result = retriever().search(ANSWERABLE, rerank=False, halves=halves)
        assert result.halves is halves


def test_a_serving_path_result_is_labelled_hybrid_without_being_told() -> None:
    assert retriever().search(ANSWERABLE).halves is Halves.HYBRID


def test_the_keyword_arm_reaches_the_service_without_a_vector_half() -> None:
    fake = service()
    retriever(fake).search(ANSWERABLE, rerank=False, halves=Halves.KEYWORD)
    assert "vectorQueries" not in fake.queries[-1]


def test_the_vector_arm_reaches_the_service_without_a_lexical_half() -> None:
    fake = service()
    retriever(fake).search(ANSWERABLE, rerank=False, halves=Halves.VECTOR)
    assert "search" not in fake.queries[-1]


def test_every_arm_still_returns_only_citable_passages() -> None:
    # The property PRD K2 sets a zero target on is a property of the type, so
    # it cannot be one of the things the ablation trades away.
    for halves in Halves:
        result = retriever().search(ANSWERABLE, rerank=False, halves=halves)
        assert result.passages
        for passage in result.passages:
            assert passage.source_url.startswith("https://")


def test_a_degraded_arm_that_matched_no_word_is_low_confidence() -> None:
    # The vector-only arm on a question the corpus cannot answer is the exact
    # near-miss RFC-001 section 08 warns about: neighbours came back, none of
    # them shares a content word with the question, and the fused score cannot
    # tell you so. The lexical floor can, which is why it is not the ranker.
    result = retriever().search(UNANSWERABLE, rerank=False, halves=Halves.VECTOR)
    assert result.passages
    assert result.confidence is Confidence.LOW


# --- The vector half that did not run ----------------------------------------
#
# chip-wez. The Free tier drops the vector half of a query and returns HTTP 200
# with an empty vector result, so the response is a well-formed hybrid response
# that is silently the keyword response. ``FakeSearchService.drop_vector``
# reproduces exactly that -- a 200 with real passages on it, not an error --
# because a fake that raised would be modelling an outage, which is the one
# thing this defect is not.


def test_a_healthy_hybrid_query_says_the_vector_half_contributed() -> None:
    result = retriever().search(ANSWERABLE, rerank=False)
    assert result.vector_arm is VectorArm.CONTRIBUTED
    assert not result.degraded


def test_a_dropped_vector_half_is_read_off_the_fused_scores() -> None:
    # Nothing in the response says which ranker found anything. This is the
    # whole of the signal: no returned document scores above 1/60, so none was
    # placed by both halves.
    fake = service()
    fake.drop_vector = True
    result = retriever(fake).search(ANSWERABLE, rerank=False)
    assert result.passages, "the lexical half answers normally, as it does live"
    # Note the tolerance, and note that it is the whole difficulty. The top
    # score comes back as 0.01666666753590107, which is single-precision 1/60
    # widened to a double and therefore *larger* than 1/60. A comparison written
    # without it would read every degraded response as a healthy one.
    assert not any(fused_by_both(passage.score) for passage in result.passages)
    assert max(passage.score for passage in result.passages) == pytest.approx(
        SINGLE_RANKER_CEILING, abs=1e-6
    )
    assert result.vector_arm is VectorArm.DROPPED
    assert result.degraded


def test_a_degraded_result_is_served_rather_than_retried_or_refused() -> None:
    # docs/decisions/vector-arm-degradation.md. One query out, one query back:
    # the fault does not clear in minutes, so a retry buys about one recovery in
    # eight and on the reranked path buys it with a monthly semantic request.
    fake = service()
    fake.drop_vector = True
    before = sum(1 for call in fake.calls if call.startswith("search:"))
    result = retriever(fake).search(ANSWERABLE, rerank=False)
    after = sum(1 for call in fake.calls if call.startswith("search:"))
    assert after - before == 1
    assert result.answered
    assert result.passages


def test_a_degraded_result_forbids_the_one_claim_it_cannot_support() -> None:
    # The visitor must never be told the restaurant does not publish something
    # by a retriever that looked with half of itself.
    fake = service()
    fake.drop_vector = True
    result = retriever(fake).search(UNANSWERABLE, rerank=False)
    joined = " ".join(result.notes)
    assert "DEGRADED RETRIEVAL" in joined
    assert "Do NOT say the restaurant does not publish" in joined


def test_a_degraded_result_does_not_carry_the_note_that_contradicts_it() -> None:
    # "these are the nearest passages in the corpus" and "half the retriever did
    # not look" are one instruction and its contradiction. Only the true one.
    fake = service()
    fake.drop_vector = True
    result = retriever(fake).search(UNANSWERABLE, rerank=False)
    assert not any("nearest passages in the corpus" in note for note in result.notes)


def test_a_healthy_low_confidence_result_keeps_its_own_note() -> None:
    # The withholding above is conditional on the defect, not a deletion.
    result = retriever().search(UNANSWERABLE, rerank=False)
    assert any("nearest passages in the corpus" in note for note in result.notes)


def test_the_tool_boundary_distinguishes_degraded_from_low_confidence() -> None:
    # Three different states the agent has to be able to tell apart: declined,
    # low confidence, and answered-with-half-a-retriever. The third is the one
    # #49's payload could not previously express.
    fake = service()
    fake.drop_vector = True
    payload = retriever(fake).search(ANSWERABLE, rerank=False).as_tool_result()
    assert payload["degraded"] is True
    assert payload["vector_arm"] == "dropped"
    assert "declined" not in payload
    healthy = retriever().search(ANSWERABLE, rerank=False).as_tool_result()
    assert healthy["degraded"] is False
    assert healthy["vector_arm"] == "contributed"


def test_the_vector_arm_of_the_ablation_reads_its_own_emptiness() -> None:
    # A vector-only query has no fusion to read, so the returned count is the
    # signal -- and that is the reading #50's sweeps recorded as 7%.
    fake = service()
    fake.drop_vector = True
    result = retriever(fake).search(ANSWERABLE, rerank=False, halves=Halves.VECTOR)
    assert result.passages == ()
    assert result.vector_arm is VectorArm.DROPPED


def test_the_keyword_arm_is_never_reported_as_degraded() -> None:
    # It asked for no vector half, so it lost nothing. Reporting it degraded
    # would put a defect on the one arm of the ablation that cannot have it.
    fake = service()
    fake.drop_vector = True
    result = retriever(fake).search(ANSWERABLE, rerank=False, halves=Halves.KEYWORD)
    assert result.vector_arm is VectorArm.NOT_SENT
    assert not result.degraded


def test_an_unanswerable_question_on_a_healthy_service_is_not_a_wolf() -> None:
    # The negative set is eight questions the corpus cannot answer, and it is a
    # real part of #50's measurement. A detector that called those degraded
    # would make restraint unmeasurable -- and would be wrong, because a
    # nearest-neighbour search returns neighbours for any question it is asked.
    result = retriever().search(UNANSWERABLE, rerank=False)
    assert result.passages
    assert result.confidence is Confidence.LOW
    assert not result.degraded


def test_a_filter_that_matches_nothing_is_a_finding_rather_than_a_fault() -> None:
    fake = FakeSearchService()
    fake.create_index({"name": "corpus-empty"})
    fake.set_alias(schema.ALIAS, "corpus-empty")
    result = retriever(fake).search("anything under 100 calories")
    assert result.constraints.filtered
    assert result.vector_arm is VectorArm.UNDETERMINED
    assert not result.degraded


def test_an_empty_response_is_not_attributed_to_the_vector_half() -> None:
    # An empty index produces this, and so does a lexical half that matched
    # nothing beside a dropped vector half. DROPPED is claimed from the
    # arithmetic or not at all; see chip_chat.search.fusion.
    fake = FakeSearchService()
    fake.create_index({"name": "corpus-empty"})
    fake.set_alias(schema.ALIAS, "corpus-empty")
    result = retriever(fake).search(ANSWERABLE)
    assert result.passages == ()
    assert result.vector_arm is VectorArm.UNDETERMINED
    assert not result.degraded
