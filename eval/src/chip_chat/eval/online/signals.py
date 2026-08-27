"""One live turn, as the thing a monitor is allowed to look at.

An offline eval knows what the right answer was. An online eval does not, and
every honest thing this package can say follows from that one difference.
There is no register, no expected lane, no labelled refusal direction and no
ground truth of any kind on a stranger's question -- so a monitor may only fire
on something that is wrong **on its face**: a claim with nothing behind it, a
photo match that resolved nothing and escalated nothing, a refusal on a turn
whose own retrieval plainly answered it, an identifier belonging to somebody
else, a turn that cost more than the budget says a turn may cost.

:class:`LiveTurn` is that view. It is assembled from the spans a backend already
holds, and it carries nothing a backend could not tell you.

**It is built from the same two readers the offline evals use.**
:func:`~chip_chat.eval.trajectory.trees.read_trajectory` and
:func:`~chip_chat.eval.grounding.evidence.read_evidence` turn a span tree into
calls and passages; this module adds the three things a live turn has that a
dataset row does not -- what it cost, how long it took, and who it was for --
and nothing else. #74's module docstring anticipates this exactly: *a second
adapter is a function; a second reader is a second implementation of the metric*.
So the adapter from a backend's rows to :class:`~chip_chat.eval.trajectory.trees.
TraceSpan` is where a new backend is absorbed, and every metric above it is the
one the offline documents already report.

**Identity appears here and is never an input.** :attr:`LiveTurn.demo_id` is read
off the ``chat.turn`` span because the cross-visitor monitor is the reason this
whole system has a launch gate, and detecting a disclosure means knowing whose
turn it was. It is read from a span the app wrote; nothing model-reachable
supplies it, no tool takes it, and this package never passes it to anything.
That distinction is the system's primary guarantee and it survives being
observed.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from openinference.semconv.trace import SpanAttributes

from chip_chat.eval.golden.cases import DIETARY_WORDS
from chip_chat.eval.grounding.evidence import Evidence, read_evidence
from chip_chat.eval.trajectory.trees import TraceSpan, Trajectory, read_trajectory
from chip_chat.otel.attributes import ChipChatAttributes, DbAttributes
from chip_chat.otel.schema import SpanName

__all__ = ["LiveTurn", "read_turn"]

_TURN = SpanName.CHAT_TURN.value
_MATCHER = SpanName.MATCHER_RESOLVE.value
_ANALYST = SpanName.DB_CORTEX_ANALYST.value

_NANOS_PER_MILLI: Final = 1_000_000

END_TIME: Final = "chip_chat.span.end_time"
"""Where a backend adapter puts the root span's end timestamp, in nanoseconds.

