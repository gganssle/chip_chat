"""The criterion that matters: one turn, two services, one connected trace.

Issue #103's acceptance is a sentence about Phoenix -- *one visitor turn produces
one connected trace spanning both service names* -- and a criterion that can only
be checked by looking at a UI is a criterion that stops being checked. This file
is the same claim without a container in the way.
"""

import textwrap

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from chip_chat.otel.propagation import (
    BAGGAGE_KEYS,
    CARRIER_FIELDS,
    TRACE_CONTEXT_FIELDS,
    TurnContextError,
    continue_turn,
    turn_context_headers,
)
from chip_chat.otel.schema import SpanName, ToolName
from chip_chat.otel.service import agent_service_name, service_name, turn_service_names
from chip_chat.otel.spans import (
    agent_step,
    budget_check,
    chat_turn,
    content_safety,
    current_turn,
    llm_completion,
    render_response,
    resume_turn,
    tool_call,
)
from chip_chat.otel.testing import SpanRecorder, span_recorder

SESSION = "sess-boundary"

THE_TREE = textwrap.dedent(
    """
    chat.turn
      guard.budget_check
      guard.content_safety
      agent.step
        llm.completion
        tool.search_menu_knowledge
      render.response
    """
).strip()


def _app_half(recorder_exporter: InMemorySpanExporter) -> None:
    """One turn, with the middle of it emitted as if by another process."""
    with span_recorder("api", exporter=recorder_exporter):
        with chat_turn(
            session_id=SESSION,
            turn_index=3,
            message="is the barbacoa spicy?",
            persona_id="persona-7",
            demo_id="demo-42",
        ) as turn:
            with budget_check() as budget:
                budget.allow()
            with content_safety() as safety:
                safety.allow()

            headers = turn_context_headers()

            # The other process. A different provider, a different service name,
            # and nothing shared with the block above except the headers.
            with span_recorder(
                "agent",
                exporter=recorder_exporter,
                resource_attributes=(("service.name", agent_service_name({})),),
            ):
                with continue_turn(headers):
                    with agent_step(index=0):
                        with llm_completion(model="gpt-4o", provider="azure") as llm:
                            llm.record_usage(prompt_tokens=10, completion_tokens=2)
                        with tool_call(ToolName.SEARCH_MENU_KNOWLEDGE) as tool:
                            tool.record_result(["menu-barbacoa-0"])

            with render_response() as render:
                render.record_output("warm rather than hot")
            turn.record_output("warm rather than hot")


@pytest.fixture
def crossed() -> SpanRecorder:
    """A recording of one turn that crossed the app-to-agent boundary."""
    exporter = InMemorySpanExporter()
    _app_half(exporter)
    return SpanRecorder(exporter)


def test_the_turn_is_one_connected_trace(crossed: SpanRecorder) -> None:
    # The whole issue in one assertion. Two traces here means every Phase 9
    # trajectory eval is scoring an orphan.
    assert len(crossed.trace_ids()) == 1
    assert crossed.tree_text() == THE_TREE


def test_the_trace_spans_both_service_names(crossed: SpanRecorder) -> None:
    app_service, agent_service = turn_service_names({})

    assert crossed.services() == {app_service, agent_service}
    assert crossed.service_of(SpanName.CHAT_TURN.value) == app_service
    assert crossed.service_of(SpanName.RENDER_RESPONSE.value) == app_service
    assert crossed.service_of(SpanName.AGENT_STEP.value) == agent_service
    assert crossed.service_of(SpanName.LLM_COMPLETION.value) == agent_service


def test_every_span_carries_the_turn_identity_on_both_sides(
    crossed: SpanRecorder,
) -> None:
    # otel/README.md promises the identity on every span in a turn. A process
    # boundary is not an exception that promise contemplates.
    attributes = [dict(span.attributes or {}) for span in crossed.finished_spans()]
    assert len(attributes) == 7
    assert {each["session.id"] for each in attributes} == {SESSION}
    assert {each["chip_chat.turn.index"] for each in attributes} == {3}
    assert {each["chip_chat.persona.id"] for each in attributes} == {"persona-7"}
    assert {each["chip_chat.demo.id"] for each in attributes} == {"demo-42"}


def test_the_headers_are_the_w3c_set_and_nothing_else() -> None:
    with span_recorder("api"):
        with chat_turn(session_id=SESSION, turn_index=0):
            headers = turn_context_headers()

    assert set(headers) <= CARRIER_FIELDS
    assert TRACE_CONTEXT_FIELDS <= CARRIER_FIELDS
    assert "traceparent" in headers
    # Identity rides as baggage rather than as headers of our own invention, so
    # anything already speaking W3C carries it without being taught to.
    assert "baggage" in headers
    for key in BAGGAGE_KEYS[:2]:
        assert key in headers["baggage"]


