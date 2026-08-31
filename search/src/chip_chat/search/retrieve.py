"""Hybrid retrieval, reranked when there is allowance for it, cited either way.

This is the single interface the tool layer calls — #49's first acceptance
criterion — and :mod:`chip_chat.search.lane` is the thin wrapper that puts a
``retriever.search`` span around it. Three things in here are decisions rather
than plumbing.

**A passage that cannot be cited is not returned.** PRD K2 makes a citation
mandatory on every food or policy claim and gives *menu claims made without a
citation* a target of zero. :mod:`chip_chat.search.documents` already refuses to
index a chunk without a resolvable ``source_url`` and an offset-aware
``harvested_at``, so a hit that arrives without them means the live index
disagrees with the builder — a rebuilt-index-versus-old-schema skew, most
likely. The honest response is not to pass it on uncited: it is dropped, counted
in :attr:`Retrieval.uncitable`, and recorded on the span. "Citations present on
every returned passage" is then a property of the type rather than a hope about
the data.

**Low-scoring results are distinguishable from good ones at the tool boundary**,
which is the behaviour #49 calls a product requirement. :class:`Confidence` is
the three-valued answer, and how it is computed depends on which path ran:

*Reranked.* ``@search.rerankerScore`` is a relevance score on 0-4 and the top
hit is compared against :data:`PROVISIONAL_RERANKER_FLOOR`.

*Degraded.* The fused ``@search.score`` **cannot** do that job, and this is the
subtlety worth spelling out. Hybrid results are ordered by reciprocal rank
fusion, which scores *rank*: the top hit of a query the corpus cannot answer
gets very nearly the same number as the top hit of a perfect match, because both
were first. Thresholding it would produce a confidence signal that is really a
measure of how many results came back. So on the degrade path confidence comes
from :func:`chip_chat.search.query.overlap` — did the keyword half have anything
at all to match — which is cruder than BM25 and, unlike the fusion score, is
actually about the passage.

**Every raw score travels.** :attr:`Passage.score`, :attr:`Passage.reranker_score`
and :attr:`Passage.overlap` are all on the payload and all on the span, so issue
#50 can sweep the floor over recorded runs rather than re-querying to move a
threshold — which matters, because the floor below is the one number in this
module that has not been measured.

**And a hybrid result says whether it was actually hybrid.** The Free tier drops
the vector half of a query and returns HTTP 200 with an empty vector result,
which fuses into a well-formed hybrid response that is silently the keyword
response — ``docs/retrieval.md`` §9. :mod:`chip_chat.search.fusion` reads the
fused scores and says which happened, :attr:`Retrieval.vector_arm` carries the
reading, and :attr:`Retrieval.degraded` is the one-word form of it. The answer
is still served — a lexical-only result is real published data, and the keyword
arm is the second-best arm this repository has measured — but it is served
*saying so*, because the one thing a retrieval that lost half its recall must
never do is assert that the corpus does not cover something.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from chip_chat.search import chunks, fusion
from chip_chat.search import query as query_module
from chip_chat.search.allowance import SemanticAllowance
from chip_chat.search.client import SearchService, ServiceError
from chip_chat.search.fusion import VectorArm
from chip_chat.search.public_url import public_url
from chip_chat.search.schema import ALIAS

__all__ = [
    "MEASURED_GOOD_RERANKER_SCORE",
    "PROVISIONAL_RERANKER_FLOOR",
    "Confidence",
    "Passage",
    "Retrieval",
    "Retriever",
    "is_semantic_refusal",
]

MEASURED_GOOD_RERANKER_SCORE: Final = 1.73
"""The one reranker score this repository has measured on a genuine hit.

``srch-chip-chat-4cy39i``, 2026-08-26, bead ``cc-okc``: a semantic query returned
``@search.rerankerScore`` 1.73 with extractive captions and reordered the hits
over a BM25 top score of 0.55. It is here so that :data:`PROVISIONAL_RERANKER_FLOOR`
has something to be checked against, and ``test_retrieve.py`` asserts the floor
sits below it — a floor that rejected the only good answer anybody has seen would
be a bug with a very quiet failure mode.
"""

PROVISIONAL_RERANKER_FLOOR: Final = 1.5
"""Below this, a reranked result is reported as low confidence rather than good.

