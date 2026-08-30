"""The only way to emit a Chip Chat span.

Every helper below is a context manager that opens exactly one node of the tree
in :mod:`chip_chat.otel.schema`, stamps the OpenInference attributes that make
the node legible to Arize, and yields a recorder whose methods are the only
attributes a call site may set. No tracer is exported from this package, so
there is no route by which application code can invent a span name; and each
helper checks its position in the tree, so there is no route by which it can
attach a legal name in an illegal place either.

.. code-block:: python

    with chat_turn(session_id=sid, turn_index=0, message=text) as turn:
        with budget_check() as guard:
            guard.allow()
        with agent_step(index=0) as step:
            with llm_completion(model="gpt-4o") as llm:
                llm.record_usage(prompt_tokens=812, completion_tokens=64)
                llm.record_finish_reason("tool_calls")
            with tool_call(
                ToolName.SEARCH_MENU_KNOWLEDGE, arguments={"query": q}
            ) as tool:
                with retriever_search(query=q) as search:
                    search.record_documents(documents)
                tool.record_result(passages)
        with render_response() as render:
            render.record_output(reply)
        turn.record_output(reply)
"""

import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Final

from openinference.semconv.trace import (
    DocumentAttributes,
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    OpenInferenceMimeTypeValues,
    SpanAttributes,
    ToolAttributes,
)
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from chip_chat.otel.attributes import (
    ChipChatAttributes,
    ConfirmationState,
    DbAttributes,
    GuardOutcome,
)
from chip_chat.otel.schema import (
    OpsAction,
    SpanName,
    ToolName,
    allowed_parents,
    ops_span_name,
    span_kind,
    tool_span_name,
)
from chip_chat.otel.tracing import get_tracer

__all__ = [
    "AgentStepRecorder",
    "CortexAnalystRecorder",
    "Document",
    "GuardRecorder",
    "LlmRecorder",
    "MatcherRecorder",
    "Message",
    "OpsRecorder",
    "RenderRecorder",
    "RetrieverRecorder",
    "SpanSchemaError",
    "TokenUsage",
    "ToolRecorder",
    "TurnIdentity",
    "TurnRecorder",
    "VisionRecorder",
    "agent_step",
    "budget_check",
    "chat_turn",
    "content_safety",
    "cortex_analyst_query",
    "current_turn",
    "llm_completion",
    "matcher_resolve",
    "ops_write",
    "render_response",
    "resume_turn",
    "retriever_search",
    "tool_call",
    "vision_describe",
]

_JSON: Final = OpenInferenceMimeTypeValues.JSON.value


class SpanSchemaError(RuntimeError):
    """Raised when a call site would have produced a span off the schema.

    Always a programming error, never a runtime condition: it means a helper was
    invoked outside the parent RFC-001 section 09 requires it to sit under.
    """


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    """The handful of values that let a bug report become a trace.

    Stamped on *every* span in the turn rather than only on the root, because
    Application Insights searches attributes far more comfortably than it walks
    trace trees, and "it did something weird" arrives with a session id at best.
    """

    session_id: str
    turn_index: int
    persona_id: str | None = None
    demo_id: str | None = None


_turn: ContextVar[TurnIdentity | None] = ContextVar("chip_chat_otel_turn", default=None)
_node: ContextVar[SpanName | None] = ContextVar("chip_chat_otel_node", default=None)


def current_turn() -> TurnIdentity | None:
    """Return the identity of the turn in progress, if a turn is in progress."""
    return _turn.get()


@contextmanager
def resume_turn(
    identity: TurnIdentity | None,
    *,
    node: SpanName = SpanName.CHAT_TURN,
) -> Iterator[None]:
    """Adopt a turn that was opened in a *different process*.

    The nesting check and the identity stamp both hang off context variables, and
    a context variable does not cross a process boundary. So the agent container
    -- which never opens ``chat.turn``, because the app did -- starts every turn
    believing it is at the trace root, and :func:`agent_step` refuses to open
    there. Correctly: an ``agent.step`` at the root of a trace is the split trace
    that decision D8 warns about, and it should be an error rather than a shape
    a dashboard has to be taught to tolerate.

    This is how the agent side says "the turn is already open, over there".
    Nothing here opens a span; it restores the two facts a span helper needs and
    the caller supplies the parent link separately, by attaching the trace
    context that came off the wire.

    **Prefer** :func:`chip_chat.otel.propagation.continue_turn`, which does both
    from one carrier. Reach for this directly only where the trace context is
    already attached by other means.

    Args:
        identity: The turn's identity, as it was on the other side. ``None``
            leaves the agent's spans unstamped, which is a legible trace and a
            poor one -- every span is supposed to carry the session id.
        node: The schema node the *caller* has open. ``chat.turn`` in every case
            the RFC describes; the argument exists so the assumption is written
            down at the call site rather than assumed here.

    Yields:
        Nothing. The effect is on the context, for the duration of the block.
    """
    turn_token = _turn.set(identity)
    node_token = _node.set(node)
    try:
        yield
    finally:
        _node.reset(node_token)
        _turn.reset(turn_token)


