"""The span schema. Every name Chip Chat may emit is enumerated here.

RFC-001 section 09 fixes the tree below, and this module is its executable form.
Span names are a schema rather than a debugging convenience: Phase 9 evaluations
and every dashboard axis attach to them, so a rename is a breaking change to
consumers that live outside this repository.

.. code-block:: text

    chat.turn                    root span, one per visitor message
    |- guard.budget_check        synchronous; may terminate the turn
    |- guard.content_safety
    |- agent.step                one per model round trip
    |  |- llm.completion         tokens, model, finish reason
    |  `- tool.<tool_name>       one per call, arguments recorded
    |     |- retriever.search    documents + scores
    |     |- db.cortex_analyst   generated SQL + row count
    |     |- vision.describe     image ref + structured output
    |     |- matcher.resolve     slot confidences + resolved SKUs
    |     `- ops.<action>        draft id + confirmation state
    `- render.response

Two names are parameterised. ``tool.<tool_name>`` takes one of the eleven tools
in RFC-001 section 06 and ``ops.<action>`` one of the four write actions the ops
API exposes; both draw from closed enumerations, so the parameter cannot smuggle
a free-form name into the vocabulary either.
"""

from collections.abc import Mapping
from enum import StrEnum

from openinference.semconv.trace import OpenInferenceSpanKindValues

__all__ = [
    "OPS_SPAN_PREFIX",
    "SPAN_NAMES",
    "TOOL_SPAN_PREFIX",
    "WRITE_TOOLS",
    "OpsAction",
    "SpanName",
    "ToolName",
    "allowed_parents",
    "ops_span_name",
    "span_kind",
    "tool_span_name",
]


class ToolName(StrEnum):
    """The eleven tools of RFC-001 section 06, and nothing else.

    The value is the tool name as the agent sees it, so it is also the suffix of
    the tool span: ``ToolName.PLACE_ORDER`` becomes ``tool.place_order``.
    """

    SEARCH_MENU_KNOWLEDGE = "search_menu_knowledge"
    ASK_ACCOUNT_QUESTION = "ask_account_question"
    GET_POINTS_BALANCE = "get_points_balance"
    GET_USUAL_ORDER = "get_usual_order"
    GET_RECOMMENDATIONS = "get_recommendations"
    MATCH_MEAL_FROM_PHOTO = "match_meal_from_photo"
    PROPOSE_ORDER = "propose_order"
    PLACE_ORDER = "place_order"
    CANCEL_ORDER = "cancel_order"
    REDEEM_POINTS = "redeem_points"
    UPDATE_PREFERENCES = "update_preferences"


WRITE_TOOLS = frozenset(
    {
        ToolName.PLACE_ORDER,
        ToolName.CANCEL_ORDER,
        ToolName.REDEEM_POINTS,
        ToolName.UPDATE_PREFERENCES,
    }
)
"""The tools that write and therefore require visitor confirmation."""


class OpsAction(StrEnum):
    """The write actions the ops API exposes, suffixing ``ops.<action>``."""

    PLACE_ORDER = "place_order"
    CANCEL_ORDER = "cancel_order"
    REDEEM_POINTS = "redeem_points"
    UPDATE_PREFERENCES = "update_preferences"


TOOL_SPAN_PREFIX = "tool."
OPS_SPAN_PREFIX = "ops."


class SpanName(StrEnum):
    """Every node of the span tree.

    ``TOOL`` and ``OPS`` hold templates rather than emittable names -- the ``*``
    is deliberate, so a value that reached a span name by accident would be
    obviously wrong in a trace. Resolve them with :func:`tool_span_name` and
    :func:`ops_span_name`.
    """

    CHAT_TURN = "chat.turn"
    GUARD_BUDGET_CHECK = "guard.budget_check"
    GUARD_CONTENT_SAFETY = "guard.content_safety"
    AGENT_STEP = "agent.step"
    LLM_COMPLETION = "llm.completion"
    TOOL = "tool.*"
    RETRIEVER_SEARCH = "retriever.search"
    DB_CORTEX_ANALYST = "db.cortex_analyst"
    VISION_DESCRIBE = "vision.describe"
    MATCHER_RESOLVE = "matcher.resolve"
    OPS = "ops.*"
    RENDER_RESPONSE = "render.response"