def test_the_headers_merge_into_a_call_that_already_has_some() -> None:
    with span_recorder("api"):
        with chat_turn(session_id=SESSION, turn_index=0):
            headers = turn_context_headers({"content-type": "application/json"})

    assert headers["content-type"] == "application/json"
    assert "traceparent" in headers


def test_headers_are_looked_up_case_insensitively() -> None:
    with span_recorder("api"):
        with chat_turn(session_id=SESSION, turn_index=0):
            headers = dict(turn_context_headers())

    shouted = {key.upper(): value for key, value in headers.items()}
    with span_recorder("agent"):
        with continue_turn(shouted) as identity:
            assert identity.session_id == SESSION


def test_the_agent_side_reads_the_identity_off_the_wire() -> None:
    with span_recorder("api"):
        with chat_turn(
            session_id=SESSION, turn_index=9, persona_id="persona-1", demo_id="demo-2"
        ):
            headers = dict(turn_context_headers())

    with span_recorder("agent"):
        with continue_turn(headers) as identity:
            assert identity == current_turn()

    assert identity.session_id == SESSION
    assert identity.turn_index == 9
    assert identity.persona_id == "persona-1"
    assert identity.demo_id == "demo-2"


def test_injecting_outside_a_span_is_an_error() -> None:
    # Nothing recording means no traceparent, which means the agent opens a
    # trace of its own. Better to fail here than to find out in Phase 9.
    with span_recorder("api"):
        with pytest.raises(TurnContextError, match="no span is open"):
            turn_context_headers()


def test_continuing_a_turn_with_no_trace_context_is_an_error() -> None:
    with span_recorder("agent"):
        with pytest.raises(TurnContextError, match="no usable W3C trace context"):
            with continue_turn({"content-type": "application/json"}):
                pass


def test_continuing_a_turn_with_no_identity_is_an_error() -> None:
    with span_recorder("api"):
        with chat_turn(session_id=SESSION, turn_index=0):
            headers = dict(turn_context_headers())
    headers.pop("baggage")

    with span_recorder("agent"):
        with pytest.raises(TurnContextError, match=r"session\.id"):
            with continue_turn(headers):
                pass


def test_a_malformed_turn_index_is_an_error() -> None:
    with span_recorder("api"):
        with chat_turn(session_id=SESSION, turn_index=0):
            headers = dict(turn_context_headers())
    headers["baggage"] = f"session.id={SESSION},chip_chat.turn.index=third"

    with span_recorder("agent"):
        with pytest.raises(TurnContextError, match="not a turn index"):
            with continue_turn(headers):
                pass


def test_the_boundary_is_crossed_by_the_carrier_and_not_by_the_process() -> None:
    """The honest version of the test above.

    Both halves run in one interpreter, so an implementation that read the
    ambient context instead of the carrier would pass every assertion in this
    file. It would fail this one: the headers are emptied of trace context and
    the agent's spans have nothing legitimate to attach to.
    """
    with span_recorder("api"):
        with chat_turn(session_id=SESSION, turn_index=0):
            headers = dict(turn_context_headers())
            headers.pop("traceparent")
            with span_recorder("agent"):
                with pytest.raises(TurnContextError):
                    with continue_turn(headers):
                        pass


def test_agent_step_at_the_trace_root_is_still_refused() -> None:
    from chip_chat.otel.spans import SpanSchemaError

    # resume_turn is the only thing that makes agent.step legal in a process that
    # never opened chat.turn, and it is not a blanket amnesty.
    with span_recorder("agent"):
        with pytest.raises(SpanSchemaError, match=r"agent\.step must be a child"):
            with agent_step(index=0):
                pass


def test_resume_turn_restores_the_node_and_the_identity() -> None:
    from chip_chat.otel.spans import TurnIdentity

    identity = TurnIdentity(session_id=SESSION, turn_index=0)
    with span_recorder("agent") as recorder:
        with resume_turn(identity, node=SpanName.CHAT_TURN):
            assert current_turn() == identity
            with agent_step(index=0):
                pass
        assert current_turn() is None

    assert recorder.names() == (SpanName.AGENT_STEP.value,)


def test_service_names_are_the_pair_a_dashboard_has_to_filter_on() -> None:
    assert turn_service_names({}) == ("chip-chat-api", "chip-chat-agent")
    assert agent_service_name({"CHIP_CHAT_AGENT_NAME": "cilantro-agent"}) == (
        "cilantro-agent"
    )
    assert turn_service_names({"CHIP_CHAT_AGENT_NAME": "cilantro-agent"}) == (
        service_name("api"),
        "cilantro-agent",
    )