def _check_parent(name: SpanName) -> None:
    parent = _node.get()
    permitted = allowed_parents(name)
    if parent in permitted:
        return
    expected = ", ".join(
        sorted("the trace root" if p is None else p.value for p in permitted)
    )
    found = "the trace root" if parent is None else parent.value
    raise SpanSchemaError(
        f"{name.value} must be a child of {expected}, but was opened under {found}"
    )


def _identity_attributes() -> dict[str, AttributeValue]:
    identity = _turn.get()
    if identity is None:
        return {}
    attributes: dict[str, AttributeValue] = {
        SpanAttributes.SESSION_ID: identity.session_id,
        ChipChatAttributes.TURN_INDEX: identity.turn_index,
    }
    if identity.persona_id is not None:
        attributes[SpanAttributes.USER_ID] = identity.persona_id
        attributes[ChipChatAttributes.PERSONA_ID] = identity.persona_id
    if identity.demo_id is not None:
        attributes[ChipChatAttributes.DEMO_ID] = identity.demo_id
    return attributes


@contextmanager
def _schema_span(
    node: SpanName,
    span_name: str,
    attributes: Mapping[str, AttributeValue],
) -> Iterator[Span]:
    """Open one schema node, or refuse to."""
    _check_parent(node)
    merged: dict[str, AttributeValue] = {
        SpanAttributes.OPENINFERENCE_SPAN_KIND: span_kind(node).value,
        **_identity_attributes(),
        **attributes,
    }
    tracer = get_tracer()
    with tracer.start_as_current_span(span_name, attributes=merged) as span:
        token = _node.set(node)
        try:
            yield span
        finally:
            _node.reset(token)


def _json(value: object) -> str:
    """Serialise for an OpenInference value attribute, never raising on odd input."""
    return json.dumps(value, default=str, sort_keys=True)


# ---------------------------------------------------------------------------
# Recorders. A call site sets attributes through these methods or not at all.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """One message in an LLM exchange."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class Document:
    """One retrieved passage, with the score that ranked it."""

    id: str
    content: str
    score: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What one model call cost, as the provider reported it.

    Carried rather than derived. A count this package estimated would still add
    up, and the sum would mean nothing -- #64 asks that the token counts on the
    spans reconcile against *the provider's* usage, which only holds if every
    number came off a response.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int | None = None
    """What the provider called the total. ``None`` means "it did not say", and
    the sum of the other two stands in -- never a silent correction of a total
    the provider did report, because a provider that counts reasoning or cached
    tokens separately is reporting a total that is deliberately not the sum."""

    @property
    def total(self) -> int:
        """The total to record: the provider's if it gave one, else the sum."""
        if self.total_tokens is not None:
            return self.total_tokens
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        """Roll two calls up into one, for a tool or a turn that made several.

        ``total_tokens`` stays ``None`` when neither side reported one, because
        the sum of two sums is the sum: materialising it would make ``x +
        TokenUsage(0, 0)`` unequal to ``x`` under dataclass equality, and every
        test comparing a measured total against a hand-written one would have
        to spell out a field it did not care about.
        """
        neither_reported = self.total_tokens is None and other.total_tokens is None
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=None if neither_reported else self.total + other.total,
        )


