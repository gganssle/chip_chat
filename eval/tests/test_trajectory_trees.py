"""Reading a turn back off its spans, and refusing a tree that cannot be believed.

The split-trace test is the one that matters most. The bead behind #74 says this
eval depends on #103 being correct, and the failure it warns about is not an
error anybody would see: every tool span is still present, so a reader that
collected them would produce a confident number over half a tree.
"""

import json

from openinference.semconv.trace import SpanAttributes

from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.trajectory.testing import turn_spans
from chip_chat.eval.trajectory.trees import TraceSpan, read_trajectory
from chip_chat.otel.schema import SpanName, ToolName, tool_span_name


def test_a_turn_is_read_back_as_its_calls_in_order() -> None:
    """The whole attachment: the calls come off the ``tool.<tool_name>`` spans."""
    spans = turn_spans(
        [
            (ToolName.GET_USUAL_ORDER, {}),
            (ToolName.PROPOSE_ORDER, {"items": []}),
        ]
    )

    trajectory = read_trajectory("row", spans)

    assert trajectory.readable
    assert trajectory.tools == (ToolName.GET_USUAL_ORDER, ToolName.PROPOSE_ORDER)
    assert trajectory.lanes == {Lane.PERSONALIZATION, Lane.ACTION}
    assert trajectory.steps == 1


def test_arguments_come_back_off_the_span() -> None:
    """What the query check reads. ``tool_call`` serialises them to JSON."""
    spans = turn_spans([(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "guacamole"})])

    call = read_trajectory("row", spans).calls[0]

    assert call.arguments == {"query": "guacamole"}


def test_a_call_records_which_step_it_sat_under() -> None:
    """Three calls in one step and three steps of one call each are different turns."""
    spans = turn_spans(
        [
            (ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "one"}),
            (ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "two"}),
        ],
        steps=2,
    )

    trajectory = read_trajectory("row", spans)

    assert trajectory.steps == 2
    assert [call.step for call in trajectory.calls] == [0, 1]


def test_a_split_turn_is_unreadable_rather_than_empty() -> None:
    """#103, as the failure this eval would otherwise score straight through.

    Both tool spans are present and both are attached to a trace the turn knows
    nothing about. The count is what makes it visible; ``readable`` is what stops
    it being counted.
    """
    spans = turn_spans([(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "q"})], split=True)

    trajectory = read_trajectory("row", spans)

    assert trajectory.calls
    assert trajectory.split
    assert not trajectory.readable
    assert "did not propagate" in (trajectory.unreadable_because or "")


def test_a_recording_with_no_spans_says_so() -> None:
    """Never an empty trajectory scored as *the model called nothing*."""
    trajectory = read_trajectory("row", ())

    assert not trajectory.readable
    assert trajectory.unreadable_because == "no spans were recorded for this turn"


def test_two_turns_in_one_recording_are_refused() -> None:
    """One row, one turn. Two roots means the recording is not one turn's."""
    spans = (
        *turn_spans([(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "q"})]),
        TraceSpan(
            name=SpanName.CHAT_TURN.value,
            span_id="turn-2",
            parent_id=None,
            trace_id="1",
            started=200,
        ),
    )

    trajectory = read_trajectory("row", spans)

    assert not trajectory.readable
    assert "2 chat.turn spans" in (trajectory.unreadable_because or "")


def test_a_tool_span_outside_a_step_is_refused() -> None:
    """RFC-001 section 09 puts every tool call under ``agent.step``.

    A tool span somewhere else is a tree that does not match the schema this
    eval attaches to, and scoring it would be scoring something else.
    """
    spans = (
        TraceSpan(
            name=SpanName.CHAT_TURN.value,
            span_id="turn",
            parent_id=None,
            trace_id="1",
        ),
        TraceSpan(
            name=tool_span_name(ToolName.PLACE_ORDER),
            span_id="tool",
            parent_id="turn",
            trace_id="1",
            started=1,
        ),
    )

    trajectory = read_trajectory("row", spans)

    assert not trajectory.readable
    assert "not agent.step" in (trajectory.unreadable_because or "")


def test_a_tool_span_naming_something_outside_the_eleven_is_refused() -> None:
    """The vocabulary is closed, and a span outside it is not a call to score."""
    spans = (
        TraceSpan(
            name=SpanName.CHAT_TURN.value,
            span_id="turn",
            parent_id=None,
            trace_id="1",
        ),
        TraceSpan(
            name=SpanName.AGENT_STEP.value,
            span_id="step-0",
            parent_id="turn",
            trace_id="1",
            started=1,
        ),
        TraceSpan(
            name="tool.order_a_pizza",
            span_id="tool",
            parent_id="step-0",
            trace_id="1",
            started=2,
        ),
    )

    trajectory = read_trajectory("row", spans)

    assert not trajectory.readable
    assert "not one of the eleven" in (trajectory.unreadable_because or "")


def test_arguments_that_are_not_a_json_object_are_read_as_none() -> None:
    """Never repaired: a call whose parameters are a string has no query to score."""
    spans = (
        TraceSpan(
            name=SpanName.CHAT_TURN.value,
            span_id="turn",
            parent_id=None,
            trace_id="1",
        ),
        TraceSpan(
            name=SpanName.AGENT_STEP.value,
            span_id="step-0",
            parent_id="turn",
            trace_id="1",
            started=1,
        ),
        TraceSpan(
            name=tool_span_name(ToolName.SEARCH_MENU_KNOWLEDGE),
            span_id="tool",
            parent_id="step-0",
            trace_id="1",
            started=2,
            attributes={SpanAttributes.TOOL_PARAMETERS: json.dumps("guacamole")},
        ),
    )

    assert read_trajectory("row", spans).calls[0].arguments == {}
