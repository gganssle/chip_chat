"""The span the lane emits, and the outage it survives on its own.

Two of #49's acceptance criteria are statements about a trace and about a
failure, so both are asserted here against the real span recorder rather than
argued in a docstring.

Every test runs inside ``chat.turn`` → ``agent.step`` → ``tool.search_menu_knowledge``,
because :mod:`chip_chat.otel.spans` enforces the tree rather than documenting
it: ``retriever.search`` outside a tool call raises rather than emitting a span
nobody's dashboard is watching. That enforcement is the reason the fixture
exists, and it is also the thing being relied on.
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeEmbedder, FakeSearchService

from chip_chat.otel import (
    ToolName,
    agent_step,
    chat_turn,
    tool_call,
    vision_describe,
)
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.search import schema
from chip_chat.search.allowance import SemanticAllowance
from chip_chat.search.build import build
from chip_chat.search.client import ServiceError
from chip_chat.search.corpus import from_path
from chip_chat.search.embedding import EmbeddingDeployment
from chip_chat.search.fusion import SINGLE_RANKER_CEILING
from chip_chat.search.lane import DECLINED, LEXICAL_ONLY_TAG, KnowledgeLane
from chip_chat.search.retrieve import Confidence, Retriever

FIXTURE = Path(__file__).parent / "fixtures" / "chunks.jsonl"
RUN_ID = "20260827T053000Z"
DEPLOYMENT = EmbeddingDeployment(
    endpoint="https://aif-example.cognitiveservices.azure.com/", dimensions=8
)
QUESTION = "how do rewards points work"


@pytest.fixture
def spans() -> Iterator[SpanRecorder]:
    """Record spans inside the tool call a retrieval is required to nest in."""
    with (
        span_recorder("search") as recorder,
        chat_turn(session_id="search-tests", turn_index=0, message=QUESTION),
        agent_step(index=0),
        tool_call(ToolName.SEARCH_MENU_KNOWLEDGE, arguments={"query": QUESTION}),
    ):
        yield recorder


def lane(fake: FakeSearchService, **kwargs: Any) -> KnowledgeLane:
    """Return a lane over ``fake``."""
    return KnowledgeLane(Retriever(fake, **kwargs))


def corpus_service() -> FakeSearchService:
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


class EmptyService(FakeSearchService):
    """A service that answers 200 with nothing in it.

    Three different situations produce exactly this response and only one of
    them is a fault: a filter that matches no published item, an index that has
    just been rebuilt and holds nothing yet, and the Free-tier defect of
    ``docs/retrieval.md`` §9 arriving beside a lexical half that also matched
    nothing. Nothing inside one response separates them, which is why the span
    records the count and withholds the tell.
    """

    def search(self, target: str, query: Mapping[str, Any]) -> dict[str, Any]:
        return {"value": []}


class DeadService(FakeSearchService):
    """A service that is not answering. RFC-001 section 10's first row."""

    def search(self, target: str, query: Mapping[str, Any]) -> dict[str, Any]:
        raise ServiceError(
            "POST /indexes/corpus/docs/search did not reach "
            "https://srch-chip-chat-4cy39i.search.windows.net: "
            "ConnectError: [Errno 61] Connection refused"
        )


def documents_on(recorder: SpanRecorder) -> Sequence[Mapping[str, Any]]:
    """Return the retrieval documents recorded on ``retriever.search``."""
    attributes = recorder.attributes_of("retriever.search")
    found: dict[int, dict[str, Any]] = {}
    for name, value in attributes.items():
        if not name.startswith("retrieval.documents."):
            continue
        _, _, index, *rest = name.split(".")
        found.setdefault(int(index), {})[".".join(rest)] = value
    return [found[index] for index in sorted(found)]


# --- The span ----------------------------------------------------------------


def test_retrieval_nests_inside_the_tool_call_that_asked_for_it(
    spans: SpanRecorder,
) -> None:
    lane(corpus_service()).search(QUESTION)
    assert spans.names() == ("retriever.search",)


