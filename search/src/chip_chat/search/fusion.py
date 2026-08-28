"""Did the vector half actually run? The fused score is the only thing that says.

This module exists because of one defect, and it is worth stating the defect
before the arithmetic. Free-tier vector search on ``srch-chip-chat-4cy39i``
returns ``{"value": []}`` with **HTTP 200** for a vector query, no error and no
warning, at a rate that climbs from roughly a quarter of queries on a rested
service to roughly nine in ten after a few dozen. ``docs/retrieval.md`` §9 is the
investigation: the vectorizer, the embedding deployment, four API versions,
rescoring, quantization, ``k`` and every service quota were each eliminated by a
run rather than by an argument, and what is left is the tier.

**A hybrid query does not report which ranker found a document.** Azure AI
Search fuses the lexical and vector orders by reciprocal rank and returns one
``@search.score`` per hit, and that response is well formed whether one ranker
contributed or two. So when the vector half is dropped the application receives
a perfectly ordinary hybrid response that is silently the *keyword* response --
which is exactly what happened to three committed ablation sweeps, where
``hybrid`` came out identical to ``keyword only`` in every single cell and was
read as a finding about embeddings rather than as a service fault.

**There is a tell, and it is arithmetic rather than reported.** Reciprocal rank
fusion gives a document ``1/(k + rank)`` from each ranker that placed it and
sums the terms. Azure's ``k`` is 60 and its rank is **zero-based** -- measured,
not assumed; see :data:`RRF_K`. So a document that exactly one ranker placed can
score at most ``1/60``, taking the best rank that ranker has to give, and a
document both rankers placed scores strictly more than that. One number
separates the two cases, and it is :data:`SINGLE_RANKER_CEILING`.

Measured against the live alias on 2026-08-27, the same question one second
apart::

    healthy   0.033060  0.031754  0.031746  0.031319  0.031054
    degraded  0.016667  0.016393  0.016129  0.015873  0.015625

The second row is ``1/60, 1/61, 1/62, 1/63, 1/64`` to every digit the service
prints. It is not a weak vector half. It is no vector half.

**The check is over every returned passage, not over the top one.** On the
reranked arm the service reorders by ``@search.rerankerScore``, so the passage
printed first may be one that only BM25 placed even while the vector half was
working normally -- observed on the live alias in the same session, top hit at
``1/60`` with two-ranker scores at ranks four and five. Reading the first score
alone would report that healthy query as degraded. The maximum over what came
back is the honest reading: *some* document was placed by both halves, therefore
both halves ran.

**What this refuses to claim, and why that matters more than what it claims.**
The eval's negative set is eight questions the published corpus cannot answer,
and a detector that called those degraded would make the one measurement that
distinguishes restraint from a fault useless. So:

*A question the corpus cannot answer still comes back full.* A nearest-neighbour
search does not return nothing for a question it has never heard of -- it
returns its nearest neighbours anyway, which is precisely why the vector arm is
not a restraint mechanism. Every negative-set question this repository has run
against a healthy service came back with five passages and low confidence, so
the detector reads a two-ranker score on all of them and is silent.

*An empty result set is never, on its own, called a fault.* A filtered query can
legitimately match nothing -- ``"under 500 calories and no dairy"`` -- and an
index that is empty or newly built produces the same response for a different
reason again. Neither is distinguishable from a dropped vector half inside one
response, so a **hybrid** query that returns nothing at all is
:attr:`VectorArm.UNDETERMINED` and :attr:`VectorArm.DROPPED` is only ever
claimed from the arithmetic, which is a proof.

That gives up one case and it is worth naming: a hybrid query whose vector half
was dropped *and* whose lexical half matched nothing would be read as
undetermined. It has not been observed -- every degraded query measured against
the live alias returned a full five BM25 passages -- and it is the one shape of
this defect that is already safe, because zero passages is
:attr:`~chip_chat.search.retrieve.Confidence.NONE` and nothing downstream builds
a confident answer out of no passages.

*The single-half arms are different, and only one of them is read this way.*
:attr:`~chip_chat.search.query.Halves.VECTOR` is the whole query rather than
half of one, so there the returned count *is* the signal and an empty unfiltered
response is the fault -- which is what #50's sweeps recorded as a vector arm
scoring 7%. That arm is on no serving path and runs only beside the other three
against the same index, so the *index is empty* reading is ruled out by its
neighbours rather than by this function.

**The precondition, stated because the tell depends on it.**
:data:`chip_chat.search.query.VECTOR_CANDIDATES` is 50 and this corpus holds 31
chunks, so a working vector half places *every* document and any document the
fusion returns was placed by both. That makes the reading a proof here. On a
corpus larger than ``k`` it degrades to a strong heuristic whose false positive
is a result set in which the two halves agreed on nothing at all -- which is not
a normal state either, and is worth the same look.
"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Final

from chip_chat.search.query import Halves

__all__ = [
    "RRF_K",
    "SINGLE_RANKER_CEILING",
    "VectorArm",
    "contribution",
    "fused_by_both",
    "placed_by_both",
]

RRF_K: Final = 60
"""Reciprocal rank fusion's smoothing constant, as Azure AI Search uses it.

