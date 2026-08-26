"""Issue #64's second criterion: one trace holding the whole photo lane.

    A photo turn's trace holds image, structured description, and resolved SKUs
    together.

*Together* is the word that costs something. Stage 4 and stage 5 each ship a
``*_as_tool`` convenience that opens its own ``agent.step`` and its own
``tool.match_meal_from_photo``, which is right for a batch evaluation over one
stage and wrong for a turn: run back to back they produce two tool calls, and
the trace stops answering "what did the lane make of this photograph" in one
place. The first test here is the one that fails if
:class:`~chip_chat.vision.lane.PhotoLane` is ever taken apart into those two
calls again.

Every test runs inside the ``chat.turn`` the package's ``conftest`` opens, which
is the tree being enforced rather than described: none of these spans is legal
outside a turn.
"""

import textwrap

import pytest
from openinference.semconv.trace import (
    ImageAttributes,
    MessageAttributes,
    MessageContentAttributes,
    SpanAttributes,
)

from chip_chat.otel import (
    ChipChatAttributes,
    SpanSchemaError,
    ToolName,
    agent_step,
    tool_call,
)
from chip_chat.otel.testing import SpanRecorder
from chip_chat.vision.describe import (
    DescribeUnavailableError,
    DescriptionRejectedError,
)
from chip_chat.vision.lane import PhotoLane
from chip_chat.vision.matcher import Outcome
from chip_chat.vision.testing import (
    STUB_PHOTO_REF,
    STUB_VISION_USAGE,
    StubVisionModel,
    photo_lane,
)

EXPECTED_SUBTREE = textwrap.dedent(
    """
    tool.match_meal_from_photo
      vision.describe
      matcher.resolve
    """
).strip()
"""The subtree RFC-001 section 09 draws under the photo tool, and nothing else."""


@pytest.fixture
def lane() -> PhotoLane:
    """A whole lane built from doubles, with a photograph already stored."""
    return photo_lane()[0]


def tool_subtree(spans: SpanRecorder) -> str:
    """Render the one ``tool.*`` subtree the recording holds."""
    nodes = [
        node
        for root in spans.roots()
        for node in ([root, *root.children] if root.children else [root])
        if node.name.startswith("tool.")
    ]
    assert len(nodes) == 1, f"expected one tool span, found {len(nodes)}"
    return nodes[0].render()


# --- the criterion ----------------------------------------------------------


def test_both_stages_land_under_one_tool_span(
    lane: PhotoLane, spans: SpanRecorder
) -> None:
    lane.match_as_tool(STUB_PHOTO_REF)
    assert tool_subtree(spans) == EXPECTED_SUBTREE


def test_one_trace_holds_the_image_the_description_and_the_skus(
    lane: PhotoLane, spans: SpanRecorder
) -> None:
    """The three facts the criterion names, read off the one recording."""
    lane.match_as_tool(STUB_PHOTO_REF)

    vision = spans.attributes_of("vision.describe")
    assert vision[ChipChatAttributes.VISION_IMAGE_REF] == str(STUB_PHOTO_REF)
    assert '"protein"' in str(vision[SpanAttributes.OUTPUT_VALUE])

    assert spans.attributes_of("matcher.resolve")[
        ChipChatAttributes.MATCHER_RESOLVED_SKUS
    ]
    # And they are the same tool call, not two that happen to share a trace.
    assert (
        spans.span_named("vision.describe").parent
        == spans.span_named("matcher.resolve").parent
    )


def test_the_photograph_rides_as_a_multimodal_llm_input(
    lane: PhotoLane, spans: SpanRecorder
) -> None:
    """What makes Phoenix render a vision call rather than an opaque string."""
    lane.match_as_tool(STUB_PHOTO_REF)
    vision = spans.attributes_of("vision.describe")
    contents = (
        f"{SpanAttributes.LLM_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENTS}"
    )

    assert (
        vision[f"{contents}.0.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}"] == "image"
    )
    assert vision[
        f"{contents}.0.{MessageContentAttributes.MESSAGE_CONTENT_IMAGE}"
        f".{ImageAttributes.IMAGE_URL}"
    ] == str(STUB_PHOTO_REF)
    assert (
        vision[f"{contents}.1.{MessageContentAttributes.MESSAGE_CONTENT_TYPE}"] == "text"
    )


# --- tokens -----------------------------------------------------------------


