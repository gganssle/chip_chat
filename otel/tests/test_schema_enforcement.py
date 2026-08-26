"""There must be no route from a call site to an off-schema span."""

import textwrap
from collections.abc import Callable
from contextlib import AbstractContextManager

import pytest

from chip_chat.otel import schema, spans
from chip_chat.otel.schema import OpsAction, SpanName, ToolName
from chip_chat.otel.spans import (
    SpanSchemaError,
    agent_step,
    budget_check,
    chat_turn,
    content_safety,
    cortex_analyst_query,
    llm_completion,
    matcher_resolve,
    ops_write,
    render_response,
    retriever_search,
    tool_call,
    vision_describe,
)
from chip_chat.otel.testing import span_recorder

Opener = Callable[[], AbstractContextManager[object]]

NOT_SPAN_OPENERS = {"current_turn", "resume_turn"}
"""Public helpers that manipulate the turn context without opening a span.

``resume_turn`` is the agent container's side of the process boundary: it
restores the turn a *different process* opened so that ``agent.step`` is legal
there. It emits nothing itself, so it is not a thirteenth node of the schema.
"""


def test_the_package_exports_no_tracer() -> None:
    # A tracer is a free-form span-name factory. If one ever appears in the
    # public surface, a call site can name a span whatever it likes and this
    # whole package stops being a schema.
    import chip_chat.otel as package

    assert "get_tracer" not in package.__all__
    assert "tracer" not in package.__all__
    assert not hasattr(package, "get_tracer")


def test_the_helpers_are_the_only_public_span_openers() -> None:
    openers = {
        name for name in spans.__all__ if name.islower() and name not in NOT_SPAN_OPENERS
    }
    assert openers == {
        "agent_step",
        "budget_check",
        "chat_turn",
        "content_safety",
        "cortex_analyst_query",
        "llm_completion",
        "matcher_resolve",
        "ops_write",
        "render_response",
        "retriever_search",
        "tool_call",
        "vision_describe",
    }


def test_there_is_one_helper_per_schema_node() -> None:
    # A node without a helper is a span nobody can emit; a helper without a node
    # is a span nobody's dashboard is watching. Neither may exist.
    helpers = {name for name in spans.__all__ if name.islower()} - NOT_SPAN_OPENERS
    assert len(helpers) == len(SpanName)


@pytest.mark.parametrize(
    "opener",
    [
        lambda: budget_check(),
        lambda: content_safety(),
        lambda: agent_step(index=0),
        lambda: render_response(),
    ],
)
def test_turn_children_refuse_to_open_at_the_trace_root(opener: Opener) -> None:
    with span_recorder(), pytest.raises(SpanSchemaError, match=r"chat\.turn"), opener():
        pass


def test_llm_completion_refuses_to_open_outside_an_agent_step() -> None:
    with span_recorder(), chat_turn(session_id="s", turn_index=0):
        with pytest.raises(SpanSchemaError, match=r"agent\.step"):
            with llm_completion(model="gpt-4o"):
                pass


def test_a_tool_refuses_to_open_directly_under_the_turn() -> None:
    with span_recorder(), chat_turn(session_id="s", turn_index=0):
        with pytest.raises(SpanSchemaError, match=r"agent\.step"):
            with tool_call(ToolName.GET_POINTS_BALANCE):
                pass


@pytest.mark.parametrize(
    "opener",
    [
        lambda: retriever_search(query="q"),
        lambda: cortex_analyst_query(question="q"),
        lambda: vision_describe(image_ref="b/1", model="m"),
        lambda: matcher_resolve(),
        lambda: ops_write(OpsAction.PLACE_ORDER, reference_id="d"),
    ],
)
def test_backends_refuse_to_open_outside_a_tool(opener: Opener) -> None:
    with span_recorder(), chat_turn(session_id="s", turn_index=0), agent_step(index=0):
        with pytest.raises(SpanSchemaError, match=r"tool\.\*"):
            with opener():
                pass


def test_a_turn_refuses_to_nest_inside_another_turn() -> None:
    with span_recorder(), chat_turn(session_id="s", turn_index=0):
        with pytest.raises(SpanSchemaError, match="trace root"):
            with chat_turn(session_id="s", turn_index=1):
                pass


def test_a_refused_span_is_not_emitted() -> None:
    with span_recorder() as recorder, chat_turn(session_id="s", turn_index=0):
        with pytest.raises(SpanSchemaError):
            with llm_completion(model="gpt-4o"):
                pass
    assert recorder.names() == ("chat.turn",)


def test_the_schema_position_unwinds_after_a_sibling_closes() -> None:
    # A stale context variable would let the *next* helper open in the wrong
    # place, which is the subtle way this kind of guard rots.
    with span_recorder() as recorder, chat_turn(session_id="s", turn_index=0):
        with agent_step(index=0):
            with tool_call(ToolName.GET_USUAL_ORDER):
                pass
            with llm_completion(model="gpt-4o"):
                pass
        with render_response():
            pass

    assert recorder.tree_text() == textwrap.dedent(
        """\
        chat.turn
          agent.step
            tool.get_usual_order
            llm.completion
          render.response"""
    )


def test_tool_call_takes_an_enum_and_not_a_string() -> None:
    # A string parameter is a typo waiting to become a span name. The enum is
    # what turns that typo into a failed import instead.
    for tool in ToolName:
        assert schema.tool_span_name(tool) in schema.SPAN_NAMES