class _Recorder:
    """Shared behaviour: metadata, tags, and marking the span failed."""

    __slots__ = ("_span",)

    def __init__(self, span: Span) -> None:
        self._span = span

    def set_metadata(self, **values: object) -> None:
        """Attach free-form context under OpenInference's ``metadata`` key.

        This is the sanctioned escape hatch. Anything that does not have a home
        in the schema goes here, where it stays out of the attribute namespace
        the dashboards and evals are built on.
        """
        self._span.set_attribute(SpanAttributes.METADATA, _json(values))

    def add_tags(self, *tags: str) -> None:
        """Attach OpenInference tags, for filtering traces in Phoenix or Arize."""
        self._span.set_attribute(SpanAttributes.TAG_TAGS, list(tags))

    def record_token_rollup(self, usage: "TokenUsage") -> None:
        """Record what the model calls *inside* this span cost, in total.

        Under :attr:`~chip_chat.otel.attributes.ChipChatAttributes.TOKENS_TOTAL`
        and its siblings, never under OpenInference's ``llm.token_count.*``:
        those belong to the calls themselves, and a rollup sharing their keys
        would make "sum the LLM spans" count every ancestor as well. See the
        attribute's docstring for why that distinction is worth a second
        vocabulary.
        """
        self._span.set_attribute(ChipChatAttributes.TOKENS_PROMPT, usage.prompt_tokens)
        self._span.set_attribute(
            ChipChatAttributes.TOKENS_COMPLETION, usage.completion_tokens
        )
        self._span.set_attribute(ChipChatAttributes.TOKENS_TOTAL, usage.total)

    def record_failure(self, error: BaseException | str) -> None:
        """Mark this span failed.

        Lanes are allowed to fail; RFC-001 section 10 only forbids the
        conversation failing with them. Recording the failure on the span is how
        a declining lane stays visible instead of looking like a success.
        """
        if isinstance(error, BaseException):
            self._span.record_exception(error)
            self._span.set_status(Status(StatusCode.ERROR, str(error)))
        else:
            self._span.set_status(Status(StatusCode.ERROR, error))


class TurnRecorder(_Recorder):
    """``chat.turn`` -- one per visitor message."""

    __slots__ = ()

    def record_output(self, response: str) -> None:
        """Record the reply the visitor saw, at the root of the trace."""
        self._span.set_attribute(SpanAttributes.OUTPUT_VALUE, response)
        self._span.set_attribute(
            SpanAttributes.OUTPUT_MIME_TYPE, OpenInferenceMimeTypeValues.TEXT.value
        )

    def record_stopped(self, reason: str) -> None:
        """Record that the turn ended early, e.g. because a guard blocked it."""
        self._span.set_attribute(ChipChatAttributes.GUARD_REASON, reason)


class GuardRecorder(_Recorder):
    """``guard.budget_check`` and ``guard.content_safety``."""

    __slots__ = ()

    def allow(self) -> None:
        """Record that the guard let the turn through."""
        self._span.set_attribute(ChipChatAttributes.GUARD_OUTCOME, GuardOutcome.ALLOWED)

    def block(self, reason: str, *, categories: Sequence[str] = ()) -> None:
        """Record that the guard stopped the turn, and why.

        Args:
            reason: A stable machine-comparable token, e.g. ``daily_ceiling``.
                Evals group on this, so it is not a sentence.
            categories: Content Safety categories, where the guard reports them.
        """
        self._span.set_attribute(ChipChatAttributes.GUARD_OUTCOME, GuardOutcome.BLOCKED)
        self._span.set_attribute(ChipChatAttributes.GUARD_REASON, reason)
        if categories:
            self._span.set_attribute(
                ChipChatAttributes.SAFETY_CATEGORIES, list(categories)
            )

    def record_shield(self, detections: Sequence[str]) -> None:
        """Record what the prompt shield found, including nothing.

        Set unconditionally rather than only when something fired, because an
        absent attribute and a shield that ran and found nothing are different
        facts. ``/api/chat`` is unauthenticated and public, so this span is the
        only record that a jailbreak was attempted -- and "no detections" is
        evidence the shield ran, which "no attribute" is not.

        Args:
            detections: Subject-prefixed labels, e.g. ``user_prompt:persona_override``
                or ``document:0:attack_detected``. See
                :data:`~chip_chat.otel.attributes.ChipChatAttributes.SAFETY_SHIELD_DETECTIONS`.
        """
        self._span.set_attribute(
            ChipChatAttributes.SAFETY_SHIELD_DETECTIONS, list(detections)
        )

    def record_budget(self, *, scope: str, tokens_used: int, tokens_limit: int) -> None:
        """Record which ceiling was evaluated and how close it is.

        Args:
            scope: ``global``, ``session`` or ``source_address``.
            tokens_used: Tokens consumed against that ceiling.
            tokens_limit: The ceiling itself.
        """
        self._span.set_attribute(ChipChatAttributes.BUDGET_SCOPE, scope)
        self._span.set_attribute(ChipChatAttributes.BUDGET_TOKENS_USED, tokens_used)
        self._span.set_attribute(ChipChatAttributes.BUDGET_TOKENS_LIMIT, tokens_limit)


class AgentStepRecorder(_Recorder):
    """``agent.step`` -- one per model round trip."""

    __slots__ = ()

    def record_output(self, summary: str) -> None:
        """Record what the step concluded, in whatever form the agent has it."""
        self._span.set_attribute(SpanAttributes.OUTPUT_VALUE, summary)