def test_the_vision_span_carries_the_providers_token_counts(
    lane: PhotoLane, spans: SpanRecorder
) -> None:
    """``vision.describe`` is an LLM span, so it owes a count like any other."""
    lane.match_as_tool(STUB_PHOTO_REF)
    vision = spans.attributes_of("vision.describe")

    assert (
        vision[SpanAttributes.LLM_TOKEN_COUNT_PROMPT] == STUB_VISION_USAGE.prompt_tokens
    )
    assert (
        vision[SpanAttributes.LLM_TOKEN_COUNT_COMPLETION]
        == STUB_VISION_USAGE.completion_tokens
    )
    assert vision[SpanAttributes.LLM_TOKEN_COUNT_TOTAL] == STUB_VISION_USAGE.total
    assert spans.llm_token_usage().total == STUB_VISION_USAGE.total


def test_the_lanes_tokens_roll_up_onto_the_tool_span(
    lane: PhotoLane, spans: SpanRecorder
) -> None:
    """ "What does the photo lane cost per call" is one attribute, not a walk."""
    lane.match_as_tool(STUB_PHOTO_REF)
    tool = spans.attributes_of("tool.match_meal_from_photo")

    assert tool[ChipChatAttributes.TOKENS_TOTAL] == STUB_VISION_USAGE.total
    # Under our own key, never OpenInference's: a rollup sharing those keys
    # would double-count on every sum. See `ChipChatAttributes.TOKENS_TOTAL`.
    assert SpanAttributes.LLM_TOKEN_COUNT_TOTAL not in tool


def test_a_provider_reporting_no_usage_leaves_the_span_bare(
    spans: SpanRecorder,
) -> None:
    """Absent counts stay absent. A lane that looked free would be worse."""
    lane = photo_lane(model=StubVisionModel(usage=None))[0]
    lane.match_as_tool(STUB_PHOTO_REF)

    assert SpanAttributes.LLM_TOKEN_COUNT_TOTAL not in spans.attributes_of(
        "vision.describe"
    )
    assert ChipChatAttributes.TOKENS_TOTAL not in spans.attributes_of(
        "tool.match_meal_from_photo"
    )


# --- the schema is enforced, not documented ---------------------------------


def test_the_lane_refuses_to_run_outside_a_tool_call(lane: PhotoLane) -> None:
    with pytest.raises(SpanSchemaError, match=r"vision\.describe"):
        lane.match(STUB_PHOTO_REF)


def test_the_lane_runs_inside_a_tool_call_the_caller_opened(
    lane: PhotoLane, spans: SpanRecorder
) -> None:
    """How the agent calls it: the tool span is `dispatch`'s, not the lane's."""
    with agent_step(index=0), tool_call(ToolName.MATCH_MEAL_FROM_PHOTO):
        match = lane.match(STUB_PHOTO_REF)

    assert match.resolution.outcome is Outcome.RESOLVED
    assert tool_subtree(spans) == EXPECTED_SUBTREE


# --- a declining lane is still a legible trace ------------------------------


def test_a_declining_stage_four_marks_the_span_and_never_reaches_stage_five(
    spans: SpanRecorder,
) -> None:
    """RFC-001 section 10. The failure is on the span rather than swallowed."""
    lane = photo_lane(
        model=StubVisionModel(error=DescribeUnavailableError("deployment is down"))
    )[0]

    with pytest.raises(DescribeUnavailableError):
        lane.match_as_tool(STUB_PHOTO_REF)

    assert spans.span_named("vision.describe").status.is_ok is False
    # Stage 5 never ran: there is nothing to resolve, and a matcher.resolve span
    # over an absent description would be a trace telling a comfortable lie.
    assert "matcher.resolve" not in spans.names()


def test_a_rejected_description_still_records_what_it_cost(
    spans: SpanRecorder,
) -> None:
    """The expensive failure must not be the one that looks free.

    A deployment answering nonsense costs exactly what one answering correctly
    costs, and it tends to do it repeatedly. Recording the tokens only on the
    happy path would hide that -- and would leave an LLM span with no counts on
    it, which is indistinguishable from broken instrumentation.
    """
    lane = photo_lane(model=StubVisionModel(response="{}"))[0]

    with pytest.raises(DescriptionRejectedError):
        lane.match_as_tool(STUB_PHOTO_REF)

    vision = spans.attributes_of("vision.describe")
    assert vision[SpanAttributes.LLM_TOKEN_COUNT_TOTAL] == STUB_VISION_USAGE.total
    assert spans.span_named("vision.describe").status.is_ok is False
    # And the sum still works, rather than raising "no token counts".
    assert spans.llm_token_usage().total == STUB_VISION_USAGE.total
