"""Carrying one turn across the app-to-agent boundary.

Decision D8 put the agent in its own container, so the span tree of RFC-001
section 09 is now emitted by two processes. The app opens ``chat.turn``, runs the
guards and closes with ``render.response``; the agent container emits
``agent.step``, ``llm.completion``, ``tool.*`` and their children. Nothing in
OpenTelemetry joins those halves by itself -- a second process that simply starts
tracing starts a *second trace*.

**A split trace is not a degraded trace.** The parent/child structure is the
thing Phase 9's trajectory and tool-selection evaluations read; two unrelated
traces do not score badly, they score nothing. So both ends of this module fail
loudly rather than quietly emitting half a turn:

.. code-block:: python

    # In the app, inside the open chat.turn:
    headers = turn_context_headers()
    response = http.post(agent_url, json=payload, headers=dict(headers))

    # In the agent container, on the way in:
    with continue_turn(request.headers):
        with agent_step(index=0):
            ...

Two things travel, and they are different. **W3C trace context**
(``traceparent`` and ``tracestate``) is what makes the agent's spans children of
the app's ``chat.turn``. **Baggage** carries the turn's identity -- session id,
turn index, persona and demo id -- so that the agent's spans are stamped with the
same values the app's are; ``otel/README.md`` promises those on *every* span in a
turn, and a process boundary is not an exception the promise contemplates.

The propagator here is built explicitly rather than taken from
:func:`opentelemetry.propagate.inject`. The global one is assembled from
``OTEL_PROPAGATORS``, which means an environment variable set for an unrelated
reason could drop baggage, or trace context, out of the middle of a turn. This
one is fixed: W3C trace context and W3C baggage, always, in both directions.
"""

import os
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from types import MappingProxyType

from opentelemetry import baggage, trace
from opentelemetry import context as context_api
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.context import Context
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from chip_chat.otel.attributes import ChipChatAttributes, SpanAttributes
from chip_chat.otel.schema import SpanName
from chip_chat.otel.spans import TurnIdentity, current_turn, resume_turn

__all__ = [
    "BAGGAGE_KEYS",
    "CARRIER_FIELDS",
    "ENVIRONMENT_CARRIER",
    "TRACE_CONTEXT_FIELDS",
    "TurnContextError",
    "carrier_from_environment",
    "carrier_to_environment",
    "continue_turn",
    "turn_context_headers",
]

_TRACE_CONTEXT = TraceContextTextMapPropagator()
_BAGGAGE = W3CBaggagePropagator()
_PROPAGATOR = CompositePropagator([_TRACE_CONTEXT, _BAGGAGE])

TRACE_CONTEXT_FIELDS: frozenset[str] = frozenset(_TRACE_CONTEXT.fields)
"""The headers that join the two halves into one trace.

``traceparent`` and ``tracestate``. Without them there is no parent to attach to.
"""

CARRIER_FIELDS: frozenset[str] = frozenset(_PROPAGATOR.fields)
"""Every header this module reads and writes -- the two above, plus ``baggage``.

All W3C, and nothing of our own invention: a carrier this module produces is
readable by anything that speaks the standard, and one it consumes can have been
produced by anything that does.
"""

_SESSION_ID = SpanAttributes.SESSION_ID
_TURN_INDEX = ChipChatAttributes.TURN_INDEX
_PERSONA_ID = ChipChatAttributes.PERSONA_ID
_DEMO_ID = ChipChatAttributes.DEMO_ID

BAGGAGE_KEYS: tuple[str, ...] = (_SESSION_ID, _TURN_INDEX, _PERSONA_ID, _DEMO_ID)
"""Baggage entries carrying the turn's identity.

Deliberately the same keys the identity is stamped under as span attributes.
One vocabulary crosses the wire and lands on the spans, so there is no mapping
table to get wrong and nothing to keep in step.
"""


ENVIRONMENT_CARRIER: Mapping[str, str] = MappingProxyType(
    {
        "traceparent": "TRACEPARENT",
        "tracestate": "TRACESTATE",
        "baggage": "BAGGAGE",
    }
)
"""Header name to environment variable, for a boundary that is not an HTTP call.

The deployed boundary is a request and the carrier is its headers. A command-line
process has no headers, and the established convention for handing it trace
context anyway is these three variables -- the same ones ``otel-cli`` and the
OpenTelemetry shell tooling read. Naming them here means the demo that runs the
agent half in a container is not inventing a private protocol to do it.
"""


class TurnContextError(RuntimeError):
    """The turn did not survive the boundary, in one direction or the other.

    Raised on the way out when there is no span to be a parent, and on the way in
    when the carrier holds no parent to attach to. Both mean the same thing --
    the trace is about to split -- and both are configuration or wiring errors
    rather than runtime conditions, so neither is worth recovering from.
    """


def _identity_baggage(identity: TurnIdentity) -> dict[str, str]:
    entries = {
        _SESSION_ID: identity.session_id,
        _TURN_INDEX: str(identity.turn_index),
    }
    if identity.persona_id is not None:
        entries[_PERSONA_ID] = identity.persona_id
    if identity.demo_id is not None:
        entries[_DEMO_ID] = identity.demo_id
    return entries