**This is the one number in this package that was not measured**, and it is
labelled rather than buried. What is known: the scale runs 0-4, and one genuine
hit scored :data:`MEASURED_GOOD_RERANKER_SCORE`. What is not known is where the
boundary between a real answer and a plausible near-miss actually falls on this
corpus, because that is a question about a golden set and the golden set is
issue #50's.

So this is a placeholder with two properties that keep it from doing harm. It
sits below the only good hit on record, so it cannot reject one. And every raw
score is carried on :class:`Passage` and on the ``retriever.search`` span, so
#50 can choose the real number from recorded runs — nothing has to be re-queried
to move it, and no caller has to be changed. Pass ``floor=`` to
:class:`Retriever` to use a different one today.
"""

_SEMANTIC_REFUSAL_MARKERS: Final[tuple[str, ...]] = (
    "quota",
    "exceed",
    "billing",
    "subscription",
    "not enabled",
    "not supported",
    "disabled",
    "limit",
    "plan",
    "tier",
    "sku",
)
"""Words that, beside *semantic*, mean the reranker was refused rather than broken.

The exact string Azure returns when a Free service passes 1,000 semantic
requests in a month is **not verified here**, because verifying it would mean
spending a month's allowance to read an error message. What is verified is the
consequence, from bead ``cc-okc``: past the ceiling the request fails rather
than being charged for.

So this match is a backstop and :mod:`chip_chat.search.allowance` is the guard.
Both fail safe in the same direction — the fallback is a query without
reranking, which is a valid query that returns real passages, so a false
positive here costs relevance on one turn and a false negative costs one
declined lane. Neither costs a wrong answer.
"""

_MODEL_HIDDEN: Final[frozenset[str]] = frozenset(
    {
        chunks.CHUNK_ID,
        chunks.TEXT,
        chunks.HEADING,
        chunks.KIND,
        chunks.HARVESTED_AT,
        chunks.SOURCE_URL,
        chunks.CITATIONS,
        "character_count",
        "chunked_at",
    }
)
"""Fields :meth:`Retrieval.as_tool_result` does not repeat into ``published``.

Most are already on the passage under their own name. ``source_url`` and
``citations`` are the interesting two: they are the fields a model could paste
into prose, and D9's whole mechanism is that **the model names ids and the app
draws the citation**. The URL travels to the renderer through
:meth:`Retrieval.citations`, which is where a citation can be checked against
what was actually retrieved; it does not travel through the model, which is
where it could not.
"""

_KIND_LABELS: Final[Mapping[str, str]] = {
    "MENU_ITEM": "Menu",
    "POLICY_SECTION": "Policy",
    "FAQ_ENTRY": "FAQ",
    "ALLERGEN_CAVEAT": "Allergens",
    "DOCUMENT_BLOCK": "Document",
    "NUTRITION_ROW": "Nutrition",
}
"""The first half of a citation label — D9's *"Menu · Barbacoa"*.

