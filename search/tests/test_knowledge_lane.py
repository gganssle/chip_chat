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

from chip_chat.otel import ToolName, agent_step, chat_turn, tool_call
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.search import schema
from chip_chat.search.allowance import SemanticAllowance
from chip_chat.search.build import build
from chip_chat.search.client import ServiceError
from chip_chat.search.corpus import from_path
from chip_chat.search.embedding import EmbeddingDeployment
from chip_chat.search.lane import DECLINED, KnowledgeLane
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
