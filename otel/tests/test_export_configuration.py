"""Decision D6: the observability vendor is a configuration value.

Issue #78 later has to prove that moving from Phoenix to Arize AX was only a
configuration change. These tests are what make that provable rather than
merely claimed -- and what will fail if somebody adds a vendor branch.
"""

import ast
from pathlib import Path

import pytest
from opentelemetry.sdk.trace.export import ConsoleSpanExporter

from chip_chat.otel.config import TelemetryConfig
from chip_chat.otel.exporters import build_span_exporters
from chip_chat.otel.tracing import build_tracer_provider

PHOENIX_LIKE = "http://localhost:6006/v1/traces"
HOSTED_LIKE = "https://otlp.example-vendor.test/v1/traces"

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "chip_chat" / "otel"


def test_from_env_appends_the_traces_path_to_a_base_endpoint() -> None:
    config = TelemetryConfig.from_env(
        "api", {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:6006"}
    )
    assert config.otlp_endpoint == PHOENIX_LIKE


def test_from_env_uses_a_signal_specific_endpoint_verbatim() -> None:
    config = TelemetryConfig.from_env(
        "api",
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://ignored.test",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": HOSTED_LIKE,
        },
    )
    assert config.otlp_endpoint == HOSTED_LIKE


def test_from_env_parses_headers_where_an_api_key_belongs() -> None:
    config = TelemetryConfig.from_env(
        "api",
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.example-vendor.test",
            "OTEL_EXPORTER_OTLP_HEADERS": "api_key=secret,space_id=abc",
        },
    )
    assert dict(config.otlp_headers) == {"api_key": "secret", "space_id": "abc"}


def test_from_env_rejects_a_malformed_header() -> None:
    with pytest.raises(ValueError, match="malformed OTLP header"):
        TelemetryConfig.from_env("api", {"OTEL_EXPORTER_OTLP_HEADERS": "nonsense"})


def test_from_env_with_an_empty_environment_exports_nowhere() -> None:
    config = TelemetryConfig.from_env("api", {})
    assert config.otlp_endpoint is None
    assert config.azure_monitor_connection_string is None
    assert config.exports_anywhere is False
    # Still a valid configuration: spans are built and schema-checked, then dropped.
    assert build_span_exporters(config) == ()


def test_switching_the_backend_is_only_a_configuration_change() -> None:
    """The D6 proof, in the form a later issue can cite."""
    local = TelemetryConfig(component="api", otlp_endpoint=PHOENIX_LIKE)
    hosted = TelemetryConfig(
        component="api",
        otlp_endpoint=HOSTED_LIKE,
        otlp_headers={"api_key": "secret", "space_id": "abc"},
    )

    local_exporters = build_span_exporters(local)
    hosted_exporters = build_span_exporters(hosted)

    # Same exporter, same count, same code path. Only the endpoint moved.
    assert [type(e) for e in local_exporters] == [type(e) for e in hosted_exporters]
    assert len(local_exporters) == 1


def _executable_tokens(module: Path) -> set[str]:
    """Every identifier and runtime string literal in ``module``, lowercased.

    Comments and every bare string expression -- module, class, function and
    attribute docstrings alike -- are excluded on purpose: prose explaining *why*
    the vendor is absent from the code is not the vendor being present in it.
    """
    tree = ast.parse(module.read_text())
    docstrings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node not in docstrings:
                tokens.add(node.value.lower())
        elif isinstance(node, ast.Name):
            tokens.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            tokens.add(node.attr.lower())
        elif isinstance(node, ast.alias | ast.FunctionDef | ast.ClassDef):
            tokens.add(node.name.lower())
        elif isinstance(node, ast.arg):
            tokens.add(node.arg.lower())
        elif isinstance(node, ast.ImportFrom) and node.module:
            tokens.add(node.module.lower())
    return tokens


