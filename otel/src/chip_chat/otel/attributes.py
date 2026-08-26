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
   confidences, ops confirmation state.

Nothing here is a free-form key. Call sites reach these through the recorders in
:mod:`chip_chat.otel.spans` and generally never name an attribute at all.
"""

from typing import Final

from openinference.semconv.trace import (
    DocumentAttributes,
    ImageAttributes,
    MessageAttributes,
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