def _identity_from(context: Context) -> TurnIdentity:
    """Rebuild the turn's identity from the baggage in ``context``.

    Raises:
        TurnContextError: If the session id is absent or the turn index is not a
            number. Either leaves the agent's half of the trace unattributable,
            which ``otel/README.md`` says does not happen.
    """
    entries = baggage.get_all(context)
    session_id = entries.get(_SESSION_ID)
    if not isinstance(session_id, str) or not session_id:
        raise TurnContextError(
            f"the carrier holds no {_SESSION_ID} baggage entry, so the agent's "
            "spans would carry no session id. It is set by turn_context_headers(), "
            "which must be called inside an open chat.turn."
        )
    raw_index = entries.get(_TURN_INDEX)
    try:
        turn_index = int(str(raw_index))
    except (TypeError, ValueError) as error:
        raise TurnContextError(
            f"{_TURN_INDEX} baggage entry is {raw_index!r}, which is not a turn index"
        ) from error

    def optional(key: str) -> str | None:
        value = entries.get(key)
        return value if isinstance(value, str) and value else None

    return TurnIdentity(
        session_id=session_id,
        turn_index=turn_index,
        persona_id=optional(_PERSONA_ID),
        demo_id=optional(_DEMO_ID),
    )


def carrier_to_environment(carrier: Mapping[str, str]) -> dict[str, str]:
    """Render a carrier as environment variables for a child process.

    Args:
        carrier: Headers from :func:`turn_context_headers`.

    Returns:
        Variables to add to the child's environment. Keys the convention has no
        name for are dropped rather than passed under an invented one.
    """
    return {
        variable: carrier[header]
        for header, variable in ENVIRONMENT_CARRIER.items()
        if carrier.get(header)
    }


def carrier_from_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read back what :func:`carrier_to_environment` wrote.

    Args:
        env: Environment mapping to read; defaults to :data:`os.environ`.

    Returns:
        A carrier, possibly empty. An empty one is not an error here --
        :func:`continue_turn` is where that is decided.
    """
    source = os.environ if env is None else env
    return {
        header: source[variable]
        for header, variable in ENVIRONMENT_CARRIER.items()
        if source.get(variable)
    }


def turn_context_headers(
    carrier: MutableMapping[str, str] | None = None,
) -> Mapping[str, str]:
    """Return the headers that carry the open turn to the agent.

    Call this *inside* the ``chat.turn`` the agent is being invoked for, and from
    within whatever span should be the agent's parent. What comes back is the W3C
    trace context of the current span plus the turn's identity as baggage.

    Args:
        carrier: Headers to add to, if the call already has some. Not mutated --
            a copy is returned.

    Returns:
        A read-only mapping of header name to value, ready to merge into an
        outbound request.

    Raises:
        TurnContextError: If no span is open. Injecting from a process with
            nothing recording produces a carrier with no ``traceparent``, and the
            agent would then open a trace of its own.
    """
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        raise TurnContextError(
            "no span is open, so there is no trace context to hand the agent. "
            "turn_context_headers() belongs inside the chat.turn the agent is "
            "being called for."
        )

    context = context_api.get_current()
    identity = current_turn()
    if identity is not None:
        for key, value in _identity_baggage(identity).items():
            context = baggage.set_baggage(key, value, context=context)

    headers: dict[str, str] = dict(carrier or {})
    _PROPAGATOR.inject(headers, context=context)
    return MappingProxyType(headers)


@contextmanager
def continue_turn(
    carrier: Mapping[str, str],
    *,
    parent: SpanName = SpanName.CHAT_TURN,
) -> Iterator[TurnIdentity]:
    """Rejoin, in this process, the turn ``carrier`` describes.

    For the duration of the block the agent's spans nest under the app's
    ``chat.turn`` and carry its session id, exactly as they would have if the
    whole turn had run in one process. Their ``service.name`` does not change and
    is not supposed to -- see :func:`chip_chat.otel.service.turn_service_names`
    for why that matters to anything filtering on it.

    Args:
        carrier: The inbound request's headers. Looked up case-insensitively,
            because HTTP header names are, and copied rather than consumed.
        parent: The node the app has open on the other side. ``chat.turn`` in
            every case RFC-001 section 09 describes.

    Yields:
        The turn's identity, as it was in the app.

    Raises:
        TurnContextError: If the carrier holds no valid ``traceparent``, or no
            session id. Both mean the trace is about to split in half, which is
            the failure this whole module exists to prevent -- so it is raised
            rather than papered over with a fresh trace.
    """
    lowered = {key.lower(): value for key, value in carrier.items()}
    # An empty starting Context, not the ambient one: what joins the two halves
    # has to be what came off the wire. Anything inherited in-process here would
    # make the boundary look like it works in a test that runs both halves in one
    # interpreter, and fail in the two processes that matter.
    context = _PROPAGATOR.extract(lowered, context=Context())

    span_context = trace.get_current_span(context).get_span_context()
    if not span_context.is_valid:
        raise TurnContextError(
            "no usable W3C trace context in the carrier "
            f"(looked for {', '.join(sorted(TRACE_CONTEXT_FIELDS))}), so this "
            "turn would open a second, unrelated trace. The app must send the "
            "headers turn_context_headers() returns."
        )

    identity = _identity_from(context)
    token = context_api.attach(context)
    try:
        with resume_turn(identity, node=parent):
            yield identity
    finally:
        context_api.detach(token)
