"""One turn's span tree, read back as the calls the model actually made.

This is where #74 attaches to the schema, and the attachment is the reason
RFC-001 section 09 was frozen in #14 before any agent existed: a trajectory is
read off the ``tool.<tool_name>`` spans by name. Rename one and this eval stops
seeing it -- which is exactly the breakage the fixed vocabulary exists to make
loud rather than silent.

**The trace is a shape this module refuses to assume.** The bead behind #74 is
explicit that #74 depends on #103 being *correct*: the app and the hosted agent
emit under two ``service.name`` values, and if W3C trace context does not
propagate, the turn arrives as two unrelated traces -- a ``chat.turn`` with the
guards under it, and an orphan ``agent.step`` carrying every tool call. The tool
spans are all still there. A reader that simply collected them would score that
turn happily and report a number computed over half a tree.

So :class:`Trajectory` carries :attr:`~Trajectory.trace_ids` and the count of
roots, :attr:`~Trajectory.readable` is false when the turn is split, and a split
turn is **unscored** in every rate downstream. :mod:`chip_chat.eval.trajectory.
report` prints the count with #103 named beside it. ``make trace-boundary`` is
how you check the propagation itself.

**The span type here is ours, not the SDK's.** :class:`TraceSpan` is six
fields, and :func:`from_readable_spans` is the adapter for an in-process
recording. That indirection buys the fourth acceptance criterion: online evals
(#76) read spans back out of a backend as rows, and a reader written against
``opentelemetry.sdk.trace.ReadableSpan`` would have to be rewritten to score
live traffic. A second adapter is a function; a second reader is a second
implementation of the metric.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from openinference.semconv.trace import SpanAttributes
from opentelemetry.sdk.trace import ReadableSpan

from chip_chat.eval.golden.lanes import Lane, lane_of
from chip_chat.otel.schema import TOOL_SPAN_PREFIX, SpanName, ToolName

__all__ = [
    "ToolCall",
    "TraceSpan",
    "Trajectory",
    "from_readable_spans",
    "read_trajectory",
]


@dataclass(frozen=True, slots=True)
class TraceSpan:
    """One span, in the six fields a trajectory is read from.

    Attributes:
        name: The span name, which for a tool call is the whole schema
            attachment. See the module docstring.
        span_id: Unique within the trace.
        parent_id: The span this nested inside, or ``None`` at a root.
        trace_id: Which trace it belongs to. More than one across a turn is the
            failure #103 is responsible for not producing.
        attributes: Everything the span recorded, as it recorded it.
        service: The ``service.name`` it was emitted under, where the source
            knows it. Two per turn is correct and expected -- the app and the
            agent are two processes -- so this is reported rather than checked.
        started: Start time, for putting calls in call order. Any monotonic
            integer will do; a source that has none may leave it at zero and
            supply the spans in order instead.
    """

    name: str
    span_id: str
    parent_id: str | None
    trace_id: str
    attributes: Mapping[str, Any] = field(default_factory=dict)
    service: str | None = None
    started: int = 0


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One ``tool.<tool_name>`` span, as the call it records.

    Attributes:
        tool: Which of the eleven. Read from the span name, because the name is
            the fixed vocabulary and the attribute is a convenience beside it.
        arguments: What the model passed, as
            :func:`~chip_chat.otel.spans.tool_call` recorded them. Empty where
            the span carried none -- which is itself an observation, and the
            query check reads it as one.
        step: Which ``agent.step`` this sat under, zero-based in start order,
            or ``-1`` where the step was not in the recording. Carried so that
            *three calls in one step* and *three steps of one call each* can be
            told apart, which is the difference between a model that fanned out
            and a model that kept going back for more.
        order: Position in the turn's call order, zero-based.
    """

    tool: ToolName
    arguments: Mapping[str, Any] = field(default_factory=dict)
    step: int = -1
    order: int = 0

    @property
    def lane(self) -> Lane:
        """The lane this call entered."""
        return lane_of(self.tool)


@dataclass(frozen=True, slots=True)
class Trajectory:
    """What one turn's spans say happened, and whether they can be believed.

    Attributes:
        entry_id: The dataset row this answers.
        calls: Every tool call on the turn, in call order.
        trace_ids: Every trace id in the recording. One is correct.
        services: Every ``service.name`` in it. One or two are both correct.
        steps: How many ``agent.step`` spans there were -- model round trips,
            which is what a trajectory costs.
        roots: How many ``chat.turn`` spans there were. One is correct.
        error: Why there is nothing here, in one line. ``None`` on success.
    """

    entry_id: str
    calls: tuple[ToolCall, ...] = ()
    trace_ids: frozenset[str] = frozenset()
    services: frozenset[str] = frozenset()
    steps: int = 0
    roots: int = 0
    error: str | None = None

    @property
    def split(self) -> bool:
        """Whether the turn arrived as more than one trace. See the module docstring."""
        return len(self.trace_ids) > 1

    @property
    def readable(self) -> bool:
        """Whether this tree can be scored at all.

        One trace, one turn, and nothing went wrong fetching it. Anything else
        is unscored rather than failed: a split trace is a fact about
        propagation, and putting it in the same column as a model that chose
        the wrong lane would send somebody to fix a prompt.
        """
        return self.error is None and not self.split and self.roots == 1

    @property
    def unreadable_because(self) -> str | None:
        """Why this tree cannot be scored, in one line, or ``None`` if it can."""
        if self.error is not None:
            return self.error
        if self.split:
            return (
                f"the turn arrived as {len(self.trace_ids)} traces; "
                "trace context did not propagate (#103)"
            )
        if self.roots != 1:
            return f"the recording holds {self.roots} chat.turn spans, not one"
        return None

    @property
    def tools(self) -> tuple[ToolName, ...]:
        """Every tool called, in call order, repeats included."""
        return tuple(call.tool for call in self.calls)

    @property
    def lanes(self) -> frozenset[Lane]:
        """Every lane entered."""
        return frozenset(call.lane for call in self.calls)

    def calls_to(self, tool: ToolName) -> tuple[ToolCall, ...]:
        """Every call to ``tool``, in call order."""
        return tuple(call for call in self.calls if call.tool is tool)


