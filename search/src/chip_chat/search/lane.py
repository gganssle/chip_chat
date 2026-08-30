"""The knowledge lane: one retrieval, one span, and a lane that declines alone.

RFC-001 §09 puts retrieval inside the tool call that asked for it::

    tool.search_menu_knowledge
    `- retriever.search        documents + scores

so this module opens the child and nothing else. The tool span belongs to the
agent's tool layer, which is where the arguments the model produced are recorded
— the same division :mod:`chip_chat.vision.lane` draws, and for the same reason:
a lane that opened its own tool span would produce two tool calls for one turn
and split the trace at exactly the point somebody is trying to read it.

**Scores are on the span because retrieval debugging without them is guesswork**
— #49's words. Every document carries its fusion score, its reranker score when
there was one, its citation, and its lexical overlap, so a trace answers *why
did it return that* rather than only *what did it return*.

**And whether the result is lexical-only is on the span, because the scores
alone say it and nobody reads scores that way.** The Free tier drops the vector
half of a hybrid query and returns HTTP 200 (``docs/retrieval.md`` §9), and the
only evidence is that every fused score is at or below ``1/60``. An operator can
work that out from the numbers already here; an operator *will* work it out from
the ``retrieval.lexical_only`` tag and the ``chip_chat.retrieval.*`` attributes,
which is a different thing. Both the roll-up and the per-document reading are
recorded — :attr:`fused_by_both` on each document is the individual arithmetic,
so a trace shows which passages the vector half actually placed and not merely
that some did.

**The roll-up rides in flat attributes as well as in metadata, and that is not
duplication for its own sake.** ``set_metadata`` writes one JSON string, which is
where somebody reading a single trace wants everything about a retrieval
together; it is not where a dashboard counting a defect over a week can look,
because Application Insights filters attributes and does not parse blobs — the
standing reason the token rollups exist. So
:meth:`~chip_chat.otel.spans.RetrieverRecorder.record_fusion` puts the reading,
the passage count, the top fused score, the threshold that score was judged
against and the tell itself under ``chip_chat.retrieval.*``. The last three are
set only on a hybrid query that returned something, because that is the only
query the inequality speaks about; absent there means *not applicable* and never
*healthy*.

**A declining lane is not a failing turn.** RFC-001 §10 gives this lane a blast
radius of one row — *AI Search unavailable → the knowledge lane declines and
says why; other lanes unaffected* — and that is a property of this method: it
catches :class:`~chip_chat.search.errors.SearchError`, marks the span failed so
the outage is visible rather than looking like an empty corpus, and returns a
:class:`~chip_chat.search.retrieve.Retrieval` that says it declined. Nothing
propagates. A visitor asking about their points balance in the next breath is
served by a lane that never heard about it.

Note what is *not* on that path. A spent semantic allowance is not an outage and
must never reach it: it degrades to hybrid-without-reranking inside
:class:`~chip_chat.search.retrieve.Retriever` and answers. Declining because a
counter rolled over would be declining for a reason no visitor can see and
nobody can fix before the first of the month.
"""

from chip_chat.otel import Document, retriever_search
from chip_chat.search.errors import SearchError
from chip_chat.search.fusion import VectorArm, placed_by_both, tell
from chip_chat.search.query import Constraints
from chip_chat.search.retrieve import Confidence, Retrieval, Retriever

__all__ = ["LEXICAL_ONLY_TAG", "KnowledgeLane"]

LEXICAL_ONLY_TAG = "retrieval.lexical_only"
"""The tag on a ``retriever.search`` span whose vector half did not answer.

Beside ``retrieval.low`` and ``retrieval.none`` rather than instead of them: a
lexical-only retrieval can carry any of the three confidences, and the two facts
are independent. A dashboard counting this tag over a day is measuring the rate
``docs/retrieval.md`` §9 had to measure by hand, which is the difference between
a defect somebody remembers and one somebody can watch.
"""

DECLINED = (
    "The published-menu search service is not answering, so I cannot look "
    "anything up in the restaurant's published pages right now."
)
"""What the lane says when the service is down. A sentence, not a stack trace:
the agent reads this and tells the visitor which lane is out."""