class LlmRecorder(_Recorder):
    """``llm.completion`` -- tokens, model and finish reason."""

    __slots__ = ()

    def record_input_messages(self, messages: Sequence[Message]) -> None:
        """Record the prompt, flattened the way OpenInference expects."""
        self._record_messages(SpanAttributes.LLM_INPUT_MESSAGES, messages)

    def record_output_messages(self, messages: Sequence[Message]) -> None:
        """Record what came back, flattened the way OpenInference expects."""
        self._record_messages(SpanAttributes.LLM_OUTPUT_MESSAGES, messages)

    def _record_messages(self, prefix: str, messages: Sequence[Message]) -> None:
        for index, message in enumerate(messages):
            base = f"{prefix}.{index}"
            role_key = f"{base}.{MessageAttributes.MESSAGE_ROLE}"
            content_key = f"{base}.{MessageAttributes.MESSAGE_CONTENT}"
            self._span.set_attribute(role_key, message.role)
            self._span.set_attribute(content_key, message.content)

    def record_tools(self, tool_schemas: Sequence[Mapping[str, Any]]) -> None:
        """Record the tool definitions the model was offered.

        Arize's tool-selection evals compare the tool chosen against the tools
        available, so the definitions have to be on the span, not merely in the
        prompt template.
        """
        for index, schema in enumerate(tool_schemas):
            self._span.set_attribute(
                f"{SpanAttributes.LLM_TOOLS}.{index}.{ToolAttributes.TOOL_JSON_SCHEMA}",
                _json(schema),
            )

    def record_invocation_parameters(self, **parameters: object) -> None:
        """Record temperature, max tokens and anything else that shaped the call."""
        self._span.set_attribute(
            SpanAttributes.LLM_INVOCATION_PARAMETERS, _json(parameters)
        )

    def record_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int | None = None,
    ) -> None:
        """Record token counts.

        The cost dashboard is a sum over these, so they are not optional on a
        real completion.
        """
        self._span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, prompt_tokens)
        self._span.set_attribute(
            SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, completion_tokens
        )
        self._span.set_attribute(
            SpanAttributes.LLM_TOKEN_COUNT_TOTAL,
            prompt_tokens + completion_tokens if total_tokens is None else total_tokens,
        )

    def record_finish_reason(self, reason: str) -> None:
        """Record why generation stopped, e.g. ``stop``, ``tool_calls``, ``length``."""
        self._span.set_attribute(SpanAttributes.LLM_FINISH_REASON, reason)


class ToolRecorder(_Recorder):
    """``tool.<tool_name>`` -- one per call, arguments recorded."""

    __slots__ = ()

    def record_result(self, result: object) -> None:
        """Record what the tool returned."""
        self._span.set_attribute(SpanAttributes.OUTPUT_VALUE, _json(result))
        self._span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, _JSON)