@pytest.mark.parametrize("vendor", ["phoenix", "arize"])
def test_the_exporter_code_names_no_vendor(vendor: str) -> None:
    # If a product name ever appears in the code here, the endpoint has stopped
    # being the only thing that changes when the vendor does.
    for module in ("exporters.py", "config.py", "tracing.py"):
        tokens = _executable_tokens(PACKAGE_ROOT / module)
        offenders = [token for token in tokens if vendor in token]
        assert not offenders, f"{vendor} leaked into {module}: {offenders}"


def test_both_backends_are_configured_from_one_instrumentation() -> None:
    config = TelemetryConfig(
        component="api",
        otlp_endpoint=PHOENIX_LIKE,
        azure_monitor_connection_string="InstrumentationKey=00000000-0000-0000-0000-000000000000",
    )
    exporters = build_span_exporters(config)

    assert len(exporters) == 2
    kinds = {type(exporter).__name__ for exporter in exporters}
    assert kinds == {"OTLPSpanExporter", "AzureMonitorTraceExporter"}


def test_the_same_provider_feeds_every_configured_backend() -> None:
    config = TelemetryConfig(
        component="api",
        otlp_endpoint=PHOENIX_LIKE,
        azure_monitor_connection_string="InstrumentationKey=00000000-0000-0000-0000-000000000000",
        console_export=True,
    )
    provider = build_tracer_provider(config)
    try:
        # One instrumentation, three destinations: there is no second tracer and
        # no second span, only a wider fan-out.
        assert len(provider._active_span_processor._span_processors) == 3
    finally:
        provider.shutdown()


def test_console_export_is_opt_in() -> None:
    assert build_span_exporters(TelemetryConfig(component="api")) == ()
    exporters = build_span_exporters(
        TelemetryConfig(component="api", console_export=True)
    )
    assert [type(e) for e in exporters] == [ConsoleSpanExporter]


def test_resource_attributes_identify_the_component_and_environment() -> None:
    config = TelemetryConfig.from_env("data-gen", {"CHIP_CHAT_ENVIRONMENT": "production"})
    attributes = config.resource_attributes()

    assert attributes["service.name"] == "chip-chat-data-gen"
    assert attributes["service.namespace"] == "chip-chat"
    assert attributes["deployment.environment"] == "production"


def test_environment_defaults_to_local() -> None:
    assert TelemetryConfig.from_env("api", {}).environment == "local"


def test_a_malformed_component_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="lowercase and hyphen-separated"):
        TelemetryConfig(component="API")


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_console_flag_accepts_the_usual_truthy_spellings(value: str) -> None:
    config = TelemetryConfig.from_env("api", {"CHIP_CHAT_OTEL_CONSOLE": value})
    assert config.console_export is True


def test_spans_reach_both_backends_from_one_emission() -> None:
    """Both exporters receive the same trace -- the acceptance criterion, in a test."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from chip_chat.otel.spans import budget_check, chat_turn
    from chip_chat.otel.tracing import use_tracer_provider

    # Two independent exporters standing in for the two backends. The point is
    # not what they are, it is that one emission feeds both.
    infrastructure_backend = InMemorySpanExporter()
    agent_backend = InMemorySpanExporter()
    provider = build_tracer_provider(
        TelemetryConfig(component="api"),
        span_processors=(
            SimpleSpanProcessor(infrastructure_backend),
            SimpleSpanProcessor(agent_backend),
        ),
    )
    try:
        with use_tracer_provider(provider):
            with chat_turn(session_id="s", turn_index=0), budget_check() as guard:
                guard.allow()
    finally:
        provider.shutdown()

    def identity(exporter: InMemorySpanExporter) -> list[tuple[str, int]]:
        return sorted(
            (span.name, span.context.span_id)
            for span in exporter.get_finished_spans()
            if span.context is not None
        )

    assert identity(infrastructure_backend) == identity(agent_backend)
    assert [name for name, _ in identity(agent_backend)] == [
        "chat.turn",
        "guard.budget_check",
    ]
