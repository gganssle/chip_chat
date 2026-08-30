"""The retrieval-degradation attributes, held to the shape a dashboard needs.

A hybrid search can lose half of itself and still answer 200 with a well-formed
result set — ``docs/retrieval.md`` §9 measures that happening to between a
quarter and nine in ten queries against the tier this demo runs on. The
retriever detects it from the fused scores; this package is where the detection
becomes something an operator can filter on, and these tests are about the two
properties that makes true.

**The reading is in flat attributes rather than in ``metadata``**, for the same
reason ``chip_chat.tokens.*`` exists beside ``llm.token_count.*``: Application
Insights searches attributes and does not walk trace trees, and it does not parse
JSON blobs either. *How often is the retriever running on half of itself* has to
be one attribute lookup and an alert rule, not a query somebody has to compose
correctly at the moment they least want to.

**Absent is not false.** The fusion inequality only speaks about a hybrid query
that returned at least one document. On anything else the recorder writes no
verdict at all, so a dashboard counting
``chip_chat.retrieval.single_ranker_fusion`` is counting evaluated readings and
never a query the tell cannot speak about. That is the difference between a
detector people believe and one they learn to ignore, and it is asserted here as
an absence rather than argued in prose.

This package is a leaf: it imports nothing from the workspace, holds no opinion
about anybody's ranker, and receives the threshold as a number. The ``1/60``
below is written out for the same reason the retriever computes it from ``k``
rather than typing it — so that a wrong constant is visible somewhere rather than
nowhere.
"""

import textwrap

import pytest

from chip_chat.otel.attributes import ChipChatAttributes
from chip_chat.otel.schema import ToolName
from chip_chat.otel.spans import (
    Document,
    agent_step,
    chat_turn,
    retriever_search,
    tool_call,
)
from chip_chat.otel.testing import SpanRecorder, span_recorder

RRF_K = 60
"""Azure AI Search's reciprocal rank fusion constant, as the retriever measured
it against the live service. Restated here rather than imported because
``chip_chat.otel`` may import nothing from the workspace; the retriever derives
its own from ``RRF_K`` and passes the quotient in, so the two cannot silently
disagree without one of these numbers being visibly wrong."""

CEILING = 1.0 / RRF_K
"""The most a document placed by exactly one ranker can score."""

HEALTHY_TOP = 0.03306011110544205
"""A top fused score from the live alias whose vector half answered."""

DEGRADED_TOP = 0.01666666753590107
"""``1/60`` as the service actually sends it: a float32 widened to a double, and
therefore *larger* than the exact value. It is here so that the attribute is
asserted against the number a real degraded query produces rather than against
the tidy one."""


def record(
    *,
    vector_arm: str,
    documents: int,
    top_fused_score: float | None = None,
    single_ranker_ceiling: float | None = None,
    single_ranker_fusion: bool | None = None,
) -> SpanRecorder:
    """Emit one turn whose retrieval records this reading, and return the spans.

    The nesting is not decoration. ``retriever.search`` outside a tool call
    raises rather than emitting a span nobody's dashboard is watching, so every
    assertion below is also an assertion that the reading rides on a span the
    schema still accepts.
    """
    with span_recorder("otel-tests") as spans:
        with (
            chat_turn(session_id="sess-1", turn_index=0, message="is the sofritas vegan"),
            agent_step(index=0),
            tool_call(ToolName.SEARCH_MENU_KNOWLEDGE, arguments={"query": "sofritas"}),
            retriever_search(query="sofritas", index="corpus") as search,
        ):
            search.record_documents(
                [Document(id="CMG-5252", content="Sofritas is braised tofu.")]
            )
            search.record_fusion(
                vector_arm=vector_arm,
                documents=documents,
                top_fused_score=top_fused_score,
                single_ranker_ceiling=single_ranker_ceiling,
                single_ranker_fusion=single_ranker_fusion,
            )
        return spans


def attributes_of(spans: SpanRecorder) -> dict[str, object]:
    """The ``retriever.search`` attributes, as a plain dict."""
    return dict(spans.attributes_of("retriever.search"))


# --- The tell ----------------------------------------------------------------


def test_a_single_ranker_fusion_is_flagged_with_the_threshold_beside_it() -> None:
    attributes = attributes_of(
        record(
            vector_arm="dropped",
            documents=5,
            top_fused_score=DEGRADED_TOP,
            single_ranker_ceiling=CEILING,
            single_ranker_fusion=True,
        )
    )
    assert attributes[ChipChatAttributes.RETRIEVAL_SINGLE_RANKER_FUSION] is True
    assert attributes[ChipChatAttributes.RETRIEVAL_VECTOR_ARM] == "dropped"
    assert attributes[ChipChatAttributes.RETRIEVAL_DOCUMENT_COUNT] == 5
    # The evidence and the threshold, so a trace read months from now can be
    # re-judged without re-querying a service whose behaviour may have moved.
    assert attributes[ChipChatAttributes.RETRIEVAL_TOP_FUSED_SCORE] == DEGRADED_TOP
    assert attributes[ChipChatAttributes.RETRIEVAL_SINGLE_RANKER_CEILING] == CEILING