def from_readable_spans(spans: Sequence[ReadableSpan]) -> tuple[TraceSpan, ...]:
    """Adapt an in-process recording to :class:`TraceSpan`.

    Args:
        spans: What :class:`~chip_chat.otel.testing.SpanRecorder` collected.

    Returns:
        The same spans, in start order, in the shape :func:`read_trajectory`
        reads. Spans with no context are dropped: an unrecorded span cannot be
        joined to a parent or a trace, so it can only add noise.
    """
    adapted = [
        TraceSpan(
            name=span.name,
            span_id=_hex(span.context.span_id),
            parent_id=None if span.parent is None else _hex(span.parent.span_id),
            trace_id=_hex(span.context.trace_id),
            attributes=dict(span.attributes or {}),
            service=_service(span),
            started=span.start_time or 0,
        )
        for span in spans
        if span.context is not None
    ]
    return tuple(sorted(adapted, key=lambda span: span.started))


def read_trajectory(entry_id: str, spans: Sequence[TraceSpan]) -> Trajectory:
    """Read one turn's spans as a trajectory.

    Never raises on a tree it dislikes. A recording is produced by a deployment,
    a backend and somebody else's propagation; the failures it can arrive in are
    not enumerable from here, and every one of them is a thing to report rather
    than a thing to stop on.

    Args:
        entry_id: The dataset row these spans answer.
        spans: The turn's spans, in any order.

    Returns:
        The trajectory. Its :attr:`~Trajectory.readable` says whether anything
        downstream may score it.
    """
    ordered = sorted(spans, key=lambda span: span.started)
    if not ordered:
        return Trajectory(entry_id=entry_id, error="no spans were recorded for this turn")
    present = {span.span_id: span for span in ordered}
    steps = [span for span in ordered if span.name == SpanName.AGENT_STEP.value]
    step_of = {span.span_id: index for index, span in enumerate(steps)}

    calls: list[ToolCall] = []
    for span in ordered:
        if not span.name.startswith(TOOL_SPAN_PREFIX):
            continue
        tool = _tool(span.name)
        if tool is None:
            return Trajectory(
                entry_id=entry_id,
                error=f"{span.name} is not one of the eleven tool spans",
            )
        parent = None if span.parent_id is None else present.get(span.parent_id)
        if parent is not None and parent.name != SpanName.AGENT_STEP.value:
            return Trajectory(
                entry_id=entry_id,
                error=f"{span.name} nested inside {parent.name}, not agent.step",
            )
        calls.append(
            ToolCall(
                tool=tool,
                arguments=_arguments(span),
                step=-1 if span.parent_id is None else step_of.get(span.parent_id, -1),
                order=len(calls),
            )
        )

    return Trajectory(
        entry_id=entry_id,
        calls=tuple(calls),
        trace_ids=frozenset(span.trace_id for span in ordered),
        services=frozenset(span.service for span in ordered if span.service is not None),
        steps=len(steps),
        roots=sum(1 for span in ordered if span.name == SpanName.CHAT_TURN.value),
    )


def _tool(span_name: str) -> ToolName | None:
    """The tool a ``tool.<tool_name>`` span names, or ``None`` for an invented one."""
    try:
        return ToolName(span_name.removeprefix(TOOL_SPAN_PREFIX))
    except ValueError:
        return None


def _arguments(span: TraceSpan) -> Mapping[str, Any]:
    """The arguments a tool span recorded, as a mapping.

    ``tool_call`` serialises them to JSON on two attributes; the OpenInference
    one is read first because that is the one a backend is guaranteed to have
    kept. Anything that does not come back as a JSON object is read as no
    arguments at all rather than repaired -- a call whose parameters are a
    string is a call nobody can score the query of, and guessing at it here
    would put a shape in the report that the trace does not support.
    """
    for key in (SpanAttributes.TOOL_PARAMETERS, SpanAttributes.INPUT_VALUE):
        raw = span.attributes.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _service(span: ReadableSpan) -> str | None:
    """The ``service.name`` a recorded span was emitted under, where it has one."""
    resource = span.resource
    value = None if resource is None else resource.attributes.get("service.name")
    return value if isinstance(value, str) else None


def _hex(identifier: int) -> str:
    """A span or trace id as hex, so the shape does not depend on the SDK's ints."""
    return format(identifier, "x")