A label is part of the retrieval payload rather than something the model writes,
so it has to be derived from something the payload carries. ``kind`` is that
something, and it is a closed vocabulary
(:data:`chip_chat.search.chunks.KINDS`), so this mapping cannot fall behind it
without a test noticing.
"""


class Confidence(StrEnum):
    """How much the corpus had to say, as the tool boundary reports it.

    Three values, and the middle one is the one the issue is about: *"a question
    with no answer in the corpus returns an empty or explicitly low-confidence
    result rather than a plausible near-miss."*
    """

    GROUNDED = "grounded"
    """The best passage cleared the bar. An answer may be drawn from these."""

    LOW = "low"
    """Passages came back and none of them cleared it. They are the nearest
    things in the corpus, which is not the same as an answer — RFC-001 §10 and
    PRD K3 both say the honest reply here is that the published data does not
    cover it."""

    NONE = "none"
    """Nothing came back at all, or the lane declined."""


@dataclass(frozen=True, slots=True)
class Passage:
    """One retrieved chunk, with its citation and every score that ranked it.

    Attributes:
        id: The ``chunk_id``. What the response envelope's ``citations`` array
            references, and what makes a minted citation impossible (D9).
        text: The chunk's text, as indexed.
        heading: Its heading, which may be empty — plenty of published policy
            sections have none, and that is a fact about the source.
        kind: One of :data:`chip_chat.search.chunks.KINDS`.
        source_url: Where it was published. Never absent: see the module
            docstring.
        harvested_at: When that page was fetched, offset-aware.
        score: ``@search.score`` — the reciprocal-rank fusion of the two halves.
            A rank score, not a relevance score.
        reranker_score: ``@search.rerankerScore`` on 0-4, or ``None`` when the
            query ran without reranking.
        caption: The sentence the semantic ranker scored, when it returned one.
            D9's *on-demand detail* expands to this.
        overlap: The fraction of the query's content words this passage
            contains. See :func:`chip_chat.search.query.overlap`.
        published: Every other published field the chunk carries.
    """

    id: str
    text: str
    heading: str
    kind: str
    source_url: str
    harvested_at: str
    score: float
    reranker_score: float | None = None
    caption: str | None = None
    overlap: float = 0.0
    published: Mapping[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Return the citation label D9 renders, e.g. ``Menu · Barbacoa``."""
        prefix = _KIND_LABELS.get(self.kind, "Source")
        return f"{prefix} · {self.heading}" if self.heading else prefix

    @property
    def ranking_score(self) -> float:
        """Return the score this passage was ordered by."""
        return self.score if self.reranker_score is None else self.reranker_score

    @property
    def public_url(self) -> str:
        """Return a page a person can read for this passage, or ``""``.

        Distinct from :attr:`source_url`, and the distinction is the whole of
        ``chip-at7``: provenance says where the fact was *read*, which for
        everything behind the ordering API is a JSON endpoint. This says where
        a visitor should be *sent*, and is empty where no published page
        exists. :mod:`chip_chat.search.public_url` carries the rules and the
        measurements behind them.

        Empty string rather than ``None`` because this crosses the wire inside
        a ``dict[str, str]``; the renderer treats empty as "draw the citation
        without a link", which is a designed state.
        """
        return public_url(self.source_url) or ""

    def citation(self) -> dict[str, str]:
        """Return the fields D9's response envelope carries per citation.

        Five now rather than four. ``public_url`` joins the original four
        because the web renderer was making an ``<a href>`` out of
        ``source_url`` and landing visitors on JSON bodies and 404s; the two
        fields answer different questions and only one of them is a link.
        """
        return {
            "id": self.id,
            "label": self.label,
            "source_url": self.source_url,
            "public_url": self.public_url,
            "harvested_at": self.harvested_at,
        }