class RetrieverRecorder(_Recorder):
    """``retriever.search`` -- documents and the scores that ranked them."""

    __slots__ = ()

    def record_documents(self, documents: Sequence[Document]) -> None:
        """Record retrieved passages, flattened per OpenInference.

        Citations are part of the payload rather than reconstructed afterwards
        (RFC-001 section 08), so ``source_url`` and ``harvested_at`` belong in
        each document's metadata and travel onto the span with it.
        """
        for index, document in enumerate(documents):
            base = f"{SpanAttributes.RETRIEVAL_DOCUMENTS}.{index}"
            self._span.set_attribute(
                f"{base}.{DocumentAttributes.DOCUMENT_ID}", document.id
            )
            self._span.set_attribute(
                f"{base}.{DocumentAttributes.DOCUMENT_CONTENT}", document.content
            )
            if document.score is not None:
                self._span.set_attribute(
                    f"{base}.{DocumentAttributes.DOCUMENT_SCORE}", document.score
                )
            if document.metadata:
                self._span.set_attribute(
                    f"{base}.{DocumentAttributes.DOCUMENT_METADATA}",
                    _json(document.metadata),
                )

    def record_fusion(
        self,
        *,
        vector_arm: str,
        documents: int,
        top_fused_score: float | None = None,
        single_ranker_ceiling: float | None = None,
        single_ranker_fusion: bool | None = None,
    ) -> None:
        """Record whether this retrieval had both of its rankers.

        A hybrid search can lose half of itself and still answer 200 with a
        well-formed result set -- ``docs/retrieval.md`` section 9 measures that
        happening to between a quarter and nine in ten queries against the Free
        tier. The retriever detects it from the fused scores; this is where the
        detection becomes something an operator can count. It goes in the
        ``chip_chat.*`` namespace because neither OpenInference nor the OTel
        database conventions has anything to say about rank fusion, and it goes
        in flat attributes rather than into ``metadata`` for the reason the token
        rollups do: Application Insights searches attributes and does not walk
        trace trees, and a JSON blob is not a filter.

        The three optional arguments are the arithmetic and they travel
        together. Pass all three on a query where the inequality was evaluated --
        a hybrid query that returned at least one document -- and none of them
        otherwise. **Absent is not false here.** A lexical-only or vector-only
        query has no fused score to threshold, and an empty result set has no
        score at all; recording ``False`` for either would put a healthy-looking
        reading on a query the tell cannot speak about, which is how a detector
        stops being believed.

        Args:
            vector_arm: The retriever's reading, as one lowercase word.
            documents: How many passages came back, ``0`` included. Recorded
                unconditionally, because *nothing matched* and *this was never
                asked* have to be distinguishable.
            top_fused_score: The largest fused score in the result set. The
                maximum, never the first: the reranked path reorders by
                relevance and its top hit is regularly one only the lexical half
                placed.
            single_ranker_ceiling: ``1/k`` for the fusion constant the service
                uses, computed by the caller.
            single_ranker_fusion: Whether no returned document cleared that
                ceiling, which proves exactly one ranker contributed.

        Raises:
            ValueError: If the arithmetic arrives incomplete. A fired tell with
                no threshold beside it is evidence nobody can check, and the
                cheapest moment to refuse it is here.
        """
        self._span.set_attribute(ChipChatAttributes.RETRIEVAL_VECTOR_ARM, vector_arm)
        self._span.set_attribute(ChipChatAttributes.RETRIEVAL_DOCUMENT_COUNT, documents)
        if (
            top_fused_score is None
            and single_ranker_ceiling is None
            and single_ranker_fusion is None
        ):
            return
        if (
            top_fused_score is None
            or single_ranker_ceiling is None
            or single_ranker_fusion is None
        ):
            raise ValueError(
                "the fusion tell travels whole: top_fused_score, "
                "single_ranker_ceiling and single_ranker_fusion are recorded "
                "together or not at all"
            )
        self._span.set_attribute(
            ChipChatAttributes.RETRIEVAL_TOP_FUSED_SCORE, top_fused_score
        )
        self._span.set_attribute(
            ChipChatAttributes.RETRIEVAL_SINGLE_RANKER_CEILING, single_ranker_ceiling
        )
        self._span.set_attribute(
            ChipChatAttributes.RETRIEVAL_SINGLE_RANKER_FUSION, single_ranker_fusion
        )


class CortexAnalystRecorder(_Recorder):
    """``db.cortex_analyst`` -- the generated SQL and how many rows it returned."""

    __slots__ = ()

    def record_query(self, *, sql: str, row_count: int) -> None:
        """Record the SQL Cortex Analyst generated and the rows it produced."""
        self._span.set_attribute(DbAttributes.DB_QUERY_TEXT, sql)
        self._span.set_attribute(DbAttributes.DB_RESPONSE_RETURNED_ROWS, row_count)

    def record_declined(self, reason: str) -> None:
        """Record a timeout or low-confidence refusal.

        RFC-001 section 10 forbids falling back to a hand-written query, so this
        is the whole of the failure path and it needs to be visible.
        """
        self._span.set_attribute(ChipChatAttributes.GUARD_REASON, reason)
        self.record_failure(reason)


