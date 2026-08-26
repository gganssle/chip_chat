"""`make trace-boundary` is evidence only if the tree it sends is the right one."""

import textwrap

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chip_chat.otel.boundary import (
    agent_configuration,
    app_configuration,
    boundary_turn,
    main,
)
from chip_chat.otel.config import TelemetryConfig
from chip_chat.otel.schema import SpanName
from chip_chat.otel.service import turn_service_names
from chip_chat.otel.testing import SpanRecorder
from chip_chat.otel.tracing import build_tracer_provider

SESSION = "test-boundary"

THE_TREE = textwrap.dedent(
    """
    chat.turn
      guard.budget_check
      guard.content_safety
      agent.step
        llm.completion
        tool.search_menu_knowledge
          retriever.search
      agent.step
        llm.completion
      render.response
    """
).strip()


@pytest.fixture
def crossed() -> SpanRecorder:
    """One boundary turn, recorded from both providers into one exporter."""
    exporter = InMemorySpanExporter()

    def provider(config: TelemetryConfig) -> object:
        return build_tracer_provider(
            config, span_processors=(SimpleSpanProcessor(exporter),)
        )

    app = provider(app_configuration({}))
    agent = provider(agent_configuration({}))
    boundary_turn(SESSION, app_provider=app, agent_provider=agent)  # type: ignore[arg-type]
    return SpanRecorder(exporter)


def test_the_boundary_turn_is_the_rfc_tree(crossed: SpanRecorder) -> None:
    assert crossed.tree_text() == THE_TREE


def test_the_boundary_turn_is_one_trace_across_two_services(
    crossed: SpanRecorder,
) -> None:
    # The acceptance criterion of issue #103, asserted rather than eyeballed.
    assert len(crossed.trace_ids()) == 1
    assert crossed.services() == set(turn_service_names({}))


def test_the_split_runs_where_the_rfc_says_it_does(crossed: SpanRecorder) -> None:
    app_service, agent_service = turn_service_names({})
    app_side = {
        SpanName.CHAT_TURN,
        SpanName.GUARD_BUDGET_CHECK,
        SpanName.GUARD_CONTENT_SAFETY,
        SpanName.RENDER_RESPONSE,
    }
    for node in app_side:
        assert crossed.service_of(node.value) == app_service
    for name in ("tool.search_menu_knowledge", SpanName.RETRIEVER_SEARCH.value):
        assert crossed.service_of(name) == agent_service


def test_the_headers_that_crossed_are_returned() -> None:
    exporter = InMemorySpanExporter()
    providers = [
        build_tracer_provider(config, span_processors=(SimpleSpanProcessor(exporter),))
        for config in (app_configuration({}), agent_configuration({}))
    ]
    headers = boundary_turn(
        SESSION, app_provider=providers[0], agent_provider=providers[1]
    )

    assert "traceparent" in headers
    # The trace id on the wire is the trace id in the backend, which is the
    # whole reason the headers are worth printing when something has split.
    trace_id = next(iter(SpanRecorder(exporter).trace_ids()))
    assert f"{trace_id:032x}" in headers["traceparent"]


def test_the_agent_half_is_labelled_with_the_forced_service_name() -> None:
    # Foundry forces service.name to the agent resource's name and ignores
    # OTEL_SERVICE_NAME. The demo has to show that, not a name of our choosing.
    config = agent_configuration({"CHIP_CHAT_AGENT_NAME": "cilantro-agent"})
    assert config.resource_attributes()["service.name"] == "cilantro-agent"


def test_main_refuses_to_export_nowhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        TelemetryConfig,
        "from_env",
        classmethod(lambda cls, component, env=None: cls(component)),
    )

    assert main([]) == 1
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in capsys.readouterr().err


def test_main_sends_the_turn_and_names_both_services(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CHIP_CHAT_OTEL_CONSOLE", "")
    monkeypatch.setattr(
        TelemetryConfig,
        "from_env",
        classmethod(lambda cls, component, env=None: cls(component, console_export=True)),
    )

    assert main(["--pause", "0"]) == 0
    printed = capsys.readouterr().out
    for service in turn_service_names({}):
        assert service in printed
    assert "traceparent" in printed
