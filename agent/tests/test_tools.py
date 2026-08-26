"""Each tool call opens exactly one span, and it is the right one.

The names asserted here are a schema, not a debugging convenience: Phase 9's
evaluations attach to them and a rename is a breaking change outside this
repository. That is the reason these tests assert on tree text rather than on
return values alone.
"""

import textwrap
from collections.abc import Mapping
from typing import Any

import pytest

from chip_chat.agent.hardcoded import ACCOUNT, MENU
from chip_chat.agent.model import ToolInvocation
from chip_chat.agent.orders import OrderDesk
from chip_chat.agent.tools import TOOL_SCHEMAS, TOOLS, dispatch
from chip_chat.otel import (
    ChipChatAttributes,
    ConfirmationState,
    ToolName,
    agent_step,
    chat_turn,
)
from chip_chat.otel.testing import span_recorder

SESSION = "sess-1"


@pytest.fixture
def desk() -> OrderDesk:
    return OrderDesk()


def call(tool: ToolName, **arguments: object) -> ToolInvocation:
    return ToolInvocation(call_id="c1", name=tool.value, arguments=arguments)


def dispatch_in_turn(
    invocation: ToolInvocation, *, desk: OrderDesk, session_id: str = SESSION
) -> Mapping[str, Any]:
    """Dispatch inside the parents ``tool.<name>`` is required to sit under.

    The schema refuses a tool span opened anywhere but under ``agent.step``, so
    there is no shorter way to call a tool -- which is the point of the check.
    """
    with chat_turn(session_id=session_id, turn_index=0), agent_step(index=0):
        return dispatch(invocation, session_id=session_id, desk=desk)


def test_every_offered_schema_names_a_real_tool() -> None:
    """A definition the model is shown but nothing implements is a dead end."""
    offered = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
    assert offered == {tool.value for tool in TOOLS}


def test_no_tool_takes_a_visitor_identifier() -> None:
    """RFC-001 section 05: the absence of the parameter is the enforcement."""
    forbidden = {"session_id", "visitor", "visitor_id", "persona_id", "demo_id"}
    for schema in TOOL_SCHEMAS:
        properties = schema["function"]["parameters"]["properties"]
        assert not forbidden & set(properties)


def test_menu_knowledge_nests_a_retriever_search(desk: OrderDesk) -> None:
    with span_recorder("agent") as spans:
        result = dispatch_in_turn(
            call(ToolName.SEARCH_MENU_KNOWLEDGE, query="is the barbacoa spicy?"),
            desk=desk,
        )
    assert (
        spans.tree_text()
        == textwrap.dedent("""
        chat.turn
          agent.step
            tool.search_menu_knowledge
              retriever.search
    """).strip()
    )
    assert result["passages"]


def test_a_menu_question_with_no_answer_still_emits_the_search(
    desk: OrderDesk,
) -> None:
    """Nothing found is a result. A trace that skipped the span would hide it."""
    with span_recorder("agent") as spans:
        result = dispatch_in_turn(
            call(ToolName.SEARCH_MENU_KNOWLEDGE, query="do you sell sushi"),
            desk=desk,
        )
    assert "retriever.search" in spans.names()
    assert result["passages"] == []
    assert "three items" in str(result["note"])


def test_the_points_balance_has_no_child_span(desk: OrderDesk) -> None:
    """Not every tool nests something, and the demo should say so out loud."""
    with span_recorder("agent") as spans:
        result = dispatch_in_turn(call(ToolName.GET_POINTS_BALANCE), desk=desk)
    assert (
        spans.tree_text()
        == textwrap.dedent("""
        chat.turn
          agent.step
            tool.get_points_balance
    """).strip()
    )
    assert result["points_balance"] > 0


def test_proposing_an_order_is_not_a_write(desk: OrderDesk) -> None:
    """A draft nests no ``ops`` span, because nothing was written."""
    with span_recorder("agent") as spans:
        result = dispatch_in_turn(
            call(ToolName.PROPOSE_ORDER, items=[{"item_id": "BOWL-CHICKEN"}]),
            desk=desk,
        )
    assert (
        spans.tree_text()
        == textwrap.dedent("""
        chat.turn
          agent.step
            tool.propose_order
    """).strip()
    )
    assert result["draft"]["requires_confirmation"] is True


def test_placing_a_confirmed_order_records_the_write(desk: OrderDesk) -> None:
    draft = desk.propose(SESSION, [{"item_id": "BOWL-CHICKEN"}])
    desk.confirm(SESSION, draft.draft_id)
    with span_recorder("agent") as spans:
        result = dispatch_in_turn(
            call(ToolName.PLACE_ORDER, draft_id=draft.draft_id),
            desk=desk,
        )
    assert (
        spans.tree_text()
        == textwrap.dedent("""
        chat.turn
          agent.step
            tool.place_order
              ops.place_order
    """).strip()
    )
    attributes = spans.attributes_of("ops.place_order")
    assert (
        attributes[ChipChatAttributes.OPS_CONFIRMATION_STATE]
        == ConfirmationState.CONFIRMED
    )
    assert attributes[ChipChatAttributes.OPS_REFERENCE_ID] == draft.draft_id
    assert result["receipt"]["order_id"].startswith("CC-")