Not an OpenTelemetry attribute: a span's end time is a property of the span
rather than of its attributes, and every backend exposes it in a different
field of a different row shape. So the adapter that turns a backend's rows
into :class:`~chip_chat.eval.trajectory.trees.TraceSpan` puts it here, and the
latency monitor reads one key rather than knowing about three backends.
Absent means *not measured*, never *fast* -- see
:func:`~chip_chat.eval.online.monitors._budget`.
"""


@dataclass(frozen=True, slots=True)
class LiveTurn:
    """What a backend can say about one production turn.

    Attributes:
        trace_id: The trace. What an alert names, so a human can open it.
        session_id: The conversation. Two turns in one session share it.
        demo_id: Whose turn this was. See the module docstring on why an
            identifier appears in an observability type and nowhere else.
        persona_id: Which archetype they were given.
        message: What the visitor said. Sampled turns hand this to a judge.
        reply: What came back.
        claim_class: What kind of claim the response declared, per D9.
        citations: The ids it carried.
        dropped_citations: Ids it named that the retriever never returned.
        trajectory: The tool calls, read off the ``tool.*`` spans.
        evidence: The passages, read off the ``retriever.search`` spans.
        total_tokens: What the turn cost, summed off the ``chat.turn`` span.
        duration_ms: How long it took, from the root span.
        resolved_skus: What the matcher landed on, where there was a photo.
        escalated: Whether the matcher asked rather than guessed.
        matched_photo: Whether there was a ``matcher.resolve`` span at all.
        foreign_identifiers: Identifiers appearing in the turn that are not
            this visitor's. Populated by the reader from the spans, never
            guessed from the prose.
        error: Why there is nothing here, in one line.
    """

    trace_id: str = ""
    session_id: str = ""
    demo_id: str = ""
    persona_id: str = ""
    message: str = ""
    reply: str = ""
    claim_class: str = ""
    citations: tuple[str, ...] = ()
    dropped_citations: tuple[str, ...] = ()
    trajectory: Trajectory | None = None
    evidence: Evidence | None = None
    total_tokens: int = 0
    duration_ms: float = 0.0
    resolved_skus: tuple[str, ...] = ()
    escalated: bool = False
    matched_photo: bool = False
    foreign_identifiers: tuple[str, ...] = ()
    error: str | None = None

    @property
    def readable(self) -> bool:
        """Whether this turn may be scored at all.

        A turn whose spans arrived as two traces is not scored, for the reason
        both offline evals refuse one: the retrieval is in one trace and the
        response in another, so nothing can show the passages belong to the
        answer. Issue #103, and ``make trace-boundary`` is the check.
        """
        if self.error is not None:
            return False
        evidence = self.evidence
        return evidence is None or evidence.readable

    @property
    def retrieved(self) -> int:
        """How many passages this turn had."""
        return 0 if self.evidence is None else len(self.evidence.passages)

    @property
    def searched(self) -> int:
        """How many ``retriever.search`` spans there were."""
        return 0 if self.evidence is None else self.evidence.searches

    @property
    def dietary(self) -> bool:
        """Whether the visitor's words touch the allergen and dietary subject.

        A **screen**, not a label. Offline, this flag comes off the golden case,
        which declares it; there is no such declaration on a stranger's
        question, so this is a keyword sweep over
        :data:`~chip_chat.eval.golden.cases.DIETARY_WORDS` and it is wrong in
        both directions. It is used for exactly one thing --
        :mod:`chip_chat.eval.online.sampling` samples these turns at 100%
        rather than at the ordinary rate -- and a screen that over-samples is
        the correct kind of wrong for that job. Nothing scores off it.
        """
        words = set(self.message.lower().replace("?", " ").replace(",", " ").split())
        return bool(words & DIETARY_WORDS)


def read_turn(
    spans: Sequence[TraceSpan], *, message: str = "", reply: str = ""
) -> LiveTurn:
    """Assemble one live turn from its span tree.

    Args:
        spans: The turn's spans, as any backend adapter produced them.
        message: What the visitor said, where the backend can supply it.
            Spans deliberately do not carry the visitor's prose beyond what the
            app chose to record, so this is a parameter rather than a read.
        reply: What came back, on the same terms.

    Returns:
        The turn. A tree with no ``chat.turn`` root comes back carrying an
        ``error`` rather than raising: a malformed trace in production is a
        thing that happens, and it must cost one turn rather than a batch.
    """
    roots = [span for span in spans if span.name == _TURN]
    if not roots:
        return LiveTurn(error="no chat.turn span in this trace")
    root = roots[0]
    trace_id = root.trace_id
    attributes = root.attributes
    matcher = [span for span in spans if span.name == _MATCHER]
    demo_id = _text(attributes, ChipChatAttributes.DEMO_ID)
    return LiveTurn(
        trace_id=trace_id,
        session_id=_text(attributes, SpanAttributes.SESSION_ID),
        demo_id=demo_id,
        persona_id=_text(attributes, ChipChatAttributes.PERSONA_ID),
        message=message,
        reply=reply,
        trajectory=read_trajectory(trace_id, spans),
        evidence=read_evidence(trace_id, spans),
        total_tokens=_number(attributes, ChipChatAttributes.TOKENS_TOTAL),
        duration_ms=_duration(root),
        resolved_skus=_strings(
            matcher[0].attributes if matcher else {},
            ChipChatAttributes.MATCHER_RESOLVED_SKUS,
        ),
        escalated=bool(
            matcher and matcher[0].attributes.get(ChipChatAttributes.MATCHER_ESCALATED)
        ),
        matched_photo=bool(matcher),
        foreign_identifiers=_foreign(spans, demo_id),
    )


def _foreign(spans: Sequence[TraceSpan], demo_id: str) -> tuple[str, ...]:
    """Demo identifiers appearing anywhere in the turn that are not this one's.

    Read off the spans rather than out of the reply, deliberately. A regular
    expression over prose finds a name and calls it a disclosure; a span
    carrying somebody else's identifier *is* one, whatever the prose says. The
    two places one can appear are another span's ``chip_chat.demo.id`` and the
    text of a Cortex Analyst query, which is the one place a generated statement
    could name a row that is not the caller's.
    """
    found: list[str] = []
    for span in spans:
        other = _text(span.attributes, ChipChatAttributes.DEMO_ID)
        if other and demo_id and other != demo_id and other not in found:
            found.append(other)
        if span.name != _ANALYST:
            continue
        query = str(span.attributes.get(DbAttributes.DB_QUERY_TEXT, ""))
        for token in _quoted(query):
            foreign = demo_id and token != demo_id and token.startswith("demo-")
            if foreign and token not in found:
                found.append(token)
    return tuple(found)


def _quoted(query: str) -> tuple[str, ...]:
    """Single-quoted literals in a SQL statement, in order."""
    parts = query.split("'")
    return tuple(parts[index] for index in range(1, len(parts), 2))


def _duration(span: TraceSpan) -> float:
    """The root span's wall time in milliseconds, where the adapter carried one.

    Zero where it did not. A monitor that treated a missing duration as a fast
    turn would report a latency breach as compliance, so
    :class:`~chip_chat.eval.online.monitors.Monitor` checks for zero rather
    than comparing it.
    """
    ended = span.attributes.get(END_TIME)
    if not isinstance(ended, int) or isinstance(ended, bool):
        return 0.0
    return max(0.0, (ended - span.started) / _NANOS_PER_MILLI)


def _text(attributes: Mapping[str, Any], key: str) -> str:
    value = attributes.get(key)
    return value if isinstance(value, str) else ""


def _number(attributes: Mapping[str, Any], key: str) -> int:
    value = attributes.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _strings(attributes: Mapping[str, Any], key: str) -> tuple[str, ...]:
    """A list-valued attribute, whether it arrived as a list or as JSON.

    Backends differ, and this one difference is not worth a second adapter.
    """
    value = attributes.get(key)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return (value,)
        if isinstance(decoded, list):
            return tuple(str(item) for item in decoded)
    return ()