class KnowledgeLane:
    """Retrieval as the tool layer sees it: one call, one span, never raising."""

    __slots__ = ("_retriever",)

    def __init__(self, retriever: Retriever) -> None:
        """Initialise the lane.

        Args:
            retriever: The retriever. Built once per process, because the
                connection pool inside it is what keeps a query at 11 ms rather
                than 84 — see :class:`~chip_chat.search.retrieve.Retriever`.
        """
        self._retriever = retriever

    def search(
        self,
        text: str,
        *,
        top: int | None = None,
        constraints: Constraints | None = None,
        rerank: bool = True,
    ) -> Retrieval:
        """Search the corpus inside a ``retriever.search`` span.

        Must be called inside a ``tool.<tool_name>`` span; the schema enforces
        the tree rather than documenting it, so a retrieval outside a tool call
        raises :class:`~chip_chat.otel.spans.SpanSchemaError` rather than
        emitting a span nobody's dashboard is watching.

        Args:
            text: The visitor's words.
            top: Passages to return.
            constraints: Constraints to apply instead of reading them out of
                ``text``.
            rerank: Whether to ask for semantic reranking.

        Returns:
            The retrieval, or one whose
            :attr:`~chip_chat.search.retrieve.Retrieval.declined` says why the
            lane could not answer. This method does not raise.
        """
        with retriever_search(query=text, index=self._retriever.alias) as span:
            try:
                result = self._retriever.search(
                    text, top=top, constraints=constraints, rerank=rerank
                )
            except SearchError as error:
                span.record_failure(error)
                span.record_fusion(vector_arm=VectorArm.UNDETERMINED.value, documents=0)
                span.set_metadata(
                    index=self._retriever.alias,
                    declined=True,
                    reason=str(error),
                    allowance=self._retriever.allowance.report().as_dict(),
                )
                return Retrieval(
                    query=text,
                    confidence=Confidence.NONE,
                    vector_arm=VectorArm.UNDETERMINED,
                    notes=(DECLINED,),
                    declined=str(error),
                )
            span.record_documents(
                [
                    Document(
                        id=passage.id,
                        content=passage.caption or passage.text,
                        score=passage.ranking_score,
                        metadata={
                            "label": passage.label,
                            "kind": passage.kind,
                            "source_url": passage.source_url,
                            "harvested_at": passage.harvested_at,
                            "score": passage.score,
                            "reranker_score": passage.reranker_score,
                            "overlap": round(passage.overlap, 3),
                            "fused_by_both": placed_by_both(result.halves, passage.score),
                        },
                    )
                    for passage in result.passages
                ]
            )
            # The reading again, in flat attributes this time, because the
            # metadata below is one JSON string and Application Insights filters
            # attributes rather than parsing blobs. The verdict is not recomputed
            # here -- ``tell`` is handed the arm the retrieval already carries,
            # so the span cannot contradict the tool result it describes -- and
            # the threshold arrives from ``fusion`` rather than being restated,
            # because ``chip_chat.otel`` is a leaf and holds no opinion about
            # anybody's ranker.
            reading = tell(
                arm=result.vector_arm,
                halves=result.halves,
                scores=[passage.score for passage in result.passages],
            )
            span.record_fusion(
                vector_arm=reading.arm.value,
                documents=reading.documents,
                top_fused_score=reading.top_score,
                single_ranker_ceiling=reading.ceiling,
                single_ranker_fusion=reading.single_ranker,
            )
            # Supersedes the ``{"index": ...}`` metadata ``retriever_search``
            # set on entry: one metadata attribute, so it carries the index and
            # everything an eval slices retrieval by.
            span.set_metadata(
                index=self._retriever.alias,
                confidence=result.confidence.value,
                reranked=result.reranked,
                degraded=result.degraded,
                vector_arm=result.vector_arm.value,
                floor=result.floor,
                uncitable=result.uncitable,
                constraints=result.constraints.as_dict(),
                allowance=self._retriever.allowance.report().as_dict(),
            )
            if result.confidence is not Confidence.GROUNDED:
                # Not an error -- the lane worked and the corpus was silent --
                # but it is the case #49 asks to be legible, and a tag is how a
                # trace is sliced without parsing metadata JSON.
                span.add_tags(f"retrieval.{result.confidence.value}")
            if result.degraded:
                # Also not an error, and for a worse reason: the service
                # answered 200 and the retrieval is real, it is just half of
                # the retrieval that was asked for. Marking the span failed
                # would put it in the same bucket as an outage, which is a
                # different remedy and a different blast radius. A tag is what
                # lets somebody count these across a day and see the rate
                # docs/retrieval.md section 9 measured.
                span.add_tags(LEXICAL_ONLY_TAG)
            return result
