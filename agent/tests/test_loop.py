"""One turn, three shapes: a menu question, an account question, an order.

These are the three interactions issue #16 asks to be demonstrated end to end,
and the assertion in each case is the span tree rather than the wording of the
reply. The tree is the deliverable: "one readable ``chat.turn`` span tree per
interaction" is an acceptance criterion, and it is the thing every Phase 9
evaluation is later built on top of.

No model is called. :class:`~chip_chat.agent.testing.ScriptedModel` stands in
for one, which is what lets a whole tool-calling turn be asserted on without a
deployment, a credential or a token.
"""

import textwrap

import pytest
from openinference.semconv.trace import SpanAttributes

from chip_chat.agent.hardcoded import ACCOUNT, STORE
from chip_chat.agent.loop import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    Conversation,
    TurnResult,
    run_turn,
)
from chip_chat.agent.model import ModelReply, ToolInvocation
from chip_chat.agent.orders import OrderDesk
from chip_chat.agent.testing import ScriptedModel, answer, calls_tool
from chip_chat.otel import ToolName, chat_turn
from chip_chat.otel.testing import span_recorder

SESSION = "sess-1"


@pytest.fixture
def desk() -> OrderDesk:
    return OrderDesk()


@pytest.fixture
def conversation() -> Conversation:
    return Conversation(session_id=SESSION)


def one_turn(
    conversation: Conversation,
    message: str,
    model: ScriptedModel,
    desk: OrderDesk,
    *,
    max_steps: int = 5,
) -> TurnResult:
    """Run one turn inside a ``chat.turn``, as the request handler does.

    ``agent.step`` is refused outside a turn, so there is no way to exercise the
    loop that skips this -- which is the point of the parent check.
    """
    with chat_turn(
        session_id=conversation.session_id,
        turn_index=conversation.next_turn_index(),
        message=message,
    ) as turn:
        result = run_turn(
            conversation, message, model=model, desk=desk, max_steps=max_steps
        )
        turn.record_output(result.reply)
    return result


def test_the_prompt_comes_first_and_today_s_facts_come_second(
    conversation: Conversation,
) -> None:
    """Two system messages, and the split is the point.

    The first is versioned and invariant, so ``chip_chat.prompt.version``
    identifies bytes rather than a visitor. The second carries what actually
    varies -- the account, the menu, which tools are registered today -- and
    never reaches the digest.
    """
    prompt, context = conversation.messages[0], conversation.messages[1]

    assert prompt["role"] == context["role"] == "system"
    assert prompt["content"] == SYSTEM_PROMPT
    assert "three items" not in prompt["content"]
    assert "BOWL-CHICKEN" in context["content"]
    assert ACCOUNT.display_name in context["content"]


def test_the_versioned_prompt_says_nothing_about_this_menu_or_this_visitor() -> None:
    """Otherwise the version would move whenever a visitor did.

    A digest that changed because somebody was called Sam identifies nothing,
    and an Arize experiment grouping on it would be grouping on noise.
    """
    assert ACCOUNT.display_name not in SYSTEM_PROMPT
    assert STORE.name not in SYSTEM_PROMPT
    assert PROMPT_VERSION.startswith("v1+")


def test_a_menu_question_is_one_search_and_two_round_trips(
    conversation: Conversation, desk: OrderDesk
) -> None:
    model = ScriptedModel(
        calls_tool(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "is the barbacoa spicy?"}),
        answer("Warmly spiced rather than hot."),
    )
    with span_recorder("api") as spans:
        result = one_turn(conversation, "is the barbacoa spicy?", model, desk)
    assert result.reply == "Warmly spiced rather than hot."
    assert (
        spans.tree_text()
        == textwrap.dedent("""
        chat.turn
          agent.step
            llm.completion
            tool.search_menu_knowledge
              retriever.search
          agent.step
            llm.completion
    """).strip()
    )


def test_an_account_question_nests_nothing_under_its_tool(
    conversation: Conversation, desk: OrderDesk
) -> None:
    model = ScriptedModel(
        calls_tool(ToolName.GET_POINTS_BALANCE),
        answer("You have 1,340 points."),
    )
    with span_recorder("api") as spans:
        one_turn(conversation, "how many points do I have?", model, desk)
    assert (
        spans.tree_text()
        == textwrap.dedent("""
        chat.turn
          agent.step
            llm.completion
            tool.get_points_balance
          agent.step
            llm.completion
    """).strip()
    )


