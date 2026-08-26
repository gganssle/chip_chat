"""An in-memory harness for asserting on the span tree.

Ships with the package rather than living in ``otel/tests`` because the packages
that arrive later -- the agent, the API, the eval suite -- need to make the same
assertions about their own turns, and a contract test is only a contract if both
sides can run it.

.. code-block:: python

    with span_recorder() as spans:
        run_one_turn()
    assert spans.tree_text() == textwrap.dedent('''
        chat.turn
          guard.budget_check
          agent.step
            llm.completion
    ''').strip()
"""

import typing
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util.types import AttributeValue

from chip_chat.otel.config import TelemetryConfig
from chip_chat.otel.tracing import build_tracer_provider, use_tracer_provider

__all__ = ["SpanRecorder", "SpanTreeNode", "span_recorder"]


@dataclass(frozen=True, slots=True)
class SpanTreeNode:
    """One span and the spans that nested inside it, in start order."""

    name: str
    attributes: Mapping[str, AttributeValue]
    children: tuple["SpanTreeNode", ...] = field(default_factory=tuple)

    def render(self, indent: int = 0) -> str:
        """Render this subtree as indented text, two spaces per level."""
        lines = [f"{'  ' * indent}{self.name}"]
        lines.extend(child.render(indent + 1) for child in self.children)
        return "\n".join(lines)


class SpanRecorder:
    """Collects finished spans and reassembles them into their tree."""

    __slots__ = ("_exporter",)

    def __init__(self, exporter: InMemorySpanExporter) -> None:
        self._exporter = exporter

    def finished_spans(self) -> tuple[ReadableSpan, ...]:
        """Every span finished so far, in the order it finished."""
        return tuple(self._exporter.get_finished_spans())

    def names(self) -> tuple[str, ...]:
        """The names of every finished span, in finish order."""
        return tuple(span.name for span in self.finished_spans())

    def span_named(self, name: str) -> ReadableSpan:
        """Return the single span called ``name``.

        Raises:
            AssertionError: If there is not exactly one such span. Tests that
                mean "one of these" should say so explicitly.
        """
        matches = [span for span in self.finished_spans() if span.name == name]
        if len(matches) != 1:
            raise AssertionError(
                f"expected exactly one {name!r} span, found {len(matches)}"
            )
        return matches[0]

    def attributes_of(self, name: str) -> Mapping[str, AttributeValue]:
        """The attributes of the single span called ``name``."""
        return dict(self.span_named(name).attributes or {})

    def service_of(self, name: str) -> str | None:
        """The ``service.name`` the single span called ``name`` was emitted under.

        One turn crosses a process boundary and therefore carries two of these
        (see :func:`chip_chat.otel.service.turn_service_names`). This is how a
        test says *which* service emitted a node, rather than only that the node
        exists.
        """
        resource = self.span_named(name).resource
        value = resource.attributes.get("service.name") if resource else None
        return value if isinstance(value, str) else None

    def services(self) -> frozenset[str]:
        """Every ``service.name`` present in the recording."""
        return frozenset(
            str(span.resource.attributes["service.name"])
            for span in self.finished_spans()
            if span.resource is not None and "service.name" in span.resource.attributes
        )

    def trace_ids(self) -> frozenset[int]:
        """Every trace id in the recording.

        More than one means the trace split, which across the app-to-agent
        boundary is the failure decision D8 warns about -- so a test that cares
        asserts on the size of this set rather than on the tree alone.
        """
        return frozenset(
            span.context.trace_id
            for span in self.finished_spans()
            if span.context is not None
        )

    def roots(self) -> tuple[SpanTreeNode, ...]:
        """The recorded spans as trees, parents before children."""
        spans = sorted(self.finished_spans(), key=_start_time)
        children: dict[int | None, list[ReadableSpan]] = {}
        recorded = {_span_id(span) for span in spans}
        for span in spans:
            parent = span.parent.span_id if span.parent is not None else None
            # A span whose parent was never recorded is a root as far as this
            # recorder is concerned -- otherwise it would silently disappear.
            children.setdefault(parent if parent in recorded else None, []).append(span)

        def build(span: ReadableSpan) -> SpanTreeNode:
            return SpanTreeNode(
                name=span.name,
                attributes=dict(span.attributes or {}),
                children=tuple(
                    build(child) for child in children.get(_span_id(span), [])
                ),
            )

        return tuple(build(span) for span in children.get(None, []))

    def tree_text(self) -> str:
        """The whole recording as indented text, one span name per line.

        This is the assertion that fails when somebody renames a span, which is
        the entire reason the schema is worth writing down.
        """
        return "\n".join(root.render() for root in self.roots())

    def clear(self) -> None:
        """Discard everything recorded so far."""
        self._exporter.clear()


class _BorrowedExporter(SpanExporter):
    """A shared exporter that the provider using it does not own.

    Shutting a ``TracerProvider`` down shuts its exporters down with it, and an
    :class:`InMemorySpanExporter` that has been shut down silently drops
    everything exported afterwards. When two recorders share one exporter --
    which is how a test stands in for two processes -- the inner one closing
    would therefore erase the outer one's remaining spans. This wrapper is the
    ownership boundary: flush through, shut down not at all.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: typing.Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export(spans)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._inner.force_flush(timeout_millis)

    def shutdown(self) -> None:
        return None


def _span_id(span: ReadableSpan) -> int | None:
    return span.context.span_id if span.context is not None else None


def _start_time(span: ReadableSpan) -> int:
    return span.start_time or 0


@contextmanager
def span_recorder(
    component: str = "otel",
    *,
    resource_attributes: Sequence[tuple[str, str]] = (),
    exporter: InMemorySpanExporter | None = None,
) -> Iterator[SpanRecorder]:
    """Route :mod:`chip_chat.otel.spans` into memory for the duration.

    Exports nowhere else, restores the previous provider on exit, and uses a
    simple processor so spans are readable the instant they close.

    Args:
        component: The component name the recorded resource is labelled with.
        resource_attributes: Extra resource attributes, as ``(key, value)`` pairs.
        exporter: An exporter to record into rather than a fresh one. Nesting a
            second recorder on a shared exporter is how one test stands in for
            two processes: each half emits under its own ``service.name``, and
            the recording holds the whole turn. See ``test_propagation.py``.

    Yields:
        A :class:`SpanRecorder` over the spans emitted inside the block.
    """
    config = TelemetryConfig(
        component=component,
        extra_resource_attributes=dict(resource_attributes),
    )
    destination = InMemorySpanExporter() if exporter is None else exporter
    processor = SimpleSpanProcessor(
        destination if exporter is None else _BorrowedExporter(destination)
    )
    provider: TracerProvider = build_tracer_provider(config, span_processors=(processor,))
    try:
        with use_tracer_provider(provider):
            yield SpanRecorder(destination)
    finally:
        provider.shutdown()
