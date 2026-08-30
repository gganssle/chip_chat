"""The arithmetic tell, held to the numbers the live service actually returned.

Every constant asserted here was measured against `srch-chip-chat-4cy39i` on
2026-08-27 rather than derived from documentation, because the one detail the
documentation does not settle is whether reciprocal rank fusion's rank is zero-
or one-based — and that detail is the whole threshold. Off by one and the
threshold sits a hair below the value a degraded query returns, so the detector
reports every degraded query as healthy and nobody ever finds out.

The live rows this file is written against, taken seconds apart in one run:

    healthy   0.033060  0.031754  0.031746  0.031319  0.031054
    degraded  0.016667  0.016393  0.016129  0.015873  0.015625

They are recorded below at full precision rather than at the six decimal places
that read nicely, and that is not fastidiousness either. The service answers in
single precision, so its ``1/60`` arrives as ``0.01666666753590107`` — *above*
the exact value. A threshold written against the rounded number would be a
threshold the real number steps over.
"""

import pytest

from chip_chat.search.fusion import (
    RRF_K,
    SINGLE_RANKER_CEILING,
    VectorArm,
    contribution,
    fused_by_both,
    placed_by_both,
    tell,
)
from chip_chat.search.query import Halves

LIVE_HEALTHY_HYBRID = (
    0.03306011110544205,
    0.0317540317773819,
    0.0317460335791111,
    0.03131881356239319,
    0.031054403632879257,
)
"""A hybrid response from the live alias whose vector half answered.
``srch-chip-chat-4cy39i``, 2026-08-27, *"how do rewards points work"*."""

LIVE_DEGRADED_HYBRID = (
    0.01666666753590107,
    0.016393441706895828,
    0.016129031777381897,
    0.01587301678955555,
    0.015625,
)
"""Four different questions in the same run returned this sequence byte for
byte. It is ``1/60, 1/61, 1/62, 1/63, 1/64`` in single precision, which is what
a result set ordered by one ranker looks like when the fusion has nothing else
to add: the ranks themselves, and no relevance anywhere in the numbers."""

LIVE_FLOAT32_CEILING = LIVE_DEGRADED_HYBRID[0]
"""``1/60`` as the service actually sends it: a float32 widened to a double, and
larger than the exact value. The reason the comparison carries a tolerance."""


# --- The threshold -----------------------------------------------------------


def test_the_ceiling_is_the_best_score_one_ranker_can_award() -> None:
    # Zero-based rank: the top hit of a single-ranker result set scores 1/60,
    # not 1/61. Measured, not assumed -- see the module docstring.
    assert SINGLE_RANKER_CEILING == 1.0 / RRF_K
    assert LIVE_DEGRADED_HYBRID[0] == pytest.approx(SINGLE_RANKER_CEILING, abs=1e-6)


def test_the_ceiling_the_service_sends_is_not_read_as_two_rankers() -> None:
    # The float32 widening puts the service's own 1/60 *above* the exact value.
    # A bare `>` here would call every degraded query healthy, which is the
    # failure mode this module exists to remove.
    assert LIVE_FLOAT32_CEILING > SINGLE_RANKER_CEILING
    assert not fused_by_both(LIVE_FLOAT32_CEILING)


def test_the_smallest_reachable_two_ranker_score_still_clears_the_ceiling() -> None:
    # Worst case at k=50: a document placed last by both halves. If this did not
    # clear the ceiling the detector would have a blind spot at the bottom of
    # the result set rather than a threshold.
    worst = 2.0 / (RRF_K + 50)
    assert fused_by_both(worst)


@pytest.mark.parametrize("score", LIVE_HEALTHY_HYBRID)
def test_every_score_of_a_healthy_live_response_reads_as_two_rankers(
    score: float,
) -> None:
    assert fused_by_both(score)


@pytest.mark.parametrize("score", LIVE_DEGRADED_HYBRID)
def test_no_score_of_a_degraded_live_response_reads_as_two_rankers(
    score: float,
) -> None:
    assert not fused_by_both(score)


# --- The reading -------------------------------------------------------------


def test_a_healthy_hybrid_response_says_the_vector_half_contributed() -> None:
    assert (
        contribution(halves=Halves.HYBRID, scores=LIVE_HEALTHY_HYBRID, filtered=False)
        is VectorArm.CONTRIBUTED
    )