def test_an_order_is_two_turns_because_confirmation_is_a_second_request(
    conversation: Conversation, desk: OrderDesk
) -> None:
    """The write cannot happen in the turn that proposed it, by design."""
    propose = ScriptedModel(
        calls_tool(ToolName.PROPOSE_ORDER, {"items": [{"item_id": "BOWL-CHICKEN"}]}),
        answer("That is $10.70. Press Confirm and I will place it."),
    )
    with span_recorder("api"):
        first = one_turn(conversation, "a chicken bowl", propose, desk)
    assert first.card is not None
    assert first.receipt is False
    draft_id = str(first.card["draft_id"])

    # The visitor presses Confirm. The request marks the draft; nothing the
    # model said or could say has any bearing on it.
    desk.confirm(SESSION, draft_id)

    place = ScriptedModel(
        calls_tool(ToolName.PLACE_ORDER, {"draft_id": draft_id}),
        answer("Ordered. Simulated, of course."),
    )
    with span_recorder("api") as spans:
        second = one_turn(conversation, "yes", place, desk)
    assert second.receipt is True
    assert second.card is not None
    assert str(second.card["order_id"]).startswith("CC-")
    assert (
        spans.tree_text()
        == textwrap.dedent("""
        chat.turn
          agent.step
            llm.completion
            tool.place_order
              ops.place_order
          agent.step
            llm.completion
    """).strip()
    )


def test_tokens_are_summed_across_every_round_trip(
    conversation: Conversation, desk: OrderDesk
) -> None:
    """The spend cap settles this number, so a turn that lost a step undercounts."""
    model = ScriptedModel(
        calls_tool(ToolName.GET_POINTS_BALANCE, prompt_tokens=100, completion_tokens=10),
        answer("1,340.", prompt_tokens=200, completion_tokens=20),
    )
    with span_recorder("api") as spans:
        result = one_turn(conversation, "points?", model, desk)
    assert (result.prompt_tokens, result.completion_tokens) == (300, 30)
    assert result.total_tokens == 330
    assert len([name for name in spans.names() if name == "llm.completion"]) == 2


def test_the_tool_definitions_are_recorded_once_per_turn(
    conversation: Conversation, desk: OrderDesk
) -> None:
    """Arize compares the tool chosen against the tools offered, so the offer
    has to be on the span -- but repeating it every step says one thing three
    times and triples the size of a trace."""
    model = ScriptedModel(calls_tool(ToolName.GET_POINTS_BALANCE), answer("1,340."))
    with span_recorder("api") as spans:
        one_turn(conversation, "points?", model, desk)
    completions = [
        span for span in spans.finished_spans() if span.name == "llm.completion"
    ]
    with_tools = [
        span
        for span in completions
        if any(
            str(key).startswith(SpanAttributes.LLM_TOOLS)
            for key in (span.attributes or {})
        )
    ]
    assert len(with_tools) == 1


def test_the_loop_stops_at_its_step_ceiling(
    conversation: Conversation, desk: OrderDesk
) -> None:
    """An unbounded loop is a turn that can outspend a fixed reservation."""
    model = ScriptedModel(*[calls_tool(ToolName.GET_POINTS_BALANCE)] * 3)
    with span_recorder("api") as spans:
        result = one_turn(conversation, "points?", model, desk, max_steps=3)
    assert result.steps == 3
    assert model.call_count == 3
    assert "ask me that again" in result.reply
    assert spans.names().count("agent.step") == 3


