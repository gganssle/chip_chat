"""Two token vocabularies, and why the distinction is load-bearing.

Every actual model call records OpenInference's ``llm.token_count.*``. Every
span that merely *contains* model calls records ``chip_chat.tokens.*``. Keep
them apart and "sum the LLM spans across a trace" is exactly the provider's
reported usage for the turn; merge them and every ancestor is counted again,
the number silently doubles, and the cost dashboard is wrong in a way nothing
looks wrong about.

That is the whole of this module. The application-level proof that a real turn
adds up lives in ``api/tests/test_turn_trace.py``.
"""

import pytest

from chip_chat.otel.attributes import ChipChatAttributes, SpanAttributes
from chip_chat.otel.schema import ToolName
from chip_chat.otel.spans import (
    TokenUsage,
    agent_step,
    chat_turn,
    llm_completion,
    tool_call,
    vision_describe,
)
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.otel.tracing import get_tracer

PROMPT = 812
COMPLETION = 64
VISION_PROMPT = 274
VISION_COMPLETION = 91


def emit_turn(*, count_the_vision_call: bool = True) -> SpanRecorder:
    """One turn with a text completion and a vision call in it."""
    with span_recorder("otel") as spans:
        with chat_turn(session_id="sess-1", turn_index=0) as turn:
            with agent_step(index=0) as step:
                with llm_completion(model="gpt-4o") as llm:
                    llm.record_usage(prompt_tokens=PROMPT, completion_tokens=COMPLETION)
                with (
                    tool_call(ToolName.MATCH_MEAL_FROM_PHOTO) as tool,
                    vision_describe(image_ref="uploads/a.jpg", model="gpt-4o") as eye,
                ):
                    if count_the_vision_call:
                        eye.record_usage(
                            prompt_tokens=VISION_PROMPT,
                            completion_tokens=VISION_COMPLETION,
                        )
                        tool.record_token_rollup(
                            TokenUsage(
                                prompt_tokens=VISION_PROMPT,
                                completion_tokens=VISION_COMPLETION,
                            )
                        )
                step.record_token_rollup(
                    TokenUsage(prompt_tokens=PROMPT, completion_tokens=COMPLETION)
                )
            turn.record_token_rollup(REPORTED if count_the_vision_call else TEXT_ONLY)
    return spans


TEXT_ONLY = TokenUsage(prompt_tokens=PROMPT, completion_tokens=COMPLETION)
REPORTED = TokenUsage(
    prompt_tokens=PROMPT + VISION_PROMPT,
    completion_tokens=COMPLETION + VISION_COMPLETION,
)
"""What the two providers between them said the turn cost."""


# --- TokenUsage itself ------------------------------------------------------


def test_the_total_defaults_to_the_sum() -> None:
    assert TokenUsage(prompt_tokens=10, completion_tokens=3).total == 13


def test_a_provider_that_reports_its_own_total_is_believed() -> None:
    """Reasoning and cached tokens make a provider's total deliberately not the sum."""
    usage = TokenUsage(prompt_tokens=10, completion_tokens=3, total_tokens=40)
    assert usage.total == 40


def test_usage_adds_up() -> None:
    rolled = TokenUsage(prompt_tokens=10, completion_tokens=3) + TokenUsage(
        prompt_tokens=1, completion_tokens=2, total_tokens=9
    )
    assert (rolled.prompt_tokens, rolled.completion_tokens, rolled.total) == (11, 5, 22)


# --- the two vocabularies stay apart ----------------------------------------


def test_model_calls_record_the_openinference_keys() -> None:
    spans = emit_turn()

    for name in ("llm.completion", "vision.describe"):
        attributes = spans.attributes_of(name)
        assert SpanAttributes.LLM_TOKEN_COUNT_PROMPT in attributes
        assert SpanAttributes.LLM_TOKEN_COUNT_COMPLETION in attributes
        assert SpanAttributes.LLM_TOKEN_COUNT_TOTAL in attributes
        assert ChipChatAttributes.TOKENS_TOTAL not in attributes


def test_containers_record_ours_and_never_openinferences() -> None:
    spans = emit_turn()

    for name in ("chat.turn", "agent.step", "tool.match_meal_from_photo"):
        attributes = spans.attributes_of(name)
        assert ChipChatAttributes.TOKENS_TOTAL in attributes
        assert SpanAttributes.LLM_TOKEN_COUNT_TOTAL not in attributes, (
            f"{name} rolled up under OpenInference's keys, which double-counts "
            "every sum across the trace"
        )


