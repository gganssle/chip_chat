"""Turning a :class:`~chip_chat.otel.config.TelemetryConfig` into span exporters.

The dual export of decision D6 lives here, and it is deliberately dull: a list
comprehension over configured slots, with no product name and no ``if backend ==
...`` anywhere. Moving from Phoenix to Arize AX changes the value of
``otlp_endpoint``; it does not change a line of this file. Issue #78 has to be
able to prove that later, so the property is worth protecting now.

The Application Insights exporter is imported lazily. It drags in ``msal`` and
``cryptography``, which is a real cost to pay at import time in a package every
other package depends on, and a component with no connection string should not
pay it at all.
"""

from collections.abc import Sequence

from opentelemetry.sdk.trace.export import SpanExporter

from chip_chat.otel.config import TelemetryConfig

__all__ = ["build_span_exporters"]


def _otlp_exporter(config: TelemetryConfig) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    assert config.otlp_endpoint is not None
    return OTLPSpanExporter(
        endpoint=config.otlp_endpoint,
        headers=dict(config.otlp_headers),
        timeout=int(config.otlp_timeout_seconds) if config.otlp_timeout_seconds else None,
    )


def _azure_monitor_exporter(config: TelemetryConfig) -> SpanExporter:
    from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter

    return AzureMonitorTraceExporter(
        connection_string=config.azure_monitor_connection_string
    )


def _console_exporter(_: TelemetryConfig) -> SpanExporter:
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter

    return ConsoleSpanExporter()


def build_span_exporters(config: TelemetryConfig) -> Sequence[SpanExporter]:
    """Return one exporter per backend ``config`` enables, in a stable order.

    Args:
        config: The resolved telemetry configuration.

    Returns:
        The exporters to fan every span out to. Empty when nothing is
        configured, which is a valid state -- spans are still built and still
        schema-checked, they simply go nowhere.
    """
    exporters: list[SpanExporter] = []
    if config.otlp_endpoint:
        exporters.append(_otlp_exporter(config))
    if config.azure_monitor_connection_string:
        exporters.append(_azure_monitor_exporter(config))
    if config.console_export:
        exporters.append(_console_exporter(config))
    return tuple(exporters)