def test_a_tool_result_reaches_the_next_request(
    conversation: Conversation, desk: OrderDesk
) -> None:
    """The model has to see what the tool said, or the second step is a guess."""
    model = ScriptedModel(calls_tool(ToolName.GET_POINTS_BALANCE), answer("1,340."))
    with span_recorder("api"):
        one_turn(conversation, "points?", model, desk)
    second_request = model.requests[1]
    tool_messages = [m for m in second_request if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "1340" in str(tool_messages[0]["content"])


def test_a_model_that_names_no_tool_answers_in_one_step(
    conversation: Conversation, desk: OrderDesk
) -> None:
    model = ScriptedModel(answer("Hello! I am Cilantro."))
    with span_recorder("api") as spans:
        result = one_turn(conversation, "hi", model, desk)
    assert result.steps == 1
    assert (
        spans.tree_text()
        == textwrap.dedent("""
        chat.turn
          agent.step
            llm.completion
    """).strip()
    )


def test_an_invented_tool_name_never_becomes_a_span(
    conversation: Conversation, desk: OrderDesk
) -> None:
    model = ScriptedModel(
        ModelReply(
            content=None,
            tool_calls=(ToolInvocation(call_id="c1", name="rm_rf"),),
            finish_reason="tool_calls",
        ),
        answer("I cannot do that."),
    )
    with span_recorder("api") as spans:
        one_turn(conversation, "delete everything", model, desk)
    assert not any(name.startswith("tool.") for name in spans.names())


def test_an_empty_reply_falls_back_rather_than_showing_nothing(
    conversation: Conversation, desk: OrderDesk
) -> None:
    model = ScriptedModel(answer("   "))
    with span_recorder("api"):
        result = one_turn(conversation, "hi", model, desk)
    assert result.reply.strip()


def test_a_step_that_said_nothing_leaves_nothing_in_the_history(
    conversation: Conversation, desk: OrderDesk
) -> None:
    """A reply with no prose and no tool call must not reach the next request.

    ``_assistant_message`` would write it as ``{"role": "assistant", "content":
    null}`` with nothing beside it, which the chat completions API rejects --
    and since the whole history is replayed on every request, that message ends
    not the turn but the *conversation*. This is bead ``chip-1sq``, and what it
    looked like in production was every turn after the bad one answering with
    the app's failure sentence.

    A reasoning model reaches this by running out of ``max_completion_tokens``
    while still thinking, which is why the reply is written here with
    ``finish_reason="length"`` rather than as an arbitrary malformed double.
    """
    model = ScriptedModel(ModelReply(content=None, finish_reason="length"))
    with span_recorder("api"):
        one_turn(conversation, "what should I order?", model, desk)

    assert not any(
        message.get("role") == "assistant" and message.get("content") is None
        for message in conversation.messages
    )


def test_the_conversation_after_a_silent_step_can_still_take_a_turn(
    conversation: Conversation, desk: OrderDesk
) -> None:
    """The turn after the bad one is the assertion, not the bad turn itself.

    The visitor always saw *something* for the truncated turn -- the fallback
    sentence -- so watching that turn alone showed a system degrading politely.
    What was actually broken was every turn after it, which is the only place
    the poisoned history is read.
    """
    model = ScriptedModel(
        ModelReply(content=None, finish_reason="length"),
        answer("A burrito bowl is a burrito without the tortilla."),
    )
    with span_recorder("api"):
        one_turn(conversation, "what should I order?", model, desk)
        second = one_turn(conversation, "what is a burrito bowl?", model, desk)

    assert "without the tortilla" in second.reply
    # What the second call was actually given, which is the thing Azure read.
    assert all(
        isinstance(message.get("content"), str)
        for message in model.requests[1]
        if message.get("role") == "assistant" and not message.get("tool_calls")
    )


def test_a_confirmed_draft_is_announced_to_the_model(
    conversation: Conversation, desk: OrderDesk
) -> None:
    """The model cannot see the button, so it has to be told the press happened.

    Without this it goes on politely refusing forever, which is exactly what the
    first deployed run of this slice did -- see docs/deployment.md.
    """
    model = ScriptedModel(answer("Ordered."))
    with (
        span_recorder("api"),
        chat_turn(session_id=SESSION, turn_index=0, message="yes"),
    ):
        run_turn(
            conversation,
            "yes",
            model=model,
            desk=desk,
            confirmed_draft_id="draft-abc",
        )
    system_notes = [
        message
        for message in model.requests[0]
        if message.get("role") == "system" and "draft-abc" in str(message["content"])
    ]
    assert len(system_notes) == 1
    assert "place_order" in str(system_notes[0]["content"])


def test_nothing_is_announced_when_nothing_was_confirmed(
    conversation: Conversation, desk: OrderDesk
) -> None:
    model = ScriptedModel(answer("Press Confirm first."))
    with (
        span_recorder("api"),
        chat_turn(session_id=SESSION, turn_index=0, message="just do it"),
    ):
        run_turn(conversation, "just do it", model=model, desk=desk)
    assert not any(
        "pressed Confirm" in str(message.get("content", ""))
        for message in model.requests[0]
    )