def test_summing_the_llm_spans_gives_the_providers_total() -> None:
    measured = emit_turn().llm_token_usage()
    assert measured.prompt_tokens == REPORTED.prompt_tokens
    assert measured.completion_tokens == REPORTED.completion_tokens
    assert measured.total == REPORTED.total


def test_the_vision_call_is_selected_by_kind_rather_than_by_name() -> None:
    """A model call added to the schema later is covered without anyone remembering."""
    names = {span.name for span in emit_turn().llm_spans()}
    assert names == {"llm.completion", "vision.describe"}


# --- the assertion helper does its job --------------------------------------


def test_the_helper_passes_on_a_turn_that_adds_up() -> None:
    emit_turn().assert_token_counts_sum(REPORTED)


def test_the_helper_catches_a_model_call_that_recorded_nothing() -> None:
    """The hole this exists to find: it looks exactly like a cheap turn."""
    spans = emit_turn(count_the_vision_call=False)
    with pytest.raises(AssertionError, match=r"vision\.describe is an LLM span"):
        spans.assert_token_counts_sum(TEXT_ONLY)


def test_the_helper_catches_counts_that_are_present_but_wrong() -> None:
    spans = emit_turn()
    with pytest.raises(AssertionError, match="do not sum to the reported usage"):
        spans.assert_token_counts_sum(TokenUsage(prompt_tokens=1, completion_tokens=1))


def test_the_helper_catches_a_rollup_that_disagrees_with_its_own_spans() -> None:
    """Two dashboards that disagree, neither obviously wrong. Worse than no rollup."""
    with span_recorder("otel") as spans:
        with chat_turn(session_id="s", turn_index=0) as turn:
            with agent_step(index=0), llm_completion(model="gpt-4o") as llm:
                llm.record_usage(prompt_tokens=PROMPT, completion_tokens=COMPLETION)
            turn.record_token_rollup(TokenUsage(prompt_tokens=1, completion_tokens=1))

    with pytest.raises(AssertionError, match="rolled up 2 tokens"):
        spans.assert_token_counts_sum(TEXT_ONLY)


def test_the_helper_catches_a_turn_that_rolled_nothing_up_at_all() -> None:
    """A missing rollup is a hole, not a pass.

    The check used to skip any span without one, so a turn that recorded no
    rollup anywhere sailed through -- which is the failure it exists to find:
    "what did this conversation cost" answerable only by walking the trace.
    """
    with span_recorder("otel") as spans:
        with chat_turn(session_id="s", turn_index=0):
            with agent_step(index=0), llm_completion(model="gpt-4o") as llm:
                llm.record_usage(prompt_tokens=PROMPT, completion_tokens=COMPLETION)

    with pytest.raises(AssertionError, match="carries no token rollup"):
        spans.assert_token_counts_sum(TEXT_ONLY)


def test_the_turn_is_found_by_name_rather_than_by_having_no_parent() -> None:
    """Because a turn is not always the trace root.

    ASGI or HTTP-client instrumentation installed above ``chat.turn`` gives it a
    parent, and a check written against parentage would go quietly vacuous the
    day somebody adds one -- passing every turn, including the wrong ones.
    """
    with span_recorder("otel") as spans:
        # Resolved inside the recorder, so the outer span lands in the same
        # recording -- exactly as a real ASGI instrumentation's would.
        with get_tracer().start_as_current_span("GET /api/chat"):
            with chat_turn(session_id="s", turn_index=0) as turn:
                with agent_step(index=0), llm_completion(model="gpt-4o") as llm:
                    llm.record_usage(prompt_tokens=PROMPT, completion_tokens=COMPLETION)
                turn.record_token_rollup(TokenUsage(prompt_tokens=1, completion_tokens=1))

    assert spans.span_named("chat.turn").parent is not None
    with pytest.raises(AssertionError, match="rolled up 2 tokens"):
        spans.assert_token_counts_sum(TEXT_ONLY)


def test_usage_that_reported_no_total_stays_that_way_when_added() -> None:
    """So a measured total compares equal to a hand-written one."""
    running = TokenUsage(prompt_tokens=0, completion_tokens=0)
    assert running + TEXT_ONLY == TEXT_ONLY