def test_a_degraded_hybrid_response_says_the_vector_half_dropped() -> None:
    assert (
        contribution(halves=Halves.HYBRID, scores=LIVE_DEGRADED_HYBRID, filtered=False)
        is VectorArm.DROPPED
    )


def test_one_two_ranker_score_anywhere_in_the_set_is_enough() -> None:
    # Measured on the live alias: a reranked response whose *top* hit scored
    # 1/60 carried two-ranker scores at ranks four and five, because the
    # semantic ranker reorders by relevance rather than by fusion. Reading the
    # first score alone would report that healthy query as degraded.
    scores = (
        0.01666666753590107,
        0.014285714365541935,
        0.011904762126505375,
        0.03154495730996132,
        0.02765064872801304,
    )
    assert (
        contribution(halves=Halves.HYBRID, scores=scores, filtered=False)
        is VectorArm.CONTRIBUTED
    )


def test_a_keyword_arm_has_no_vector_half_to_lose() -> None:
    # Its @search.score is BM25 -- 34.6 on the live alias -- and running the
    # threshold over that would answer a question nobody asked.
    assert (
        contribution(halves=Halves.KEYWORD, scores=(34.6, 20.5), filtered=False)
        is VectorArm.NOT_SENT
    )
    assert (
        contribution(halves=Halves.KEYWORD, scores=(), filtered=False)
        is VectorArm.NOT_SENT
    )


def test_a_vector_arm_that_answered_is_read_from_the_count_not_the_score() -> None:
    # Cosine similarities, not fused scores. All of them are far above the
    # ceiling and none of them means anything about fusion; what means something
    # is that five of them exist.
    assert (
        contribution(halves=Halves.VECTOR, scores=(0.705, 0.695, 0.670), filtered=False)
        is VectorArm.CONTRIBUTED
    )


def test_a_vector_arm_that_returned_nothing_dropped() -> None:
    # `{"value": []}` with HTTP 200 from an unfiltered vector query. A nearest-
    # neighbour search returns neighbours for any question it is asked.
    assert (
        contribution(halves=Halves.VECTOR, scores=(), filtered=False) is VectorArm.DROPPED
    )


# --- What it refuses to claim ------------------------------------------------


def test_an_empty_filtered_result_is_not_called_a_fault() -> None:
    # "under 500 calories and no dairy" can legitimately match no published
    # item. A detector that reported that as a service defect would put a defect
    # report on top of a correct answer.
    for halves in (Halves.HYBRID, Halves.VECTOR):
        assert (
            contribution(halves=halves, scores=(), filtered=True)
            is VectorArm.UNDETERMINED
        )


def test_an_empty_hybrid_result_is_undetermined_rather_than_dropped() -> None:
    # An empty index produces this too, and so does a lexical half that matched
    # nothing beside a dropped vector half. None of the three is separable from
    # the others inside one response, so DROPPED is claimed from the arithmetic
    # or not at all -- and the case given up is already safe, because no
    # passages is Confidence.NONE and nothing builds a confident answer on that.
    assert (
        contribution(halves=Halves.HYBRID, scores=(), filtered=False)
        is VectorArm.UNDETERMINED
    )


def test_a_per_document_reading_is_withheld_on_a_single_half_query() -> None:
    # The span carries one of these per passage. On a keyword-only arm the score
    # is BM25 -- 34.6 on the live alias -- and a `False` there would read as
    # "the vector half did not place this", which is true and misleading.
    # `None` is not applicable and must not be read as no.
    assert placed_by_both(Halves.HYBRID, LIVE_HEALTHY_HYBRID[0]) is True
    assert placed_by_both(Halves.HYBRID, LIVE_DEGRADED_HYBRID[0]) is False
    assert placed_by_both(Halves.KEYWORD, 34.6) is None
    assert placed_by_both(Halves.VECTOR, 0.705) is None


def test_only_dropped_counts_as_degraded() -> None:
    # The eval scores a degraded question unscored and the agent is told it
    # cannot conclude an absence. Both are wrong on a filter and on an
    # unanswerable question, which is why `degraded` is not `is not CONTRIBUTED`.
    assert VectorArm.DROPPED.degraded
    assert not VectorArm.CONTRIBUTED.degraded
    assert not VectorArm.NOT_SENT.degraded
    assert not VectorArm.UNDETERMINED.degraded


