"""The attribute vocabulary that rides on the spans of :mod:`chip_chat.otel.schema`.

Three sources, in strict order of precedence:

1. **OpenInference** (:class:`SpanAttributes` and friends, re-exported below).
   These are what make Arize and Phoenix read a span as an LLM call, a retrieval
   or a tool invocation rather than as an anonymous unit of work. Where
   OpenInference defines a name, that name is used and no alternative is offered.
2. **OpenTelemetry's database conventions**, for the generated SQL and row count
   on ``db.cortex_analyst``. OpenInference has nothing to say about SQL, and the
   OTel names already exist, so they are spelled out here rather than invented.
3. **Chip Chat's own namespace** (:class:`ChipChatAttributes`), for the handful
   of facts neither standard covers -- turn index, guard outcomes, matcher slot
   confidences, ops confirmation state, and whether the retrieval that produced
   ``retrieval.documents.*`` had both of its rankers.

Nothing here is a free-form key. Call sites reach these through the recorders in
:mod:`chip_chat.otel.spans` and generally never name an attribute at all.
"""

from typing import Final

from openinference.semconv.trace import (
    DocumentAttributes,
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    OpenInferenceMimeTypeValues,
    OpenInferenceSpanKindValues,
    SpanAttributes,
    ToolCallAttributes,
)

__all__ = [
    "ChipChatAttributes",
    "ConfirmationState",
    "DbAttributes",
    "DocumentAttributes",
    "GuardOutcome",
    "ImageAttributes",
    "MessageAttributes",
    "MessageContentAttributes",
    "OpenInferenceMimeTypeValues",
    "OpenInferenceSpanKindValues",
    "SpanAttributes",
    "ToolCallAttributes",
]


class DbAttributes:
    """The subset of OpenTelemetry's database conventions Cortex Analyst needs."""

    DB_SYSTEM: Final = "db.system"
    DB_QUERY_TEXT: Final = "db.query.text"
    DB_RESPONSE_RETURNED_ROWS: Final = "db.response.returned_rows"