@dataclass(frozen=True, slots=True)
class Retrieval:
    """What one search found, and everything the caller needs to judge it.

    Attributes:
        query: The words that were searched for.
        passages: The passages, best first, every one of them citable.
        confidence: See :class:`Confidence`.
        reranked: Whether the semantic ranker ordered these. ``False`` is the
            degrade path, not a failure.
        halves: Which halves of the hybrid query ran. Always
            :attr:`~chip_chat.search.query.Halves.HYBRID` on any path that
            serves a visitor; carried so that #50's ablation cannot label a
            single-half run as a hybrid one.
        vector_arm: Which halves actually *answered*, which on the Free tier is
            a different question from which were asked for. See
            :mod:`chip_chat.search.fusion`.
        constraints: What was read out of the query, including what could not
            be applied.
        floor: The reranker floor this result was judged against, so a later
            sweep can re-judge it without re-querying.
        notes: Sentences for the agent. Each one is something it would
            otherwise have to infer from an absence.
        uncitable: Hits dropped for arriving without a source. Should be zero;
            if it is not, the index and the builder disagree.
        declined: Why the lane could not answer at all, or ``None``. Set only by
            :mod:`chip_chat.search.lane`, on the RFC-001 §10 outage path.
    """

    query: str
    passages: tuple[Passage, ...] = ()
    confidence: Confidence = Confidence.NONE
    reranked: bool = False
    halves: query_module.Halves = query_module.Halves.HYBRID
    vector_arm: VectorArm = VectorArm.UNDETERMINED
    constraints: query_module.Constraints = field(
        default_factory=query_module.Constraints
    )
    floor: float = PROVISIONAL_RERANKER_FLOOR
    notes: tuple[str, ...] = ()
    uncitable: int = 0
    declined: str | None = None

    @property
    def answered(self) -> bool:
        """Whether the lane ran at all. ``False`` on an outage."""
        return self.declined is None

    @property
    def grounded(self) -> bool:
        """Whether an answer may be drawn from these passages."""
        return self.confidence is Confidence.GROUNDED

    @property
    def degraded(self) -> bool:
        """Whether the vector half was asked for and did not answer.

        The one-word form of :attr:`vector_arm`, and the thing every consumer of
        this type actually branches on: a degraded retrieval has the recall of
        the keyword arm and the name of the hybrid one, so it may be answered
        from and may not be used to conclude that the corpus is silent.
        """
        return self.vector_arm.degraded

    def citations(self) -> dict[str, dict[str, str]]:
        """Return the citations, keyed by id.

        The mapping :func:`chip_chat.agent.envelope.render` needs: an id the
        model names resolves here or is dropped as a violation, which is how a
        source cannot be minted.

        Returns:
            ``{chunk_id: {id, label, source_url, harvested_at}}``.
        """
        return {passage.id: passage.citation() for passage in self.passages}

    def as_tool_result(self) -> dict[str, Any]:
        """Return what ``search_menu_knowledge`` hands back to the model.

        Carries the ids, the text, the scores and the published fields — and
        **not** ``source_url``, which reaches the visitor through
        :meth:`citations` and the renderer instead. See :data:`_MODEL_HIDDEN`.

        Three keys are about the *health* of the retrieval rather than about
        what it found, and they are three different states the agent has to be
        able to tell apart. ``declined`` means the lane is out and there is
        nothing here at all. ``confidence`` means the lane worked and says how
        much the corpus had to say. ``degraded`` means the lane worked, returned
        real passages, and did so with half of itself missing — which the agent
        can neither infer from the other two nor see in the passages, because a
        lexical-only hybrid result looks exactly like a hybrid one.

        Returns:
            A JSON-ready tool result.
        """
        if self.declined is not None:
            return {
                "declined": "KNOWLEDGE_LANE_UNAVAILABLE",
                "detail": self.declined,
                "notes": list(self.notes),
            }
        return {
            "passages": [
                {
                    "id": passage.id,
                    "label": passage.label,
                    "kind": passage.kind,
                    "text": passage.text,
                    "caption": passage.caption,
                    "score": round(passage.ranking_score, 4),
                    "harvested_at": passage.harvested_at,
                    "published": dict(passage.published),
                }
                for passage in self.passages
            ],
            "confidence": self.confidence.value,
            "reranked": self.reranked,
            "degraded": self.degraded,
            "vector_arm": self.vector_arm.value,
            "notes": list(self.notes),
        }


def is_semantic_refusal(message: str) -> bool:
    """Whether a service error reads as *the reranker was refused*.

    Args:
        message: What the service said.

    Returns:
        ``True`` if the message is about semantic search **and** about an
        allowance, a plan or a subscription — rather than about the query being
        malformed, which is a bug to fix and not a ceiling to degrade past.
    """
    lowered = message.casefold()
    if "semantic" not in lowered:
        return False
    return any(marker in lowered for marker in _SEMANTIC_REFUSAL_MARKERS)