class VisionRecorder(_Recorder):
    """``vision.describe`` -- image reference, structured output and tokens.

    An LLM-kind span (see :func:`~chip_chat.otel.schema.span_kind`), so it owes
    the same two things every other model call owes: what went in, in the form
    OpenInference reads, and what it cost.
    """

    __slots__ = ()

    def record_image(self, image_ref: str, *, prompt: str | None = None) -> None:
        """Record the photograph as a multimodal LLM input.

        The image already rides on the span as
        :attr:`~chip_chat.otel.attributes.ChipChatAttributes.VISION_IMAGE_REF`,
        which is what an operator greps. This records it *again* under
        OpenInference's message-contents layout, which is what makes Phoenix and
        Arize render the span as a vision call with an image attached rather
        than as an LLM span that happens to carry an opaque string. Both, not
        one: the first is searchable, the second is legible, and #64 asks for a
        photo turn a person can read.

        A reference and never the bytes, here as everywhere -- RFC-001 section
        07, and traces are not an image store.

        Args:
            image_ref: The blob reference for the normalised image.
            prompt: The text part of the same message, if the call had one.
        """
        base = f"{SpanAttributes.LLM_INPUT_MESSAGES}.0"
        self._span.set_attribute(f"{base}.{MessageAttributes.MESSAGE_ROLE}", "user")
        contents = f"{base}.{MessageAttributes.MESSAGE_CONTENTS}"
        self._span.set_attribute(
            f"{contents}.0.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "image"
        )
        self._span.set_attribute(
            f"{contents}.0.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}"
            f".{ImageAttributes.IMAGE_URL}",
            image_ref,
        )
        if prompt is not None:
            self._span.set_attribute(
                f"{contents}.1.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}", "text"
            )
            self._span.set_attribute(
                f"{contents}.1.{MessageContentAttributes.MESSAGE_CONTENT_TEXT}", prompt
            )

    def record_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int | None = None,
    ) -> None:
        """Record token counts, exactly as :meth:`LlmRecorder.record_usage` does.

        The photo lane is the expensive one -- an image is worth a few hundred
        prompt tokens -- so a cost dashboard that omitted it would be wrong in
        the direction that matters most.
        """
        self._span.set_attribute(SpanAttributes.LLM_TOKEN_COUNT_PROMPT, prompt_tokens)
        self._span.set_attribute(
            SpanAttributes.LLM_TOKEN_COUNT_COMPLETION, completion_tokens
        )
        self._span.set_attribute(
            SpanAttributes.LLM_TOKEN_COUNT_TOTAL,
            prompt_tokens + completion_tokens if total_tokens is None else total_tokens,
        )

    def record_description(self, description: Mapping[str, Any]) -> None:
        """Record the stage-4 structured output verbatim.

        The model returns slots and confidences only. ``notes`` is display-only
        and nothing downstream parses it, which stays true here too.
        """
        self._span.set_attribute(SpanAttributes.OUTPUT_VALUE, _json(description))
        self._span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, _JSON)
        meals_visible = description.get("meals_visible")
        if isinstance(meals_visible, int):
            self._span.set_attribute(
                ChipChatAttributes.VISION_MEALS_VISIBLE, meals_visible
            )


class MatcherRecorder(_Recorder):
    """``matcher.resolve`` -- slot confidences and the SKUs they resolved to."""

    __slots__ = ()

    def record_slots(self, slots: Mapping[str, tuple[str, float]]) -> None:
        """Record each slot's resolved value and confidence.

        Args:
            slots: Slot name to ``(value, confidence)``. Recorded as two aligned
                lists so a dashboard can chart confidence per slot without
                parsing JSON.
        """
        names = sorted(slots)
        self._span.set_attribute(
            ChipChatAttributes.MATCHER_SLOT_VALUES,
            [f"{name}={slots[name][0]}" for name in names],
        )
        self._span.set_attribute(
            ChipChatAttributes.MATCHER_SLOT_CONFIDENCES,
            [slots[name][1] for name in names],
        )

    def record_resolved_skus(self, skus: Sequence[str]) -> None:
        """Record the catalogue rows the slots landed on."""
        self._span.set_attribute(ChipChatAttributes.MATCHER_RESOLVED_SKUS, list(skus))

    def record_escalation(self, reason: str) -> None:
        """Record that a slot fell below threshold, so the turn asked instead."""
        self._span.set_attribute(ChipChatAttributes.MATCHER_ESCALATED, True)
        self._span.set_attribute(ChipChatAttributes.GUARD_REASON, reason)


class OpsRecorder(_Recorder):
    """``ops.<action>`` -- draft id and confirmation state."""

    __slots__ = ()

    def record_confirmation(self, state: str) -> None:
        """Record whether the ops API accepted the write as confirmed.

        ``rejected`` means the agent tried to write against something the
        visitor never confirmed. That is a launch-gate violation and the span is
        where an eval finds it.
        """
        self._span.set_attribute(ChipChatAttributes.OPS_CONFIRMATION_STATE, state)
        if state == ConfirmationState.REJECTED:
            self.record_failure("write attempted without visitor confirmation")

    def record_receipt(self, receipt: object) -> None:
        """Record what the ops API returned."""
        self._span.set_attribute(SpanAttributes.OUTPUT_VALUE, _json(receipt))
        self._span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, _JSON)