# --- The span-facing reading -------------------------------------------------
#
# `tell` adds numbers to a verdict `contribution` has already reached; it never
# reaches a second one. These tests are about the three cases the attributes
# have to keep apart -- the vector half contributed nothing, nothing matched at
# all, and this was never a hybrid query -- because a dashboard that cannot tell
# those apart is a dashboard that reports the negative set as an outage.


def test_a_degraded_hybrid_result_is_flagged_with_its_evidence() -> None:
    reading = tell(
        arm=VectorArm.DROPPED, halves=Halves.HYBRID, scores=LIVE_DEGRADED_HYBRID
    )
    assert reading.single_ranker is True
    assert reading.documents == len(LIVE_DEGRADED_HYBRID)
    # The float32 1/60 the service actually sends, carried unrounded: the whole
    # point of putting the number beside the verdict is that a later reader can
    # re-judge it, and a rounded number cannot be re-judged.
    assert reading.top_score == LIVE_FLOAT32_CEILING
    assert reading.ceiling == SINGLE_RANKER_CEILING


def test_a_healthy_hybrid_result_is_not_flagged() -> None:
    reading = tell(
        arm=VectorArm.CONTRIBUTED, halves=Halves.HYBRID, scores=LIVE_HEALTHY_HYBRID
    )
    assert reading.single_ranker is False
    assert reading.top_score == max(LIVE_HEALTHY_HYBRID)
    assert reading.ceiling == SINGLE_RANKER_CEILING


def test_the_top_score_is_the_maximum_and_not_the_first() -> None:
    # The reranked arm reorders by relevance, so the first score printed is
    # regularly one only BM25 placed. Reading it would flag a healthy query.
    scores = (
        0.01666666753590107,
        0.014285714365541935,
        0.011904762126505375,
        0.03154495730996132,
        0.02765064872801304,
    )
    reading = tell(arm=VectorArm.CONTRIBUTED, halves=Halves.HYBRID, scores=scores)
    assert reading.top_score == 0.03154495730996132
    assert reading.single_ranker is False


def test_a_keyword_only_query_carries_no_fusion_arithmetic_at_all() -> None:
    # BM25 scores. Running the ceiling over them would answer a question nobody
    # asked, and answering it False would say "healthy" about a query that has
    # no vector half to be healthy about.
    reading = tell(arm=VectorArm.NOT_SENT, halves=Halves.KEYWORD, scores=(34.6, 20.5))
    assert reading.single_ranker is None
    assert reading.top_score is None
    assert reading.ceiling is None
    assert reading.documents == 2


def test_a_vector_only_query_carries_no_fusion_arithmetic_either() -> None:
    # Cosine similarities. Every one of them clears 1/60 by two orders of
    # magnitude, so a threshold applied here would read "healthy" from a number
    # that is not a fused score -- including on the empty-response case that is
    # the whole reason that arm is instrumented.
    reading = tell(arm=VectorArm.CONTRIBUTED, halves=Halves.VECTOR, scores=(0.705, 0.695))
    assert reading.single_ranker is None
    assert reading.top_score is None
    assert reading.ceiling is None

    empty = tell(arm=VectorArm.DROPPED, halves=Halves.VECTOR, scores=())
    assert empty.single_ranker is None
    assert empty.documents == 0


def test_nothing_matched_at_all_is_a_count_and_not_a_flag() -> None:
    # A filter that matches no published item, an index with nothing in it and a
    # dropped vector half beside a lexical half that matched nothing all produce
    # this. The count says the result set was empty; the tell stays silent,
    # because there is no score to read and therefore nothing to prove.
    reading = tell(arm=VectorArm.UNDETERMINED, halves=Halves.HYBRID, scores=())
    assert reading.documents == 0
    assert reading.single_ranker is None
    assert reading.top_score is None
    assert reading.ceiling is None


def test_the_verdict_is_carried_rather_than_recomputed() -> None:
    # `tell` adds numbers and never a verdict: whatever arm the retrieval
    # reached is the arm the span reports, so the trace cannot contradict the
    # tool result it describes.
    for arm in VectorArm:
        assert tell(arm=arm, halves=Halves.HYBRID, scores=LIVE_HEALTHY_HYBRID).arm is arm