class Retriever:
    """Hybrid retrieval over the corpus alias. The interface the tool layer calls.

    Holds its :class:`~chip_chat.search.client.SearchService` — and therefore
    the HTTP connection pool inside it — for its whole lifetime, which is the
    single decision that dominates this path's latency. Measured from the
    container app against the service on 2026-08-26: a hybrid query on a warm
    pooled connection is 11.2 ms at p50; the same query on a fresh TLS
    connection is 84.3 ms. The cross-region hop everybody worries about is
    6.8 ms of that. Build one :class:`Retriever` per process, not one per turn,
    and see :func:`chip_chat.search.client.pooled_client`.
    """

    __slots__ = ("_alias", "_allowance", "_floor", "_service", "_top")

    def __init__(
        self,
        service: SearchService,
        *,
        alias: str = ALIAS,
        allowance: SemanticAllowance | None = None,
        top: int = query_module.TOP,
        floor: float = PROVISIONAL_RERANKER_FLOOR,
    ) -> None:
        """Initialise the retriever.

        Args:
            service: The search service. Never an index name below this line —
                the application knows the alias and nothing else.
            alias: The alias to query.
            allowance: The month's semantic requests. A fresh in-memory
                allowance by default, which is right for one long-lived process
                and wrong for a batch of short ones; see
                :mod:`chip_chat.search.allowance`.
            top: Passages to return.
            floor: The reranker score at or above which a result is grounded.
        """
        self._service = service
        self._alias = alias
        self._allowance = SemanticAllowance() if allowance is None else allowance
        self._top = top
        self._floor = floor

    @property
    def alias(self) -> str:
        """The alias this retriever queries."""
        return self._alias

    @property
    def allowance(self) -> SemanticAllowance:
        """The semantic request counter, for a span or a status command."""
        return self._allowance

    def search(
        self,
        text: str,
        *,
        top: int | None = None,
        constraints: query_module.Constraints | None = None,
        rerank: bool = True,
        halves: query_module.Halves = query_module.Halves.HYBRID,
    ) -> Retrieval:
        """Search the corpus and return citable passages, best first.

        Args:
            text: The visitor's words, as the tool received them.
            top: Passages to return, defaulting to the retriever's.
            constraints: Constraints to apply instead of reading them out of
                ``text``. For the eval harness, which wants to hold the query
                fixed and vary the filter.
            rerank: Whether to ask for semantic reranking at all. ``False``
                exercises the degrade path deliberately — which is the point of
                being able to say so, since the path is otherwise reached only
                by a ceiling nobody wants to hit to test it.
            halves: Which halves of the hybrid query to send. The default is the
                only value any serving path may pass; the other two exist for
                #50's ablation and :class:`chip_chat.search.query.Halves` is
                where the argument for them lives.

        Returns:
            The retrieval. Never ``None`` and never partially populated: a
            search that found nothing is :attr:`Confidence.NONE` with no
            passages.

            **A retrieval whose vector half was dropped is returned, not
            retried and not refused.** It carries
            :attr:`Retrieval.vector_arm` as
            :attr:`~chip_chat.search.fusion.VectorArm.DROPPED`,
            two notes that say so, and passages that are real published data —
            the keyword arm is the second-best of #50's four and measured 84%
            recall on three consecutive sweeps. Retrying is deliberately not
            done here and ``docs/decisions/vector-arm-degradation.md`` is the
            argument: the fault does not clear in minutes, so a retry buys about
            one recovery in eight on a hot service, and on the reranked path it
            buys it with a second of the month's 1,000 semantic requests.

        Raises:
            chip_chat.search.errors.SearchError: If the service refuses for any
                reason other than a spent semantic allowance. RFC-001 §10's
                outage path; :class:`chip_chat.search.lane.KnowledgeLane` is
                what turns it into a declining lane rather than a failing turn.
        """
        wanted = self._top if top is None else top
        narrowing = query_module.read(text) if constraints is None else constraints
        notes: list[str] = [*narrowing.notes, *narrowing.unapplied]

        reranked = rerank and self._allowance.spend()
        if rerank and not reranked:
            notes.append(_DEGRADED_NOTE)
        request = query_module.body(
            text, constraints=narrowing, top=wanted, rerank=reranked, halves=halves
        )
        try:
            response = self._service.search(self._alias, request)
        except ServiceError as error:
            if not reranked or not is_semantic_refusal(str(error)):
                raise
            # The service is the authority on its own allowance and it has just
            # disagreed with the counter. Degrade for the rest of the month
            # rather than for this turn: whatever spent the allowance is not
            # going to give it back before the first.
            self._allowance.exhaust(str(error))
            notes.append(_DEGRADED_NOTE)
            reranked = False
            response = self._service.search(
                self._alias,
                query_module.body(
                    text,
                    constraints=narrowing,
                    top=wanted,
                    rerank=False,
                    halves=halves,
                ),
            )

        passages, uncitable = self._passages(text, response)
        arm = fusion.contribution(
            halves=halves,
            scores=[passage.score for passage in passages],
            filtered=narrowing.filtered,
        )
        confidence = self._confidence(passages, reranked=reranked)
        notes.extend(_degraded_notes(arm, confidence))
        notes.extend(_confidence_notes(confidence, narrowing, arm))
        if uncitable:
            notes.append(
                f"{uncitable} passage(s) were dropped for arriving without a "
                f"source. The live index and the build disagree about the chunk "
                f"schema; this is a defect, not a data gap."
            )
        return Retrieval(
            query=text,
            passages=passages,
            confidence=confidence,
            reranked=reranked,
            halves=halves,
            vector_arm=arm,
            constraints=narrowing,
            floor=self._floor,
            notes=tuple(notes),
            uncitable=uncitable,
        )

    def _passages(
        self, text: str, response: Mapping[str, Any]
    ) -> tuple[tuple[Passage, ...], int]:
        """Turn a search response into citable passages, best first."""
        words = query_module.terms(text)
        found: list[Passage] = []
        uncitable = 0
        hits: Sequence[Mapping[str, Any]] = response.get("value", [])
        for hit in hits:
            passage = _passage(hit, words)
            if passage is None:
                uncitable += 1
            else:
                found.append(passage)
        # The service already orders semantic results by reranker score, but
        # sorting here is what makes the payload's order and the scores it
        # carries the same claim -- including on the degrade path, where the
        # fusion order is the service's and nothing re-reads it.
        found.sort(key=lambda passage: passage.ranking_score, reverse=True)
        return tuple(found), uncitable

    def _confidence(self, passages: Sequence[Passage], *, reranked: bool) -> Confidence:
        """Judge the best passage. See the module docstring for the two rules."""
        if not passages:
            return Confidence.NONE
        if reranked:
            score = passages[0].reranker_score
            if score is None:
                # A semantic query that came back without a reranker score is
                # not a good result with a missing number; it is a query that
                # did not get reranked and did not say so.
                return Confidence.LOW
            return Confidence.GROUNDED if score >= self._floor else Confidence.LOW
        # Across the whole result set rather than the top hit alone. The
        # question the lexical floor answers -- did the keyword half have
        # anything at all to match -- is a property of what came back, and on
        # this path the order it came back in is a *rank* fusion that the
        # keyword half only partly decided.
        return (
            Confidence.GROUNDED
            if max(passage.overlap for passage in passages) > 0.0
            else Confidence.LOW
        )