def test_documents_and_scores_are_both_on_the_span(spans: SpanRecorder) -> None:
    # #49: "emitted as the retriever.search span with documents and scores
    # attached, because retrieval debugging without scores in the trace is
    # guesswork."
    result = lane(corpus_service()).search(QUESTION)
    recorded = documents_on(spans)
    assert len(recorded) == len(result.passages)
    for document, passage in zip(recorded, result.passages, strict=True):
        assert document["document.id"] == passage.id
        assert document["document.score"] == pytest.approx(passage.ranking_score)
        assert document["document.content"]


def test_the_citation_rides_on_every_document_in_the_trace(
    spans: SpanRecorder,
) -> None:
    # RFC-001 section 08: citations are part of the payload rather than
    # reconstructed afterwards -- including in the trace #75 reads ids back
    # from.
    lane(corpus_service()).search(QUESTION)
    recorded = documents_on(spans)
    assert recorded
    for document in recorded:
        metadata = json.loads(str(document["document.metadata"]))
        assert metadata["source_url"].startswith("https://")
        assert metadata["harvested_at"].endswith("+00:00")
        assert "reranker_score" in metadata
        assert "overlap" in metadata


def test_the_span_says_which_alias_answered_and_how_it_was_ranked(
    spans: SpanRecorder,
) -> None:
    lane(corpus_service()).search(QUESTION)
    metadata = json.loads(str(spans.attributes_of("retriever.search")["metadata"]))
    assert metadata["index"] == schema.ALIAS
    assert metadata["confidence"] == Confidence.GROUNDED.value
    assert metadata["reranked"] is True
    assert metadata["allowance"]["limit"] == 1000
    assert metadata["constraints"]["without_allergens"] == []


def test_a_degraded_retrieval_says_so_on_the_span(spans: SpanRecorder) -> None:
    allowance = SemanticAllowance()
    allowance.exhaust("the month's semantic requests are spent")
    lane(corpus_service(), allowance=allowance).search(QUESTION)
    metadata = json.loads(str(spans.attributes_of("retriever.search")["metadata"]))
    assert metadata["reranked"] is False
    assert metadata["allowance"]["exhausted"] is True


def test_a_low_confidence_retrieval_is_tagged_for_slicing(
    spans: SpanRecorder,
) -> None:
    lane(corpus_service()).search("do you sell sushi")
    tags = spans.attributes_of("retriever.search")["tag.tags"]
    assert isinstance(tags, Sequence)
    assert list(tags) == ["retrieval.low"]


def test_a_grounded_retrieval_is_not_tagged(spans: SpanRecorder) -> None:
    lane(corpus_service()).search(QUESTION)
    assert "tag.tags" not in spans.attributes_of("retriever.search")


# --- The vector half that did not run ----------------------------------------


def test_a_lexical_only_result_is_legible_on_the_span(spans: SpanRecorder) -> None:
    # chip-wez. An operator reading a trace has to be able to see that this
    # hybrid query was hybrid in name only -- the arithmetic is already in the
    # per-document scores and nobody reads scores that way.
    fake = corpus_service()
    fake.drop_vector = True
    lane(fake).search(QUESTION)
    metadata = json.loads(str(spans.attributes_of("retriever.search")["metadata"]))
    assert metadata["degraded"] is True
    assert metadata["vector_arm"] == "dropped"
    tags = spans.attributes_of("retriever.search")["tag.tags"]
    assert isinstance(tags, Sequence)
    assert LEXICAL_ONLY_TAG in list(tags)


def test_the_lexical_only_span_is_not_marked_failed(spans: SpanRecorder) -> None:
    # The service answered 200 and the lane returned real passages. Marking the
    # span failed would put this in the same bucket as an outage, which has a
    # different remedy and a different blast radius.
    fake = corpus_service()
    fake.drop_vector = True
    lane(fake).search(QUESTION)
    statuses = {
        span.name: span.status.status_code.name for span in spans.finished_spans()
    }
    assert statuses["retriever.search"] != "ERROR"


def test_each_document_says_whether_the_vector_half_placed_it(
    spans: SpanRecorder,
) -> None:
    # The roll-up says "some document was placed by both". The per-document
    # reading says which, which is what a trace is for.
    lane(corpus_service()).search(QUESTION, rerank=False)
    recorded = documents_on(spans)
    assert recorded
    readings = {
        json.loads(str(document["document.metadata"]))["fused_by_both"]
        for document in recorded
    }
    assert True in readings