class RenderRecorder(_Recorder):
    """``render.response`` -- what the visitor actually saw."""

    __slots__ = ()

    def record_output(self, response: str) -> None:
        """Record the rendered reply."""
        self._span.set_attribute(SpanAttributes.OUTPUT_VALUE, response)
        self._span.set_attribute(
            SpanAttributes.OUTPUT_MIME_TYPE, OpenInferenceMimeTypeValues.TEXT.value
        )


# ---------------------------------------------------------------------------
# The context managers. One per node of the tree, and no others exist.
# ---------------------------------------------------------------------------


@contextmanager
def chat_turn(
    *,
    session_id: str,
    turn_index: int,
    message: str | None = None,
    persona_id: str | None = None,
    demo_id: str | None = None,
    prompt_version: str | None = None,
) -> Iterator[TurnRecorder]:
    """Open ``chat.turn``, the root of one visitor message.

    Args:
        session_id: The conversation this turn belongs to.
        turn_index: Zero-based index of the turn within the session.
        message: What the visitor said, recorded as the trace's input.
        persona_id: The synthetic account being browsed as, if one is bound.
        demo_id: Opaque correlation value only -- never an identity input. See
            :attr:`~chip_chat.otel.attributes.ChipChatAttributes.DEMO_ID`.
        prompt_version: Which system prompt the turn ran under, from
            :attr:`chip_chat.agent.prompt.SystemPrompt.version`. Root span only:
            an experiment groups on it to attribute a score change to a prompt.

    Yields:
        A :class:`TurnRecorder` for the root span.
    """
    identity = TurnIdentity(
        session_id=session_id,
        turn_index=turn_index,
        persona_id=persona_id,
        demo_id=demo_id,
    )
    turn_token = _turn.set(identity)
    try:
        attributes: dict[str, AttributeValue] = {}
        if prompt_version is not None:
            attributes[ChipChatAttributes.PROMPT_VERSION] = prompt_version
        if message is not None:
            attributes[SpanAttributes.INPUT_VALUE] = message
            attributes[SpanAttributes.INPUT_MIME_TYPE] = (
                OpenInferenceMimeTypeValues.TEXT.value
            )
        with _schema_span(
            SpanName.CHAT_TURN, SpanName.CHAT_TURN.value, attributes
        ) as span:
            yield TurnRecorder(span)
    finally:
        _turn.reset(turn_token)


@contextmanager
def budget_check() -> Iterator[GuardRecorder]:
    """Open ``guard.budget_check``. Synchronous, and may terminate the turn."""
    with _schema_span(
        SpanName.GUARD_BUDGET_CHECK, SpanName.GUARD_BUDGET_CHECK.value, {}
    ) as span:
        yield GuardRecorder(span)


@contextmanager
def content_safety(*, subject: str = "text") -> Iterator[GuardRecorder]:
    """Open ``guard.content_safety``.

    Args:
        subject: What was screened -- ``text`` or ``image``. Image moderation
            runs before inference, so this distinguishes the two call sites.
    """
    with _schema_span(
        SpanName.GUARD_CONTENT_SAFETY,
        SpanName.GUARD_CONTENT_SAFETY.value,
        {SpanAttributes.INPUT_VALUE: subject},
    ) as span:
        yield GuardRecorder(span)


@contextmanager
def agent_step(*, index: int) -> Iterator[AgentStepRecorder]:
    """Open ``agent.step``, one model round trip.

    Args:
        index: Zero-based index of the round trip within the turn.
    """
    with _schema_span(
        SpanName.AGENT_STEP,
        SpanName.AGENT_STEP.value,
        {ChipChatAttributes.AGENT_STEP_INDEX: index},
    ) as span:
        yield AgentStepRecorder(span)


@contextmanager
def llm_completion(
    *,
    model: str,
    provider: str | None = None,
    system: str | None = None,
) -> Iterator[LlmRecorder]:
    """Open ``llm.completion``.

    Args:
        model: The deployment or model name the call was made against.
        provider: OpenInference ``llm.provider``, e.g. ``azure``.
        system: OpenInference ``llm.system``, e.g. ``openai``.
    """
    attributes: dict[str, AttributeValue] = {SpanAttributes.LLM_MODEL_NAME: model}
    if provider is not None:
        attributes[SpanAttributes.LLM_PROVIDER] = provider
    if system is not None:
        attributes[SpanAttributes.LLM_SYSTEM] = system
    with _schema_span(
        SpanName.LLM_COMPLETION, SpanName.LLM_COMPLETION.value, attributes
    ) as span:
        yield LlmRecorder(span)


