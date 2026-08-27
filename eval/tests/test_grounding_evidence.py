"""What the turn really had, read off ``retriever.search``.

#75 puts the judge's evidence on that span by name, so these tests are about
one thing: a reader that gets this wrong scores a response against passages it
cannot show belong to it, and every number downstream is over the wrong set.
"""

from chip_chat.eval.grounding.evidence import read_evidence
from chip_chat.eval.grounding.testing import turn_spans

_ENTRY = "golden/k1-bowl-ingredients"
_PASSAGES = (
    {
        "id": "menu-chicken-bowl",
        "content": "Chicken bowl: white rice, black beans, chicken.",
        "score": 0.81,
        "metadata": {"source_url": "https://example.test/menu", "kind": "menu"},
    },
    {"id": "menu-white-rice", "content": "White rice.", "score": 0.4},
)


def test_the_documents_come_back_in_rank_order() -> None:
    """The flattened OpenInference layout, read back as the passages it is."""
    evidence = read_evidence(_ENTRY, turn_spans(_PASSAGES))

    assert evidence.readable
    assert [passage.id for passage in evidence.passages] == [
        "menu-chicken-bowl",
        "menu-white-rice",
    ]
    assert evidence.passages[0].score == 0.81
    assert evidence.passages[0].metadata["source_url"] == "https://example.test/menu"
    assert evidence.ids == {"menu-chicken-bowl", "menu-white-rice"}


def test_a_turn_that_never_searched_retrieved_nothing() -> None:
    """The finding a free run produces: fluent prose attached to nothing."""
    evidence = read_evidence(_ENTRY, turn_spans(searches=0))

    assert evidence.readable
    assert evidence.searches == 0
    assert not evidence.retrieved


def test_a_search_that_returned_nothing_is_not_a_search_that_failed() -> None:
    """An empty corpus and an unavailable service are two different findings."""
    empty = read_evidence(_ENTRY, turn_spans(searches=1))
    outage = read_evidence(_ENTRY, turn_spans(searches=1, declined=True))

    assert empty.searches == 1
    assert empty.failed_searches == 0
    assert outage.failed_searches == 1


def test_passages_are_collected_across_every_search() -> None:
    """A turn that searched twice had everything both searches returned."""
    evidence = read_evidence(_ENTRY, turn_spans(_PASSAGES, searches=2))

    assert evidence.searches == 2
    assert len(evidence.passages) == 2


def test_a_split_turn_is_unreadable_and_names_the_issue() -> None:
    """#103. The passages are all still there and none of them is attached.

    Unreadable rather than empty: a reader that collected them anyway would
    score the response against retrieval it cannot show belongs to it, which is
    worse than scoring nothing.
    """
    evidence = read_evidence(_ENTRY, turn_spans(_PASSAGES, split=True))

    assert evidence.split
    assert not evidence.readable
    assert "#103" in (evidence.unreadable_because or "")
    assert evidence.passages  # the spans were read; they are simply not usable


def test_no_spans_at_all_is_an_error_rather_than_an_empty_retrieval() -> None:
    """Nothing recorded is not evidence that nothing was retrieved."""
    evidence = read_evidence(_ENTRY, ())

    assert not evidence.readable
    assert evidence.unreadable_because == "no spans were recorded for this turn"


def test_a_document_with_no_id_is_dropped() -> None:
    """An unidentified passage cannot be what a citation resolved against."""
    spans = turn_spans(({"id": "menu-a"},))
    stripped = tuple(
        span
        if "retrieval" not in " ".join(span.attributes)
        else type(span)(
            name=span.name,
            span_id=span.span_id,
            parent_id=span.parent_id,
            trace_id=span.trace_id,
            attributes={
                key: value
                for key, value in span.attributes.items()
                if not key.endswith("document.id")
            },
            service=span.service,
            started=span.started,
        )
        for span in spans
    )

    evidence = read_evidence(_ENTRY, stripped)

    assert evidence.searches == 1
    assert not evidence.passages


def test_the_lane_s_confidence_rides_along_as_text() -> None:
    """Read, printed, and never turned into a dependency on ``chip_chat.search``."""
    assert read_evidence(_ENTRY, turn_spans(_PASSAGES)).confidence == "grounded"
    assert read_evidence(_ENTRY, turn_spans()).confidence == "none"
    assert read_evidence(_ENTRY, turn_spans(declined=True)).confidence is None
