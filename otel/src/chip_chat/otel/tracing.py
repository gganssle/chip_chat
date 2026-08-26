"""Provider assembly and the one tracer the span helpers use.

Call sites never touch this module beyond :func:`configure_tracing` at start-up.
In particular no tracer is exported to application code: a tracer would let a
caller invent a span name, and the entire point of this package is that they
cannot. :mod:`chip_chat.otel.spans` reaches the tracer through :func:`get_tracer`
here, and it resolves at call time so tests can swap in an isolated provider
without fighting OpenTelemetry's global.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from chip_chat.otel.config import TelemetryConfig
from chip_chat.otel.exporters import build_span_exporters

__all__ = [
    "INSTRUMENTATION_NAME",
    "build_tracer_provider",
    "configure_tracing",
    "get_tracer",
    "shutdown_tracing",
    "use_tracer_provider",
]

INSTRUMENTATION_NAME = "chip_chat.otel"
"""The instrumentation scope every Chip Chat span is recorded under."""

_provider: TracerProvider | None = None


def build_tracer_provider(
    config: TelemetryConfig,
    *,
    span_processors: Sequence[SpanProcessor] = (),
) -> TracerProvider:
    """Build a provider that fans spans out to every backend ``config`` enables.

    Args:
        config: The resolved telemetry configuration.
        span_processors: Additional processors, appended after the exporters.
            Tests use this to attach an in-memory recorder.

    Returns:
        A provider carrying this component's resource attributes. Not installed
        anywhere -- :func:`configure_tracing` does that.
    """
    provider = TracerProvider(
        resource=Resource.create(dict(config.resource_attributes()))
    )
    for exporter in build_span_exporters(config):
        provider.add_span_processor(BatchSpanProcessor(exporter))
    for processor in span_processors:
        provider.add_span_processor(processor)
    return provider


def configure_tracing(
    config: TelemetryConfig,
    *,
    span_processors: Sequence[SpanProcessor] = (),
    set_global: bool = True,
) -> TracerProvider:
    """Install tracing for this process. Call once, at start-up.

    Args:
        config: The resolved telemetry configuration.
        span_processors: Additional processors, as in :func:`build_tracer_provider`.
        set_global: Also publish the provider as OpenTelemetry's global, so that
            third-party instrumentation (FastAPI, HTTP clients) joins the same
            traces. Leave it on outside tests.

    Returns:
        The installed provider, so the caller can shut it down deliberately.
    """
    global _provider
    provider = build_tracer_provider(config, span_processors=span_processors)
    if set_global:
        trace.set_tracer_provider(provider)
    _provider = provider
    return provider


def get_tracer() -> trace.Tracer:
    """Return the tracer the span helpers emit through.

    Falls back to OpenTelemetry's global when :func:`configure_tracing` has not
    run, which yields a no-op tracer rather than an error: a library import must
    never require that the application configured telemetry first.
    """
    if _provider is not None:
        return _provider.get_tracer(INSTRUMENTATION_NAME)
    return trace.get_tracer(INSTRUMENTATION_NAME)


def shutdown_tracing() -> None:
    """Flush and shut down the installed provider, if there is one."""
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


@contextmanager
def use_tracer_provider(provider: TracerProvider) -> Iterator[TracerProvider]:
    """Temporarily route the span helpers through ``provider``.

    Restores the previous provider on exit. Intended for tests; see
    :func:`chip_chat.otel.testing.span_recorder` for the usual entry point.
    """
    global _provider
    previous = _provider
    _provider = provider
    try:
        yield provider
    finally:
        _provider = previous
