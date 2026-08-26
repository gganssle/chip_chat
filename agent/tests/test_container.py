"""The image's entrypoint, which is also the agent half of the boundary."""

from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chip_chat.agent.container import main
from chip_chat.otel.boundary import agent_configuration, app_configuration, boundary_turn
from chip_chat.otel.propagation import carrier_to_environment
from chip_chat.otel.service import turn_service_names
from chip_chat.otel.testing import SpanRecorder
from chip_chat.otel.tracing import build_tracer_provider, use_tracer_provider


def _carrier() -> dict[str, str]:
    """A real carrier, produced by an app-side turn rather than hand-written."""
    captured: dict[str, str] = {}
    exporter = InMemorySpanExporter()
    providers = [
        build_tracer_provider(config, span_processors=(SimpleSpanProcessor(exporter),))
        for config in (app_configuration({}), agent_configuration({}))
    ]
    boundary_turn(
        "sess-container",
        app_provider=providers[0],
        agent_provider=providers[1],
        call_agent=lambda headers: captured.update(headers),
    )
    return captured


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Iterator[SpanRecorder]:
    """Route the entrypoint's spans into memory instead of at a backend.

    The entrypoint configures tracing for itself, as a container must, so the
    test replaces that step rather than working around it.
    """
    exporter = InMemorySpanExporter()
    monkeypatch.setattr("chip_chat.agent.container.configure_tracing", lambda _: None)
    monkeypatch.setattr("chip_chat.agent.container.shutdown_tracing", lambda: None)
    provider = build_tracer_provider(
        agent_configuration({}), span_processors=(SimpleSpanProcessor(exporter),)
    )
    try:
        with use_tracer_provider(provider):
            yield SpanRecorder(exporter)
    finally:
        provider.shutdown()


def test_check_reports_both_service_names(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == 0
    printed = capsys.readouterr().out
    for service in turn_service_names():
        assert service in printed


def test_check_is_the_default_action(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "service.name" in capsys.readouterr().out


def test_check_warns_when_the_image_exports_nowhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Immutable per agent version: an unconfigured exporter costs a version to
    # correct, so the image says so rather than starting quietly.
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("CHIP_CHAT_OTEL_CONSOLE", raising=False)

    assert main(["check"]) == 0
    assert "immutable per agent version" in capsys.readouterr().err


def test_the_agent_half_joins_the_turn_the_carrier_describes(
    recorded: SpanRecorder,
) -> None:
    carrier = _carrier()
    arguments = [f"--header={name}={value}" for name, value in carrier.items()]
    assert main([*arguments, "agent-half"]) == 0

    recorder = recorded
    # The container emitted only the agent's half...
    assert set(recorder.names()) == {
        "agent.step",
        "llm.completion",
        "tool.search_menu_knowledge",
        "retriever.search",
    }
    # ...and it landed inside the app's trace, which is the whole point.
    assert len(recorder.trace_ids()) == 1
    assert f"{next(iter(recorder.trace_ids())):032x}" in carrier["traceparent"]
    identified = recorder.attributes_of("tool.search_menu_knowledge")
    assert identified["session.id"] == "sess-container"


def test_the_carrier_can_arrive_in_the_environment(
    recorded: SpanRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # How the carrier reaches a process that has no request to carry it.
    for variable, value in carrier_to_environment(_carrier()).items():
        monkeypatch.setenv(variable, value)

    assert main(["agent-half"]) == 0
    assert "agent.step" in recorded.names()


def test_an_agent_half_with_no_carrier_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A container that shrugged and started its own trace would produce an
    # orphaned agent.step, which is exactly what issue #103 exists to prevent.
    for variable in ("TRACEPARENT", "TRACESTATE", "BAGGAGE"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(
        "chip_chat.agent.container.configure_tracing", lambda config: None
    )
    monkeypatch.setattr("chip_chat.agent.container.shutdown_tracing", lambda: None)

    assert main(["agent-half"]) == 1
    assert "did not reach this container" in capsys.readouterr().err


def test_a_malformed_header_argument_is_rejected() -> None:
    with pytest.raises(SystemExit, match="name=value"):
        main(["--header=nonsense", "agent-half"])