def test_an_unconfirmed_write_is_recorded_as_rejected(desk: OrderDesk) -> None:
    """The launch-gate violation an eval has to be able to find."""
    draft = desk.propose(SESSION, [{"item_id": "BOWL-CHICKEN"}])
    with span_recorder("agent") as spans:
        result = dispatch_in_turn(
            call(ToolName.PLACE_ORDER, draft_id=draft.draft_id),
            desk=desk,
        )
    assert "ops.place_order" in spans.names()
    assert (
        spans.attributes_of("ops.place_order")[ChipChatAttributes.OPS_CONFIRMATION_STATE]
        == ConfirmationState.REJECTED
    )
    assert result["rejected"] == "DRAFT_NOT_CONFIRMED"


def test_a_rejection_is_a_result_and_not_an_exception(desk: OrderDesk) -> None:
    """The model has to be able to read it and ask the visitor something."""
    with span_recorder("agent"):
        result = dispatch_in_turn(
            call(ToolName.PROPOSE_ORDER, items=[{"item_id": "BOWL-TOFU"}]),
            desk=desk,
        )
    assert result["rejected"] == "ITEM_NOT_ORDERABLE"


def test_a_tool_nobody_wrote_emits_no_span_at_all(desk: OrderDesk) -> None:
    """An invented name must never reach the vocabulary the dashboards read."""
    with span_recorder("agent") as spans:
        result = dispatch_in_turn(
            ToolInvocation(call_id="c1", name="drop_database"),
            desk=desk,
        )
    assert "drop_database" not in spans.names()
    assert not any(name.startswith("tool.") for name in spans.names())
    assert result["rejected"] == "UNKNOWN_TOOL"


def test_a_tool_from_a_later_phase_is_refused_by_name(desk: OrderDesk) -> None:
    """It is a real tool, so it gets a real span -- and a typed refusal."""
    with span_recorder("agent") as spans:
        result = dispatch_in_turn(call(ToolName.MATCH_MEAL_FROM_PHOTO), desk=desk)
    assert "tool.match_meal_from_photo" in spans.names()
    assert result["rejected"] == "TOOL_NOT_IMPLEMENTED"


def test_a_session_id_in_the_arguments_is_refused_before_it_is_ignored(
    desk: OrderDesk,
) -> None:
    """A model that invented an identity argument does not get to send it.

    This test used to assert that the extra argument was *ignored*, and the
    draft not found. Both are still true and the second half below still checks
    it -- but the call now does not survive :mod:`chip_chat.agent.surface` at
    all, because ``session_id`` is not an argument ``place_order`` declares.
    Refusing is stronger than ignoring: ignoring means the field arrived and
    something chose not to read it, and every later reader has to make the same
    choice.
    """
    draft = desk.propose("sess-victim", [{"item_id": "BOWL-CHICKEN"}])
    desk.confirm("sess-victim", draft.draft_id)
    with span_recorder("agent"):
        smuggled = dispatch_in_turn(
            ToolInvocation(
                call_id="c1",
                name=ToolName.PLACE_ORDER.value,
                arguments={"draft_id": draft.draft_id, "session_id": "sess-victim"},
            ),
            desk=desk,
            session_id="sess-attacker",
        )
    assert smuggled["rejected"] == "ARGUMENTS_REJECTED"
    assert "session_id" in str(smuggled["detail"])

    # And with a call the surface does accept, the draft still belongs to
    # somebody else -- which is the property the identity argument was trying
    # to talk its way around.
    with span_recorder("agent"):
        legal = dispatch_in_turn(
            ToolInvocation(
                call_id="c2",
                name=ToolName.PLACE_ORDER.value,
                arguments={"draft_id": draft.draft_id},
            ),
            desk=desk,
            session_id="sess-attacker",
        )
    assert legal["rejected"] == "DRAFT_NOT_FOUND"


# --- get_usual_order ---------------------------------------------------------


def test_the_usual_order_comes_back_as_item_ids(desk: OrderDesk) -> None:
    """ "Reorder my usual" has to become a draft over real rows.

    A model handed only "a chicken burrito bowl with a side of guac" would have
    to turn prose back into identifiers, and a menu item arrived at by inference
    is the one thing this architecture is arranged to prevent. So the assertion
    is on the ids, not on the sentence.
    """
    result = dispatch_in_turn(
        ToolInvocation(call_id="c1", name=ToolName.GET_USUAL_ORDER.value, arguments={}),
        desk=desk,
    )

    assert [item["item_id"] for item in result["items"]] == list(ACCOUNT.favourite_items)
    assert all(item["item_id"] in MENU for item in result["items"])
    assert result["usual_order"] == ACCOUNT.usual_order


def test_the_usual_order_does_not_claim_a_confidence_it_cannot_compute(
    desk: OrderDesk,
) -> None:
    """The surface promises a confidence and says it is sometimes low.

    There is no gold mart behind this yet, so what is reported is the absence of
    one. A number invented here would be exactly the guess-presented-as-a-habit
    the tool's own description warns against.
    """
    result = dispatch_in_turn(
        ToolInvocation(call_id="c1", name=ToolName.GET_USUAL_ORDER.value, arguments={}),
        desk=desk,
    )

    assert result["confidence"] is None
    assert "not computed from order" in result["how_it_was_worked_out"]


def test_the_usual_order_names_nothing_the_menu_does_not_sell(
    desk: OrderDesk,
) -> None:
    """The account fixture and the menu are two lists; only their overlap ships."""
    result = dispatch_in_turn(
        ToolInvocation(call_id="c1", name=ToolName.GET_USUAL_ORDER.value, arguments={}),
        desk=desk,
    )

    for item in result["items"]:
        assert item["name"] == MENU[item["item_id"]].name
        assert item["unit_price"] == str(MENU[item["item_id"]].unit_price)