def test_a_two_ranker_fusion_is_recorded_as_a_false_rather_than_omitted() -> None:
    # The inequality *was* evaluated here, and "evaluated and healthy" is a
    # different fact from "not evaluated". A dashboard needs the denominator.
    attributes = attributes_of(
        record(
            vector_arm="contributed",
            documents=5,
            top_fused_score=HEALTHY_TOP,
            single_ranker_ceiling=CEILING,
            single_ranker_fusion=False,
        )
    )
    assert attributes[ChipChatAttributes.RETRIEVAL_SINGLE_RANKER_FUSION] is False
    assert attributes[ChipChatAttributes.RETRIEVAL_VECTOR_ARM] == "contributed"


# --- What it withholds -------------------------------------------------------


def test_a_query_that_was_never_hybrid_carries_no_verdict_at_all() -> None:
    # A lexical-only query's @search.score is BM25 and a vector-only query's is
    # a cosine similarity; the ceiling means nothing against either. Recording
    # False here would put a healthy-looking reading on a query the tell cannot
    # speak about, which is how a detector stops being believed.
    attributes = attributes_of(record(vector_arm="not_sent", documents=5))
    assert ChipChatAttributes.RETRIEVAL_SINGLE_RANKER_FUSION not in attributes
    assert ChipChatAttributes.RETRIEVAL_TOP_FUSED_SCORE not in attributes
    assert ChipChatAttributes.RETRIEVAL_SINGLE_RANKER_CEILING not in attributes
    assert attributes[ChipChatAttributes.RETRIEVAL_VECTOR_ARM] == "not_sent"


def test_nothing_matched_at_all_is_a_count_and_never_a_flag() -> None:
    # A filter matching no published item, a freshly built index and the defect
    # itself all produce an empty result set, and nothing inside one response
    # separates them. The count is present and zero — an absent attribute could
    # not tell "returned nothing" from "was never asked" — and the tell is
    # silent, because there is no score to prove anything with.
    attributes = attributes_of(record(vector_arm="undetermined", documents=0))
    assert attributes[ChipChatAttributes.RETRIEVAL_DOCUMENT_COUNT] == 0
    assert attributes[ChipChatAttributes.RETRIEVAL_VECTOR_ARM] == "undetermined"
    assert ChipChatAttributes.RETRIEVAL_SINGLE_RANKER_FUSION not in attributes


def test_the_arithmetic_may_not_arrive_half_recorded() -> None:
    # A fired tell with no threshold beside it is evidence nobody can check.
    # The cheapest moment to refuse it is at the call site.
    with pytest.raises(ValueError, match="travels whole"):
        record(
            vector_arm="dropped",
            documents=5,
            single_ranker_fusion=True,
        )


# --- The schema --------------------------------------------------------------


def test_the_reading_does_not_disturb_the_span_tree() -> None:
    # RFC-001 §09 nesting is enforced rather than documented, so an attribute
    # added to retriever.search has to leave the tree it hangs on untouched.
    spans = record(
        vector_arm="dropped",
        documents=5,
        top_fused_score=DEGRADED_TOP,
        single_ranker_ceiling=CEILING,
        single_ranker_fusion=True,
    )
    assert (
        spans.tree_text()
        == textwrap.dedent(
            """
        chat.turn
          agent.step
            tool.search_menu_knowledge
              retriever.search
        """
        ).strip()
    )


def test_every_attribute_the_reading_adds_is_in_our_own_namespace() -> None:
    # OpenInference owns retrieval.documents.* and the OTel db.* conventions own
    # the Cortex Analyst span; neither has anything to say about rank fusion, so
    # this is ours and it says so in the name.
    attributes = attributes_of(
        record(
            vector_arm="dropped",
            documents=5,
            top_fused_score=DEGRADED_TOP,
            single_ranker_ceiling=CEILING,
            single_ranker_fusion=True,
        )
    )
    added = [name for name in attributes if name.startswith("chip_chat.retrieval.")]
    assert sorted(added) == [
        ChipChatAttributes.RETRIEVAL_DOCUMENT_COUNT,
        ChipChatAttributes.RETRIEVAL_SINGLE_RANKER_CEILING,
        ChipChatAttributes.RETRIEVAL_SINGLE_RANKER_FUSION,
        ChipChatAttributes.RETRIEVAL_TOP_FUSED_SCORE,
        ChipChatAttributes.RETRIEVAL_VECTOR_ARM,
    ]