def tool_span_name(tool: ToolName) -> str:
    """Return the span name for a call to ``tool``, e.g. ``tool.place_order``."""
    return f"{TOOL_SPAN_PREFIX}{tool.value}"


def ops_span_name(action: OpsAction) -> str:
    """Return the span name for an ops write, e.g. ``ops.place_order``."""
    return f"{OPS_SPAN_PREFIX}{action.value}"


SPAN_NAMES: frozenset[str] = frozenset(
    [name.value for name in SpanName if name not in (SpanName.TOOL, SpanName.OPS)]
    + [tool_span_name(tool) for tool in ToolName]
    + [ops_span_name(action) for action in OpsAction]
)
"""Every span name a conforming Chip Chat turn can emit, templates expanded."""


_ALLOWED_PARENTS: Mapping[SpanName, frozenset[SpanName | None]] = {
    SpanName.CHAT_TURN: frozenset({None}),
    SpanName.GUARD_BUDGET_CHECK: frozenset({SpanName.CHAT_TURN}),
    SpanName.GUARD_CONTENT_SAFETY: frozenset({SpanName.CHAT_TURN}),
    SpanName.AGENT_STEP: frozenset({SpanName.CHAT_TURN}),
    SpanName.LLM_COMPLETION: frozenset({SpanName.AGENT_STEP}),
    SpanName.TOOL: frozenset({SpanName.AGENT_STEP}),
    SpanName.RETRIEVER_SEARCH: frozenset({SpanName.TOOL}),
    SpanName.DB_CORTEX_ANALYST: frozenset({SpanName.TOOL}),
    SpanName.VISION_DESCRIBE: frozenset({SpanName.TOOL}),
    SpanName.MATCHER_RESOLVE: frozenset({SpanName.TOOL}),
    SpanName.OPS: frozenset({SpanName.TOOL}),
    SpanName.RENDER_RESPONSE: frozenset({SpanName.CHAT_TURN}),
}


def allowed_parents(name: SpanName) -> frozenset[SpanName | None]:
    """Return the nodes ``name`` may nest inside; ``None`` means "trace root"."""
    return _ALLOWED_PARENTS[name]


_SPAN_KINDS: Mapping[SpanName, OpenInferenceSpanKindValues] = {
    # A turn is a chain: it orchestrates, it does not itself call a model.
    SpanName.CHAT_TURN: OpenInferenceSpanKindValues.CHAIN,
    SpanName.GUARD_BUDGET_CHECK: OpenInferenceSpanKindValues.GUARDRAIL,
    SpanName.GUARD_CONTENT_SAFETY: OpenInferenceSpanKindValues.GUARDRAIL,
    SpanName.AGENT_STEP: OpenInferenceSpanKindValues.AGENT,
    SpanName.LLM_COMPLETION: OpenInferenceSpanKindValues.LLM,
    SpanName.TOOL: OpenInferenceSpanKindValues.TOOL,
    SpanName.RETRIEVER_SEARCH: OpenInferenceSpanKindValues.RETRIEVER,
    SpanName.DB_CORTEX_ANALYST: OpenInferenceSpanKindValues.TOOL,
    # The vision model is a model call, so Arize should score it as one.
    SpanName.VISION_DESCRIBE: OpenInferenceSpanKindValues.LLM,
    # The matcher is deterministic, so it is a chain rather than a model call.
    SpanName.MATCHER_RESOLVE: OpenInferenceSpanKindValues.CHAIN,
    SpanName.OPS: OpenInferenceSpanKindValues.TOOL,
    SpanName.RENDER_RESPONSE: OpenInferenceSpanKindValues.CHAIN,
}


def span_kind(name: SpanName) -> OpenInferenceSpanKindValues:
    """Return the OpenInference span kind for ``name``.

    The kind is what makes Arize read a span as an LLM call, a retrieval or a
    guardrail rather than as an anonymous unit of work, so it belongs to the
    schema and not to the call site.
    """
    return _SPAN_KINDS[name]