def test_a_healthy_hybrid_retrieval_carries_no_lexical_only_tag(
    spans: SpanRecorder,
) -> None:
    lane(corpus_service()).search(QUESTION)
    assert "tag.tags" not in spans.attributes_of("retriever.search")


# --- The tell, in attributes a dashboard can filter on -----------------------
#
# The metadata above is one JSON string. It is the right shape for somebody
# reading a single trace and the wrong shape for counting a defect over a week,
# because Application Insights filters attributes and does not parse blobs --
# the same reason `chip_chat.tokens.*` exists beside `llm.token_count.*`. So the
# reading rides twice, and these are the flat copies.


def test_the_tell_is_flagged_in_its_own_attribute_with_the_threshold_beside_it(
    spans: SpanRecorder,
) -> None:
    fake = corpus_service()
    fake.drop_vector = True
    lane(fake).search(QUESTION)
    attributes = spans.attributes_of("retriever.search")
    assert attributes["chip_chat.retrieval.single_ranker_fusion"] is True
    assert attributes["chip_chat.retrieval.vector_arm"] == "dropped"
    # The threshold travels with the verdict rather than being reconstructed
    # later from whichever version of the source tree somebody guesses was
    # deployed. `1/60`, and the top score sits at or below it.
    ceiling = attributes["chip_chat.retrieval.single_ranker_ceiling"]
    assert ceiling == pytest.approx(SINGLE_RANKER_CEILING)
    top = attributes["chip_chat.retrieval.top_fused_score"]
    assert isinstance(top, float)
    assert top <= SINGLE_RANKER_CEILING * (1.0 + 1e-6)


def test_a_healthy_hybrid_retrieval_is_not_flagged(spans: SpanRecorder) -> None:
    lane(corpus_service()).search(QUESTION)
    attributes = spans.attributes_of("retriever.search")
    assert attributes["chip_chat.retrieval.single_ranker_fusion"] is False
    assert attributes["chip_chat.retrieval.vector_arm"] == "contributed"
    top = attributes["chip_chat.retrieval.top_fused_score"]
    assert isinstance(top, float)
    assert top > SINGLE_RANKER_CEILING


def test_nothing_matched_at_all_is_a_count_rather_than_a_defect_report(
    spans: SpanRecorder,
) -> None:
    # An empty result set and a dropped vector half are different claims and
    # only one of them is a fault. The count is present and zero -- an absent
    # attribute could not distinguish "returned nothing" from "was never asked"
    # -- and the tell says nothing, because there is no score to prove anything
    # with.
    lane(EmptyService()).search(QUESTION)
    attributes = spans.attributes_of("retriever.search")
    assert attributes["chip_chat.retrieval.document_count"] == 0
    assert attributes["chip_chat.retrieval.vector_arm"] == "undetermined"
    assert "chip_chat.retrieval.single_ranker_fusion" not in attributes
    assert "chip_chat.retrieval.top_fused_score" not in attributes
    assert "chip_chat.retrieval.single_ranker_ceiling" not in attributes


def test_the_document_count_matches_what_the_retrieval_returned(
    spans: SpanRecorder,
) -> None:
    result = lane(corpus_service()).search(QUESTION)
    attributes = spans.attributes_of("retriever.search")
    assert attributes["chip_chat.retrieval.document_count"] == len(result.passages)


def test_a_declining_lane_records_a_reading_it_cannot_take(
    spans: SpanRecorder,
) -> None:
    # The service never answered, so there is nothing to read and the arm is
    # undetermined -- but the attribute is present, because a dashboard slicing
    # retrievals by arm should find the outage rows rather than silently omit
    # them and flatter the healthy rate.
    lane(DeadService()).search(QUESTION)
    attributes = spans.attributes_of("retriever.search")
    assert attributes["chip_chat.retrieval.vector_arm"] == "undetermined"
    assert attributes["chip_chat.retrieval.document_count"] == 0
    assert "chip_chat.retrieval.single_ranker_fusion" not in attributes


# --- The outage --------------------------------------------------------------