class ChipChatAttributes:
    """Chip Chat's own keys, for facts no standard covers.

    All of them are prefixed ``chip_chat.`` so a backend can tell at a glance
    which attributes are portable and which are ours.
    """

    TURN_INDEX: Final = "chip_chat.turn.index"
    """Zero-based index of this turn within its session."""

    PERSONA_ID: Final = "chip_chat.persona.id"
    """The synthetic account the visitor is browsing as."""

    DEMO_ID: Final = "chip_chat.demo.id"
    """Opaque correlation value only.

    ``demo_id`` is the row-level security key inside Snowflake and it is *not* an
    identity input anywhere else. It appears here so that a report of "it did
    something weird" can be turned into a trace, and for no other purpose. Never
    read it back off a span to make an authorisation decision.
    """

    AGENT_STEP_INDEX: Final = "chip_chat.agent.step_index"
    """Zero-based index of this model round trip within the turn."""

    PROMPT_VERSION: Final = "chip_chat.prompt.version"
    """Which system prompt this turn ran under.

    On the root span only, because it is a property of the turn rather than of
    the identity stamped on every span. An Arize experiment attributes a score
    change to a specific prompt by grouping on this, so the value has to change
    whenever the text does -- see :func:`chip_chat.agent.prompt.load`.
    """

    TOKENS_PROMPT: Final = "chip_chat.tokens.prompt"
    TOKENS_COMPLETION: Final = "chip_chat.tokens.completion"
    TOKENS_TOTAL: Final = "chip_chat.tokens.total"
    """Rolled-up token counts for a span that *contains* model calls.

    Deliberately not OpenInference's ``llm.token_count.*``, and the distinction
    is load-bearing rather than stylistic. Every actual model call carries the
    OpenInference keys, so "sum ``llm.token_count.*`` across the trace" is
    exactly the provider's reported usage for the turn -- which is the property
    #64 asks to be verifiable and :func:`chip_chat.otel.testing.assert_token_counts_sum`
    verifies. A rollup written under the same keys would double-count every
    ancestor and quietly make that sum meaningless.

    The rollups exist because Application Insights searches attributes and does
    not walk trace trees: "what did this conversation cost" and "what does the
    photo lane cost per call" are one attribute lookup with them, and a
    tree-walk without them.
    """

    RETRIEVAL_VECTOR_ARM: Final = "chip_chat.retrieval.vector_arm"
    """Which halves of a hybrid query actually *answered*, as one searchable word.

    ``contributed``, ``dropped``, ``not_sent`` or ``undetermined``. The
    vocabulary is the retriever's -- this package does not import it, because
    ``chip_chat.otel`` is a leaf and the whole reading arrives here as a string
    -- but the reason it is four values rather than a boolean belongs on the
    span, because that is where somebody reads it back.

    *Free-tier vector search returns an empty result set with HTTP 200*
    (``docs/retrieval.md`` §9). A hybrid query whose vector half was dropped
    fuses into a perfectly ordinary hybrid response that is silently the keyword
    response, so the application cannot tell it apart from a healthy one without
    the arithmetic in :attr:`RETRIEVAL_SINGLE_RANKER_FUSION`. ``dropped`` is that
    fault. ``undetermined`` is *nothing came back and there is therefore nothing
    to read*, which a filter matching no published item produces just as readily
    as a defect does; collapsing the two would put a defect report on top of a
    correct answer. ``not_sent`` is a query that asked for no vector half at all
    and so lost nothing.
    """

    RETRIEVAL_DOCUMENT_COUNT: Final = "chip_chat.retrieval.document_count"
    """How many passages the retrieval returned, including zero.

    Set on every ``retriever.search`` span, present even when it is ``0``,
    because *the retriever returned nothing* and *the retriever was never asked*
    are different facts and an absent attribute cannot tell them apart. It is
    what separates the third case from the other two: a
    :attr:`RETRIEVAL_VECTOR_ARM` of ``undetermined`` with a count of ``0`` is
    *nothing matched at all*, which is a finding about the corpus or the filter
    rather than about the service.
    """

    RETRIEVAL_SINGLE_RANKER_FUSION: Final = "chip_chat.retrieval.single_ranker_fusion"
    """The reciprocal-rank-fusion tell: no returned document was placed by both rankers.

    Reciprocal rank fusion gives a document ``1/(k + rank)`` from each ranker
    that placed it and sums the terms, so a document exactly one ranker placed
    can score at most ``1/k`` and a document both rankers placed scores strictly
    more. ``True`` here means every returned score sat at or below
    :attr:`RETRIEVAL_SINGLE_RANKER_CEILING`, which is a proof that one ranker
    contributed -- and on a query that asked for both halves, a proof that the
    vector half returned nothing while the service reported success.

    **Set only when the inequality was actually evaluated**, which means only on
    a hybrid query that returned at least one document. A lexical-only or
    vector-only query carries a BM25 score or a cosine similarity rather than a
    fusion, and the threshold means nothing against either; an empty result set
    has no score to read at all. In both cases this attribute is *absent* rather
    than ``False``, so a dashboard counting it is counting evaluated readings and
    never a query the tell does not apply to.

    A boolean rather than a derivation from :attr:`RETRIEVAL_VECTOR_ARM` for the
    reason the token rollups exist: Application Insights searches attributes and
    does not walk trace trees, and *how often is the retriever running on half of
    itself* should be one attribute lookup and an alert rule rather than a query
    somebody has to compose correctly under pressure.
    """

    RETRIEVAL_TOP_FUSED_SCORE: Final = "chip_chat.retrieval.top_fused_score"
    """The largest fused ``@search.score`` in the result set -- the tell's evidence.

    The maximum over every returned passage rather than the score of the first
    one, and the difference is not pedantry. On the reranked path the service
    reorders by ``@search.rerankerScore``, so the passage printed first is
    regularly one that only the lexical half placed even while the vector half is
    working normally. Reading the top *printed* score would report those healthy
    queries as degraded.

    Recorded beside the reading rather than instead of it so that a trace read
    months later can be re-judged against a different threshold without
    re-querying a service whose behaviour by then may have changed. Set on the
    same queries as :attr:`RETRIEVAL_SINGLE_RANKER_FUSION` and absent on the
    rest.
    """

    RETRIEVAL_SINGLE_RANKER_CEILING: Final = "chip_chat.retrieval.single_ranker_ceiling"
    """The threshold :attr:`RETRIEVAL_SINGLE_RANKER_FUSION` was decided against.

    ``1/k`` for the fusion constant the search service actually uses, computed by
    the caller and passed in -- this package is a leaf and holds no opinion about
    anybody's ranker. It is on the span because the reading is only as good as
    the constant behind it: if the service ever changes ``k``, or if this
    repository ever gets it wrong, the traces already recorded say which number
    produced their verdict instead of leaving it to be reconstructed from the
    source tree at the version somebody guesses was deployed.
    """

    GUARD_OUTCOME: Final = "chip_chat.guard.outcome"
    """``allowed`` or ``blocked``; see :class:`GuardOutcome`."""

    GUARD_REASON: Final = "chip_chat.guard.reason"
    """Why a guard blocked, in machine-comparable form, e.g. ``daily_ceiling``."""

    BUDGET_SCOPE: Final = "chip_chat.budget.scope"
    """Which ceiling was evaluated: ``global``, ``session`` or ``source_address``."""

    BUDGET_TOKENS_USED: Final = "chip_chat.budget.tokens_used"
    BUDGET_TOKENS_LIMIT: Final = "chip_chat.budget.tokens_limit"

    SAFETY_CATEGORIES: Final = "chip_chat.content_safety.categories"
    """Categories Content Safety flagged, as a list of strings."""

    SAFETY_SHIELD_DETECTIONS: Final = "chip_chat.content_safety.shield_detections"
    """What the prompt shield flagged, as a list of strings.

    Set on every screened turn including when it is empty, because an absent
    attribute and a shield that found nothing are different facts. This endpoint
    is unauthenticated and public, so the span is the only record that a
    jailbreak was attempted at all -- a detection nobody recorded is a detection
    nobody can audit after the event.

    Carries the subject as a prefix: ``user_prompt:`` for the visitor's own
    message, ``document:`` for a retrieved passage. The second is the
    cross-prompt half, and it is the half issue #81 plants its payloads for.
    """

    VISION_IMAGE_REF: Final = "chip_chat.vision.image_ref"
    """Blob reference for the uploaded image -- a storage key, not the bytes."""

    VISION_MEALS_VISIBLE: Final = "chip_chat.vision.meals_visible"

    MATCHER_SLOT_VALUES: Final = "chip_chat.matcher.slot_values"
    """Resolved slot values as ``slot=value`` strings, one per required slot."""

    MATCHER_SLOT_CONFIDENCES: Final = "chip_chat.matcher.slot_confidences"
    """Confidences aligned index-for-index with ``MATCHER_SLOT_VALUES``."""

    MATCHER_RESOLVED_SKUS: Final = "chip_chat.matcher.resolved_skus"
    MATCHER_ESCALATED: Final = "chip_chat.matcher.escalated"
    """True when a slot fell below threshold and the turn asked for clarification."""

    OPS_ACTION: Final = "chip_chat.ops.action"
    OPS_REFERENCE_ID: Final = "chip_chat.ops.reference_id"
    """The draft id, order id or reward id the write acts on."""

    OPS_CONFIRMATION_STATE: Final = "chip_chat.ops.confirmation_state"
    """``confirmed``, ``unconfirmed`` or ``rejected``; see :class:`ConfirmationState`."""


class GuardOutcome:
    """Values for :attr:`ChipChatAttributes.GUARD_OUTCOME`."""

    ALLOWED: Final = "allowed"
    BLOCKED: Final = "blocked"


class ConfirmationState:
    """Values for :attr:`ChipChatAttributes.OPS_CONFIRMATION_STATE`.

    Confirmation is enforced by the ops API, so ``REJECTED`` is what a span
    records when the agent tried to write against a draft the visitor never
    confirmed. That is an eval failure, and it needs to be visible as one.
    """

    CONFIRMED: Final = "confirmed"
    UNCONFIRMED: Final = "unconfirmed"
    REJECTED: Final = "rejected"