_DEGRADED_NOTE: Final = (
    "Reranking is off — the Free tier's monthly semantic allowance is spent — so "
    "these passages are ordered by hybrid fusion alone and are ranked less well "
    "than usual. Answer from them anyway; they are the published data."
)

_LOW_CONFIDENCE_NOTE: Final = (
    "Low confidence: these are the nearest passages in the corpus, not an answer "
    "to the question. If none of them actually says it, say the published data "
    "does not cover it rather than paraphrasing over the gap."
)

_EMPTY_NOTE: Final = (
    "Nothing in the published corpus matched. Say so. Do not answer from general "
    "knowledge about the restaurant."
)

_EMPTY_FILTERED_NOTE: Final = (
    "Nothing in the published corpus matched *with the filter applied*, which is "
    "a finding rather than a gap: no published item meets what was asked for."
)

_LEXICAL_ONLY_NOTE: Final = (
    "DEGRADED RETRIEVAL: only the keyword half of the search ran. The search "
    "service dropped the vector half and reported that as a success, so these "
    "passages are what a word match found and anything the published pages say "
    "in different words than the question used was never looked at. Answer from "
    "what is here if it genuinely answers the question. Do NOT say the "
    "restaurant does not publish something — half the retriever did not look, "
    "and an absence measured through it is not an absence. Say instead that the "
    "search is only partly working and offer to try again."
)