def test_an_unavailable_service_declines_instead_of_failing_the_turn(
    spans: SpanRecorder,
) -> None:
    # RFC-001 section 10: AI Search unavailable -> the knowledge lane declines
    # and says why; other lanes unaffected. Blast radius is knowledge only, and
    # that is a property of this method returning rather than raising.
    result = lane(DeadService()).search(QUESTION)

    assert not result.answered
    assert result.declined is not None
    assert "Connection refused" in result.declined
    assert result.passages == ()
    assert result.confidence is Confidence.NONE
    assert result.notes == (DECLINED,)


def test_a_declining_lane_is_visible_in_the_trace(spans: SpanRecorder) -> None:
    # A lane that declined quietly looks exactly like a corpus with nothing in
    # it, and those need different fixes.
    lane(DeadService()).search(QUESTION)
    span = spans.span_named("retriever.search")
    assert span.status.status_code.name == "ERROR"
    metadata = json.loads(str(spans.attributes_of("retriever.search")["metadata"]))
    assert metadata["declined"] is True
    assert "Connection refused" in metadata["reason"]


def test_the_declining_lane_tells_the_agent_which_lane_is_out(
    spans: SpanRecorder,
) -> None:
    payload = lane(DeadService()).search(QUESTION).as_tool_result()
    assert payload["declined"] == "KNOWLEDGE_LANE_UNAVAILABLE"
    assert "passages" not in payload
    assert payload["notes"] == [DECLINED]


def test_the_turn_survives_a_dead_lane_and_the_next_call_still_works(
    spans: SpanRecorder,
) -> None:
    # The blast radius, asserted rather than described: the outage does not
    # propagate, so anything else the same turn does afterwards is unaffected.
    assert not lane(DeadService()).search(QUESTION).answered
    healthy = lane(corpus_service()).search(QUESTION)
    assert healthy.answered
    assert healthy.grounded
    assert spans.names() == ("retriever.search", "retriever.search")


def test_a_dead_knowledge_lane_leaves_another_lane_in_the_same_turn_untouched() -> None:
    """#49's fifth criterion, over a whole turn rather than over one call.

    The tests above say the lane returns rather than raises. That is the
    mechanism; this is the property RFC-001 §10 actually states — *AI Search
    unavailable → the knowledge lane declines and says why; other lanes
    unaffected. Blast radius: knowledge only* — and the only way to assert
    "other lanes" is to have a second one in the trace.

    The second lane here is a ``vision.describe`` span rather than
    :mod:`chip_chat.vision`. That is deliberate and it is not a weaker test.
    ``search`` does not depend on ``vision`` and must not start: the two lanes
    meet in the agent's tool layer and nowhere below it, and a test that
    reached across would be asserting the blast radius by building the coupling
    it exists to rule out. What both lanes genuinely share is the span schema,
    which is where a turn is assembled and therefore where an outage would have
    to leak in order to reach anything else. So the second tool call is opened
    for real, under the schema's own parent rules, and the assertions are about
    what the exporter saw: the knowledge lane's span failed, the photo lane's
    span did not, neither tool call failed, and the turn closed.
    """
    with (
        span_recorder("search") as recorder,
        chat_turn(session_id="search-tests", turn_index=0, message=QUESTION),
        agent_step(index=0),
    ):
        with tool_call(ToolName.SEARCH_MENU_KNOWLEDGE, arguments={"query": QUESTION}):
            knowledge = lane(DeadService()).search(QUESTION)
        with (
            tool_call(ToolName.MATCH_MEAL_FROM_PHOTO, arguments={"photo": "a-blob"}),
            vision_describe(image_ref="a-blob", model="gpt-4.1-mini") as vision,
        ):
            vision.record_usage(prompt_tokens=1, completion_tokens=1)

    assert knowledge.declined is not None
    statuses = {
        span.name: span.status.status_code.name for span in recorder.finished_spans()
    }
    assert statuses["retriever.search"] == "ERROR"
    # Everything else in the turn, including the lane that never heard about it.
    assert statuses["vision.describe"] != "ERROR"
    assert statuses["tool.search_menu_knowledge"] != "ERROR"
    assert statuses["tool.match_meal_from_photo"] != "ERROR"
    assert statuses["agent.step"] != "ERROR"
    assert statuses["chat.turn"] != "ERROR"
    # One trace, so this is one turn rather than two runs that cannot interfere.
    assert len(recorder.trace_ids()) == 1