@contextmanager
def tool_call(
    tool: ToolName,
    *,
    arguments: Mapping[str, Any] | None = None,
    description: str | None = None,
) -> Iterator[ToolRecorder]:
    """Open ``tool.<tool_name>``.

    Args:
        tool: One of the eleven tools. A :class:`~chip_chat.otel.schema.ToolName`
            and not a string, so a typo is a failed import rather than a span
            nobody's dashboard is watching.
        arguments: The arguments as called. Recorded verbatim -- none of the
            eleven tools takes a visitor identifier, so there is nothing here
            that should not be in a trace.
        description: The tool description the model was shown, if to hand.

    Yields:
        A :class:`ToolRecorder`.
    """
    attributes: dict[str, AttributeValue] = {SpanAttributes.TOOL_NAME: tool.value}
    if description is not None:
        attributes[SpanAttributes.TOOL_DESCRIPTION] = description
    if arguments is not None:
        serialised = _json(arguments)
        attributes[SpanAttributes.TOOL_PARAMETERS] = serialised
        attributes[SpanAttributes.INPUT_VALUE] = serialised
        attributes[SpanAttributes.INPUT_MIME_TYPE] = _JSON
    with _schema_span(SpanName.TOOL, tool_span_name(tool), attributes) as span:
        yield ToolRecorder(span)


@contextmanager
def retriever_search(
    *, query: str, index: str | None = None
) -> Iterator[RetrieverRecorder]:
    """Open ``retriever.search``.

    Args:
        query: The search text sent to AI Search.
        index: The index alias searched, so a stale alias swap is visible.
    """
    attributes: dict[str, AttributeValue] = {
        SpanAttributes.INPUT_VALUE: query,
        SpanAttributes.INPUT_MIME_TYPE: OpenInferenceMimeTypeValues.TEXT.value,
    }
    if index is not None:
        attributes[SpanAttributes.METADATA] = _json({"index": index})
    with _schema_span(
        SpanName.RETRIEVER_SEARCH, SpanName.RETRIEVER_SEARCH.value, attributes
    ) as span:
        yield RetrieverRecorder(span)


@contextmanager
def cortex_analyst_query(*, question: str) -> Iterator[CortexAnalystRecorder]:
    """Open ``db.cortex_analyst``.

    Args:
        question: The natural-language question handed to Cortex Analyst.
    """
    with _schema_span(
        SpanName.DB_CORTEX_ANALYST,
        SpanName.DB_CORTEX_ANALYST.value,
        {
            DbAttributes.DB_SYSTEM: "snowflake",
            SpanAttributes.INPUT_VALUE: question,
            SpanAttributes.INPUT_MIME_TYPE: OpenInferenceMimeTypeValues.TEXT.value,
        },
    ) as span:
        yield CortexAnalystRecorder(span)


@contextmanager
def vision_describe(*, image_ref: str, model: str) -> Iterator[VisionRecorder]:
    """Open ``vision.describe``.

    Args:
        image_ref: The blob reference for the normalised image. A storage key,
            never the bytes and never a data URI -- traces are not an image store.
        model: The vision deployment called.
    """
    with _schema_span(
        SpanName.VISION_DESCRIBE,
        SpanName.VISION_DESCRIBE.value,
        {
            ChipChatAttributes.VISION_IMAGE_REF: image_ref,
            SpanAttributes.LLM_MODEL_NAME: model,
        },
    ) as span:
        yield VisionRecorder(span)


@contextmanager
def matcher_resolve() -> Iterator[MatcherRecorder]:
    """Open ``matcher.resolve``, the deterministic slot-to-SKU step."""
    with _schema_span(
        SpanName.MATCHER_RESOLVE, SpanName.MATCHER_RESOLVE.value, {}
    ) as span:
        yield MatcherRecorder(span)


@contextmanager
def ops_write(action: OpsAction, *, reference_id: str) -> Iterator[OpsRecorder]:
    """Open ``ops.<action>``.

    Args:
        action: One of the four write actions.
        reference_id: The draft id, order id or reward id the write acts on --
            always an identifier for something the visitor has already been shown.
    """
    with _schema_span(
        SpanName.OPS,
        ops_span_name(action),
        {
            ChipChatAttributes.OPS_ACTION: action.value,
            ChipChatAttributes.OPS_REFERENCE_ID: reference_id,
        },
    ) as span:
        yield OpsRecorder(span)


@contextmanager
def render_response() -> Iterator[RenderRecorder]:
    """Open ``render.response``, the last child of the turn."""
    with _schema_span(
        SpanName.RENDER_RESPONSE, SpanName.RENDER_RESPONSE.value, {}
    ) as span:
        yield RenderRecorder(span)
