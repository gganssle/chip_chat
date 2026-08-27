"""What the turn actually retrieved, read off the ``retriever.search`` spans.

Issue #75 is specific about where the groundedness judge gets its evidence:
*the retrieved documents are on the ``retriever.search`` span, so the judge
scores against what the system really had, not against the corpus in general*.
That sentence is this module. A judge handed the corpus would score a system
that never opened it as grounded; a judge handed the passages the turn returned
scores the turn.

**The span is the schema attachment, the same way it is for #74.** RFC-001 §09
puts retrieval under the tool call that asked for it, and
:class:`~chip_chat.otel.schema.SpanName.RETRIEVER_SEARCH` is the frozen name.
The documents ride on it in OpenInference's flattened layout --
``retrieval.documents.0.document.id`` and its siblings -- which is what a
backend keeps and therefore what an online eval (#76) will read out of one.

**The span type is #74's, deliberately.** :class:`~chip_chat.eval.trajectory.
trees.TraceSpan` and :func:`~chip_chat.eval.trajectory.trees.from_readable_spans`
are already the adapter between a recording and a reader, and a second copy of
them here would be a second thing to keep in step with the SDK. This module
reads different spans out of the same tree; it does not need a different tree.

**A split turn is unscored here for a second reason.** #74 refuses a split trace
because the tool spans arrive unattached to the turn that caused them. The same
break does something worse to this eval: the ``retriever.search`` spans are in
the agent's half and the response is in the app's, so a reader that collected
them anyway would score a response against passages it cannot show belong to it.
Issue #103, ``make trace-boundary``, and :attr:`Evidence.readable` is where it
surfaces.

**The lane's own verdict is read as an opaque string.**
:mod:`chip_chat.search.lane` records a ``confidence`` in the span's metadata --
*grounded*, *low*, *none* -- which is the retriever saying whether an answer may
be drawn from what it found. It is carried here as text rather than as
:class:`chip_chat.search.retrieve.Confidence`, so that ``eval`` does not take a
dependency on ``search`` to read a value it only ever prints. Absent on a
recording whose lane never set one, and a report says *unreported* rather than
inventing a third meaning for it.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from openinference.semconv.trace import DocumentAttributes, SpanAttributes

from chip_chat.eval.trajectory.trees import TraceSpan
from chip_chat.otel.schema import SpanName

__all__ = ["Evidence", "Passage", "read_evidence"]

_DOCUMENTS = SpanAttributes.RETRIEVAL_DOCUMENTS


@dataclass(frozen=True, slots=True)
class Passage:
    """One retrieved passage, as the span recorded it.

    Attributes:
        id: The ``chunk_id``. What a citation resolves against -- see
            :class:`chip_chat.agent.envelope.Citation` -- and therefore the
            join between what the model named and what the retriever returned.
        content: The text the retriever handed back. What a judge reads.
        score: The ranking score, where the span carried one.
        metadata: Everything else the document carried, as it carried it:
            label, kind, source url, harvest date, the two scores and the
            lexical overlap, for a real retrieval.
    """

    id: str
    content: str = ""
    score: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Evidence:
    """What one turn had to be grounded in, and whether it can be believed.

    Attributes:
        entry_id: The dataset row this answers.
        passages: Every passage returned on the turn, in span order, across
            every search it made.
        searches: How many ``retriever.search`` spans there were. Zero is the
            observation that matters most: a turn that answered a menu question
            without searching cannot be grounded in anything, whatever it said.
        failed_searches: Searches whose span was marked failed. RFC-001 §10's
            outage path -- the knowledge lane declining is not the corpus being
            empty, and a report that conflated them would send somebody to look
            at a model.
        confidence: What the lane made of the best search's result, as the
            string it recorded. See the module docstring. ``None`` where no
            search reported one.
        trace_ids: Every trace id in the recording. One is correct.
        roots: How many ``chat.turn`` spans there were. One is correct.
        error: Why there is nothing here, in one line. ``None`` on success.
    """

    entry_id: str = ""
    passages: tuple[Passage, ...] = ()
    searches: int = 0
    failed_searches: int = 0
    confidence: str | None = None
    trace_ids: frozenset[str] = frozenset()
    roots: int = 0
    error: str | None = None

    @property
    def split(self) -> bool:
        """Whether the turn arrived as more than one trace. See the module docstring."""
        return len(self.trace_ids) > 1

    @property
    def readable(self) -> bool:
        """Whether this recording may be used as evidence about the turn."""
        return self.error is None and not self.split and self.roots == 1

    @property
    def unreadable_because(self) -> str | None:
        """Why it cannot be, in one line, or ``None`` where it can."""
        if self.error is not None:
            return self.error
        if self.split:
            return (
                f"the turn arrived as {len(self.trace_ids)} traces, so the "
                "retrieval cannot be shown to belong to the response (#103)"
            )
        if self.roots != 1:
            return f"the recording holds {self.roots} chat.turn spans, not one"
        return None

    @property
    def retrieved(self) -> bool:
        """Whether anything came back at all."""
        return bool(self.passages)

    @property
    def ids(self) -> frozenset[str]:
        """Every passage id the turn had. What a citation is resolvable against."""
        return frozenset(passage.id for passage in self.passages)


def read_evidence(entry_id: str, spans: Sequence[TraceSpan]) -> Evidence:
    """Read one turn's spans as the evidence it had.

    Never raises on a tree it dislikes, for the reason
    :func:`~chip_chat.eval.trajectory.trees.read_trajectory` gives: a recording
    is produced by a deployment, a backend and somebody else's propagation, and
    every way it can arrive broken is a thing to report rather than a thing to
    stop on.

    Args:
        entry_id: The dataset row these spans answer.
        spans: The turn's spans, in any order.

    Returns:
        The evidence. Its :attr:`Evidence.readable` says whether anything
        downstream may score against it.
    """
    ordered = sorted(spans, key=lambda span: span.started)
    if not ordered:
        return Evidence(entry_id=entry_id, error="no spans were recorded for this turn")

    searches = [span for span in ordered if span.name == SpanName.RETRIEVER_SEARCH.value]
    passages: list[Passage] = []
    for span in searches:
        passages.extend(_passages(span))
    return Evidence(
        entry_id=entry_id,
        passages=tuple(passages),
        searches=len(searches),
        failed_searches=sum(1 for span in searches if _failed(span)),
        confidence=_confidence(searches),
        trace_ids=frozenset(span.trace_id for span in ordered),
        roots=sum(1 for span in ordered if span.name == SpanName.CHAT_TURN.value),
    )


def _passages(span: TraceSpan) -> tuple[Passage, ...]:
    """The documents one ``retriever.search`` span recorded, in rank order.

    Read back out of OpenInference's flattened layout, which is what
    :meth:`~chip_chat.otel.spans.RetrieverRecorder.record_documents` writes and
    what a backend hands to an online eval. A document with no id is dropped:
    an unidentified passage cannot be what a citation resolved against, so it
    cannot be evidence about a citation either.
    """
    indices = sorted(_indices(span.attributes))
    found: list[Passage] = []
    for index in indices:
        base = f"{_DOCUMENTS}.{index}"
        identifier = span.attributes.get(f"{base}.{DocumentAttributes.DOCUMENT_ID}")
        if not isinstance(identifier, str) or not identifier:
            continue
        found.append(
            Passage(
                id=identifier,
                content=_text(
                    span.attributes, f"{base}.{DocumentAttributes.DOCUMENT_CONTENT}"
                ),
                score=_number(
                    span.attributes, f"{base}.{DocumentAttributes.DOCUMENT_SCORE}"
                ),
                metadata=_object(
                    span.attributes, f"{base}.{DocumentAttributes.DOCUMENT_METADATA}"
                ),
            )
        )
    return tuple(found)


def _indices(attributes: Mapping[str, Any]) -> set[int]:
    """Which document positions this span carries attributes for."""
    prefix = f"{_DOCUMENTS}."
    found: set[int] = set()
    for key in attributes:
        if not key.startswith(prefix):
            continue
        head = key[len(prefix) :].split(".", 1)[0]
        if head.isdigit():
            found.add(int(head))
    return found


def _failed(span: TraceSpan) -> bool:
    """Whether the lane recorded this search as an outage.

    Read off the metadata's ``declined`` flag rather than off the span status,
    because a :class:`~chip_chat.eval.trajectory.trees.TraceSpan` carries the
    attributes a backend kept and not the status object the SDK had.
    """
    return bool(_object(span.attributes, SpanAttributes.METADATA).get("declined"))


def _confidence(searches: Sequence[TraceSpan]) -> str | None:
    """What the lane made of its result, off the first search that said."""
    for span in searches:
        value = _object(span.attributes, SpanAttributes.METADATA).get("confidence")
        if isinstance(value, str) and value:
            return value
    return None


def _text(attributes: Mapping[str, Any], key: str) -> str:
    value = attributes.get(key)
    return value if isinstance(value, str) else ""


def _number(attributes: Mapping[str, Any], key: str) -> float | None:
    value = attributes.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _object(attributes: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """One JSON-encoded attribute, as a mapping.

    Anything that does not come back as a JSON object is read as nothing at all
    rather than repaired -- the rule
    :func:`~chip_chat.eval.trajectory.trees.read_trajectory` applies to a tool
    call's arguments, for the same reason: guessing at it here would put a
    finding in the report that the trace does not support.
    """
    raw = attributes.get(key)
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