Documented by Microsoft and confirmed against the live alias: a hybrid query
whose vector half had been dropped returned the exact sequence ``1/60, 1/61,
1/62, 1/63, 1/64``. That sequence also settles a detail the documentation leaves
ambiguous -- the rank is **zero-based**, so the best score one ranker can award
is ``1/60`` and not ``1/61``. The difference is the whole of
:data:`SINGLE_RANKER_CEILING`, and getting it wrong by one would put the
threshold a hair below the value a degraded query actually returns and report
every degraded query as healthy.
"""

SINGLE_RANKER_CEILING: Final = 1.0 / RRF_K
"""The most a document placed by exactly one ranker can score. ``0.016666...``

Strictly above it means two rankers contributed, which means the vector half
ran. At or below it means no returned document was placed by both, which on this
corpus means the vector half returned nothing at all -- see the module
docstring's precondition.
"""

_TOLERANCE: Final = 1e-6
"""Relative slack on the comparison, because the service answers in float32.

The live service returns the ceiling itself as ``0.01666666753590107`` -- a
single-precision ``1/60`` widened to a double, and larger than the exact value
by 9e-10. A bare ``>`` would read that as proof of a second ranker and report
every degraded query as healthy, which is the failure mode this whole module
exists to remove. A relative tolerance of 1e-6 sits an order of magnitude above
float32's own precision and four orders below the margin that matters: the
*smallest* two-ranker score reachable at ``k = 50`` is ``2/110``, which clears
the ceiling by nine per cent.
"""


class VectorArm(StrEnum):
    """Whether the vector half of a hybrid query contributed to the result.

    Four values rather than a boolean, because *"it did not"* and *"there is
    nothing here that could say"* are different claims and only the first one is
    a defect. Collapsing them is how the negative set would start reporting a
    service fault eight times a run.
    """

    CONTRIBUTED = "contributed"
    """A returned document was placed by both rankers. The vector half ran."""

    DROPPED = "dropped"
    """No returned document was placed by both rankers. The vector half returned
    nothing and the service called that a success. This result is lexical-only:
    it is the *keyword* arm wearing the hybrid arm's name."""

    NOT_SENT = "not_sent"
    """The query carried no vector half. True of
    :attr:`chip_chat.search.query.Halves.KEYWORD`, which is #50's control and is
    on no serving path -- nothing is wrong and nothing degraded."""

    UNDETERMINED = "undetermined"
    """Nothing came back, so there is no score to read and no claim to make. A
    filter that matches no published item is a finding and an index with nothing
    in it is an outage; neither is this defect, and neither is separable from it
    inside one response. Calling any of them ``DROPPED`` would put a defect
    report on top of a correct answer, which is how a detector stops being
    believed."""

    @property
    def degraded(self) -> bool:
        """Whether this result lost half its recall without being told so."""
        return self is VectorArm.DROPPED


def fused_by_both(score: float) -> bool:
    """Whether one fused score proves two rankers placed that document.

    Args:
        score: A hybrid query's ``@search.score``. Meaningless on a single-half
            query, where the score is BM25 or a cosine similarity rather than a
            fusion -- :func:`contribution` is what knows the difference.

    Returns:
        Whether the score exceeds :data:`SINGLE_RANKER_CEILING` by more than the
        service's own float32 noise.
    """
    return score > SINGLE_RANKER_CEILING * (1.0 + _TOLERANCE)


def placed_by_both(halves: Halves, score: float) -> bool | None:
    """Whether one document was placed by both rankers, where that is a question.

    The per-document form of :func:`fused_by_both`, guarded by the arm. It is
    what the ``retriever.search`` span records against each passage, so a trace
    shows *which* passages the vector half placed rather than only that some
    were — the difference between reading a defect and inferring one.

    Args:
        halves: Which halves the request asked for.
        score: That document's ``@search.score``.

    Returns:
        The reading, or ``None`` on a single-half query, where the score is a
        BM25 score or a cosine similarity and the threshold means nothing.
        ``None`` is *not applicable* and must not be read as *no*.
    """
    return fused_by_both(score) if halves is Halves.HYBRID else None


def contribution(*, halves: Halves, scores: Sequence[float], filtered: bool) -> VectorArm:
    """Read whether the vector half ran, from the scores it left behind.

    Args:
        halves: Which halves the request asked for. The reading is only about a
            query that asked for a vector half at all.
        scores: Every returned hit's ``@search.score``, in any order. Every one
            of them, not the first: the reranked arm reorders by relevance and
            its top hit is regularly one that only BM25 placed.
        filtered: Whether the request carried an OData ``$filter``. The one
            thing that makes an empty result set from a *vector-only* query
            unreadable rather than damning.

    Returns:
        The :class:`VectorArm` reading. Never raises and never guesses:
        :attr:`VectorArm.UNDETERMINED` is available and is the right answer more
        often than a detector's author would like.
    """
    if halves is Halves.KEYWORD:
        return VectorArm.NOT_SENT
    if halves is Halves.VECTOR:
        # No fusion, so no arithmetic -- but this arm has the simpler tell and
        # it is the one #50's sweep needed: a nearest-neighbour search returns
        # neighbours for any question it is asked, so an unfiltered vector query
        # that came back with nothing did not run.
        if scores:
            return VectorArm.CONTRIBUTED
        return VectorArm.UNDETERMINED if filtered else VectorArm.DROPPED
    if not scores:
        # A fused response with nothing in it has no scores to read, and a
        # filter, an empty index and a dropped vector half all produce it. See
        # the module docstring: DROPPED is claimed from proof or not at all.
        return VectorArm.UNDETERMINED
    return (
        VectorArm.CONTRIBUTED
        if any(fused_by_both(score) for score in scores)
        else VectorArm.DROPPED
    )
