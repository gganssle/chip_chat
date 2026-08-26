"""``match_meal_from_photo``: the real lane, reached through the real dispatch.

``vision/tests/test_lane.py`` asserts the span tree the lane emits.
``api/tests/test_turn_trace.py`` asserts that a whole request produces it. This
module is the seam between them -- what the *model* is handed back, and what it
is not.

The two properties worth stating out loud:

**The model never sees a token count.** The lane's usage goes onto the tool span
as a rollup and is dropped from the tool result. A model told what a call cost
is a model that can be steered by the number, and none of the eleven tool
contracts includes one.

**The model never sees ``notes``.** RFC-001 makes stage 4's free-text field
display-only, and a model asked to answer from it is the most enthusiastic
parser there is. It goes to the renderer; the model gets catalogue rows.
"""

import json
from collections.abc import Mapping
from typing import Any

import pytest

from chip_chat.agent.model import ToolInvocation
from chip_chat.agent.orders import OrderDesk
from chip_chat.agent.tools import (
    PHOTO_UNAVAILABLE_MESSAGE,
    TOOLS,
    dispatch,
    offered_schemas,
    offered_tools,
)
from chip_chat.otel import ChipChatAttributes, ToolName, agent_step, chat_turn
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.vision.describe import DescribeUnavailableError
from chip_chat.vision.lane import PhotoLane
from chip_chat.vision.testing import (
    CONFIDENT_MEAL,
    STUB_PHOTO_REF,
    STUB_VISION_USAGE,
    StubVisionModel,
    photo_lane,
)

SESSION = "sess-photo"


@pytest.fixture
def desk() -> OrderDesk:
    return OrderDesk()


@pytest.fixture
def lane() -> PhotoLane:
    return photo_lane()[0]


def run(
    lane: PhotoLane | None, desk: OrderDesk, image_ref: str = str(STUB_PHOTO_REF)
) -> tuple[Mapping[str, Any], SpanRecorder]:
    """Dispatch the photo tool inside the parents the schema requires."""
    invocation = ToolInvocation(
        call_id="c1",
        name=ToolName.MATCH_MEAL_FROM_PHOTO.value,
        arguments={"image_ref": image_ref},
    )
    with (
        span_recorder("agent") as spans,
        chat_turn(session_id=SESSION, turn_index=0),
        agent_step(index=0),
    ):
        result = dispatch(invocation, session_id=SESSION, desk=desk, lane=lane)
    return result, spans


# --- what is offered --------------------------------------------------------


def test_the_photo_tool_is_absent_without_a_lane() -> None:
    assert offered_tools() == TOOLS
    assert ToolName.MATCH_MEAL_FROM_PHOTO not in offered_tools()


def test_the_photo_tool_is_offered_when_a_lane_is_wired(lane: PhotoLane) -> None:
    assert ToolName.MATCH_MEAL_FROM_PHOTO in offered_tools(lane=lane)
    names = {schema["function"]["name"] for schema in offered_schemas(lane=lane)}
    assert ToolName.MATCH_MEAL_FROM_PHOTO.value in names


def test_the_photo_tool_takes_a_reference_and_nothing_else(lane: PhotoLane) -> None:
    """RFC-001 section 07: the image does not cross a tool boundary."""
    schema = next(
        definition
        for definition in offered_schemas(lane=lane)
        if definition["function"]["name"] == ToolName.MATCH_MEAL_FROM_PHOTO.value
    )
    assert set(schema["function"]["parameters"]["properties"]) == {"image_ref"}


# --- what comes back --------------------------------------------------------


def test_a_resolved_photo_returns_catalogue_rows(
    lane: PhotoLane, desk: OrderDesk
) -> None:
    result, _ = run(lane, desk)

    assert result["outcome"] == "resolved"
    assert result["items"]
    assert all(item["item_id"] for item in result["items"])


def test_the_model_is_never_told_what_the_call_cost(
    lane: PhotoLane, desk: OrderDesk
) -> None:
    result, spans = run(lane, desk)

    serialised = json.dumps(result)
    assert "token" not in serialised
    assert str(STUB_VISION_USAGE.total) not in serialised
    # It is on the span instead, which is where a cost dashboard reads it.
    assert (
        spans.attributes_of("tool.match_meal_from_photo")[ChipChatAttributes.TOKENS_TOTAL]
        == STUB_VISION_USAGE.total
    )


def test_the_model_is_never_shown_the_display_only_notes(
    lane: PhotoLane, desk: OrderDesk
) -> None:
    result, _ = run(lane, desk)
    assert str(CONFIDENT_MEAL["notes"]) not in json.dumps(result)


def test_a_low_confidence_slot_becomes_a_question_rather_than_a_guess(
    desk: OrderDesk,
) -> None:
    """PRD V5. The tool hands the model the slots to ask about, not a nearest row."""
    unsure = dict(CONFIDENT_MEAL)
    unsure["protein"] = {"value": "chicken", "confidence": 0.11}
    lane = photo_lane(model=StubVisionModel(response=json.dumps(unsure)))[0]

    result, _ = run(lane, desk)

    assert result["outcome"] == "clarify"
    assert [item["slot"] for item in result["clarifications"]] == ["protein"]
    assert "items" not in result


# --- the lane declining -----------------------------------------------------


def test_a_declining_lane_is_a_tool_result_and_a_failed_span(desk: OrderDesk) -> None:
    """RFC-001 section 10: the lane fails, the conversation does not fail with it."""
    lane = photo_lane(
        model=StubVisionModel(error=DescribeUnavailableError("deployment is down"))
    )[0]

    result, spans = run(lane, desk)

    assert result["detail"] == PHOTO_UNAVAILABLE_MESSAGE
    assert spans.span_named("tool.match_meal_from_photo").status.is_ok is False


def test_an_invented_reference_is_refused_before_a_container_is_touched(
    lane: PhotoLane, desk: OrderDesk
) -> None:
    """A model that composed a path must fail at the parse, not at the store."""
    result, _ = run(lane, desk, image_ref="uploads/../functions/host.json")
    assert result["rejected"] == "NO_PHOTO"


def test_an_empty_reference_is_refused(lane: PhotoLane, desk: OrderDesk) -> None:
    result, _ = run(lane, desk, image_ref="")
    assert result["rejected"] == "NO_PHOTO"


def test_the_tool_is_unimplemented_when_no_lane_is_wired(desk: OrderDesk) -> None:
    """It should never be called -- it was not offered -- but a refusal is legible."""
    result, _ = run(None, desk)
    assert result["rejected"] == "TOOL_NOT_IMPLEMENTED"