_LEXICAL_ONLY_ABSENCE_NOTE: Final = (
    "And nothing that came back cleared the bar, which on a degraded retrieval "
    "is evidence about the retrieval rather than about the corpus. This is the "
    "case to say out loud: the lookup is not working properly right now, so you "
    "cannot answer this one and are not concluding anything from the silence."
)


def _degraded_notes(arm: fusion.VectorArm, confidence: Confidence) -> tuple[str, ...]:
    """Return the sentences that go with a vector half that did not answer.

    Two, and the second one only where it applies. The first says what happened
    and forbids the one inference a lexical-only result cannot support. The
    second fires when nothing cleared the bar either, which is the combination
    that would otherwise reach a visitor as a confident *"the published data
    does not cover that"* — the exact sentence a retriever missing half its
    recall has no standing to say.

    Args:
        arm: What :mod:`chip_chat.search.fusion` read off the fused scores.
        confidence: How much the passages that did come back had to say.

    Returns:
        The notes, empty on a healthy retrieval.
    """
    if not arm.degraded:
        return ()
    if confidence is Confidence.GROUNDED:
        return (_LEXICAL_ONLY_NOTE,)
    return (_LEXICAL_ONLY_NOTE, _LEXICAL_ONLY_ABSENCE_NOTE)


def _confidence_notes(
    confidence: Confidence,
    constraints: query_module.Constraints,
    arm: fusion.VectorArm = fusion.VectorArm.CONTRIBUTED,
) -> tuple[str, ...]:
    """Return the sentences that go with a confidence, for the agent to read.

    The ``arm`` argument is here to *withhold* a sentence rather than to add
    one. Both of the notes below tell the agent something about the corpus —
    that nothing in it matched, or that these are the nearest things in it — and
    neither claim is available from a retrieval whose vector half never ran.
    :func:`_degraded_notes` has already said the true thing in that case, and
    saying both would hand the agent one instruction and its contradiction.
    """
    if arm.degraded:
        return ()
    if confidence is Confidence.NONE:
        return (_EMPTY_FILTERED_NOTE if constraints.filtered else _EMPTY_NOTE,)
    if confidence is Confidence.LOW:
        return (_LOW_CONFIDENCE_NOTE,)
    return ()


def _passage(hit: Mapping[str, Any], words: frozenset[str]) -> Passage | None:
    """Return one passage, or ``None`` if it arrived without a citation."""
    chunk_id = str(hit.get(chunks.CHUNK_ID, "") or "")
    source_url = str(hit.get(chunks.SOURCE_URL, "") or "")
    harvested_at = str(hit.get(chunks.HARVESTED_AT, "") or "")
    if not chunk_id or not source_url or not harvested_at:
        return None
    text = str(hit.get(chunks.TEXT, "") or "")
    heading = str(hit.get(chunks.HEADING, "") or "")
    published = {
        name: value
        for name, value in hit.items()
        if not name.startswith("@") and name not in _MODEL_HIDDEN
    }
    return Passage(
        id=chunk_id,
        text=text,
        heading=heading,
        kind=str(hit.get(chunks.KIND, "") or ""),
        source_url=source_url,
        harvested_at=harvested_at,
        score=float(hit.get("@search.score", 0.0) or 0.0),
        reranker_score=_optional_float(hit.get("@search.rerankerScore")),
        caption=_caption(hit),
        overlap=query_module.overlap(
            words,
            " ".join(str(hit.get(name, "") or "") for name in sorted(chunks.SEARCHABLE)),
        ),
        published=published,
    )


def _optional_float(value: Any) -> float | None:
    """Return ``value`` as a float, or ``None`` if it was absent or unreadable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _caption(hit: Mapping[str, Any]) -> str | None:
    """Return the first extractive caption on a hit, if the reranker sent one."""
    captions = hit.get("@search.captions")
    if not isinstance(captions, Sequence) or isinstance(captions, str | bytes):
        return None
    for caption in captions:
        if isinstance(caption, Mapping):
            text = str(caption.get("text", "") or "")
            if text:
                return text
    return None
