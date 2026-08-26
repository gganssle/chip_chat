"""The surface is RFC-001 section 06's table, including its absent column.

The security-shaped assertions about that absence live in ``test_sabotage.py``,
where they read as the attacks they defeat. What is here is the rest of the
contract: the surface is exactly eleven, the read tools have no side effects,
the schemas are closed, and a call that does not match its tool is refused
rather than repaired.
"""

import pytest

from chip_chat.agent.surface import (
    CANCELLATION_REALITY,
    REDEMPTION_FINALITY,
    TOOL_SPECS,
    BoundArguments,
    Lane,
    ToolCallRejectedError,
    argument_names,
    spec,
    tools_for_lane,
)
from chip_chat.otel.schema import WRITE_TOOLS, ToolName


def test_the_surface_is_the_eleven_and_only_the_eleven() -> None:
    assert [tool.name for tool in TOOL_SPECS] == list(ToolName)


def test_every_lane_is_served() -> None:
    """A lane with no tool is a lane that silently answers from another one."""
    for lane in Lane:
        assert tools_for_lane(lane), lane


def test_the_six_read_tools_have_no_side_effects() -> None:
    """RFC-001 section 06's second clause, which the write flag encodes."""
    reads = [tool for tool in TOOL_SPECS if not tool.writes]

    assert len(reads) == 7  # the six read tools, plus propose_order
    assert {tool.name for tool in TOOL_SPECS if tool.writes} == set(WRITE_TOOLS)


def test_propose_order_is_a_read_that_mints_a_draft() -> None:
    """The row that reads oddly in the RFC table, and is right.

    ``propose_order`` writes a draft and yet the table's *Writes* column says
    no, because what it writes is not an action -- nothing is placed, nothing is
    charged, and no confirmation is needed to produce a card. It is the first
    half of the two-step, and calling it costs the visitor a tap to ignore.
    """
    propose = spec(ToolName.PROPOSE_ORDER)

    assert propose.lane is Lane.ACTION
    assert not propose.writes


def test_every_schema_is_closed() -> None:
    """``additionalProperties: false``, top level and nested.

    The provider's own validation is not the control -- ``BoundArguments`` is --
    but there is no reason to invite the extra field across the wire either.
    """
    for tool in TOOL_SPECS:
        assert tool.json_schema()["additionalProperties"] is False


def test_a_call_missing_a_required_argument_is_refused() -> None:
    with pytest.raises(
        ToolCallRejectedError, match="required argument 'query' is absent"
    ):
        spec(ToolName.SEARCH_MENU_KNOWLEDGE).bind({})


def test_a_call_with_the_wrong_json_type_is_refused() -> None:
    with pytest.raises(ToolCallRejectedError, match="must be a JSON string"):
        spec(ToolName.PLACE_ORDER).bind({"draft_id": 7})


def test_the_no_argument_tools_take_no_arguments() -> None:
    """They answer for whoever is bound, which is why they need nothing.

    Two of the eleven have an empty parameter list, and that is the identity
    guarantee at its most visible: *how many points do I have* is answerable
    without the model knowing who "I" is.
    """
    for name in (ToolName.GET_POINTS_BALANCE, ToolName.GET_USUAL_ORDER):
        assert spec(name).parameters == ()
        assert spec(name).bind({}).arguments == {}


def test_validation_runs_on_every_construction_path() -> None:
    """Not only through ``bind``.

    A check that only ``bind`` performs is a check a caller can walk around by
    building the object directly. This one is in ``__post_init__``.
    """
    with pytest.raises(ToolCallRejectedError):
        BoundArguments(
            spec=spec(ToolName.PLACE_ORDER),
            arguments={"draft_id": "d1", "confirmed": True},
        )


def test_argument_names_walks_nested_schemas() -> None:
    """Otherwise the identity check in ``test_sabotage`` would see only the top.

    ``propose_order`` takes one argument and hides three more levels under it,
    and a walk that stopped at the top would have declared the surface clean
    without looking at where a smuggled field would actually go.
    """
    names = argument_names(spec(ToolName.PROPOSE_ORDER))

    assert {"items", "item_id", "quantity", "selections", "modifier_item_id"} <= names


def test_the_two_invented_capabilities_carry_their_provenance() -> None:
    """``docs/action-surface.md`` section 10, in the definition rather than beside it.

    ``cancel_order`` models something the published FAQ refuses outright, and
    ``redeem_points`` rests on ids and a reward mapping the published record does
    not contain. Both are required by PRD T1. What must not happen is either one
    reading as published fact, so the note travels with the tool -- and says what
    removing it would cost, because ``cancel_order``'s exit path is a PRD change
    and that is cheap only while the tool stays separable.
    """
    for name in (ToolName.CANCEL_ORDER, ToolName.REDEEM_POINTS):
        invention = spec(name).invention
        assert invention is not None
        assert "action-surface.md" in invention

    assert spec(ToolName.SEARCH_MENU_KNOWLEDGE).invention is None


def test_the_two_sentences_the_invented_capabilities_owe_the_visitor() -> None:
    """Required, not optional -- ``docs/action-surface.md`` sections 7.2 and 7.3.

    They live beside the tools rather than in the ops API that prints them,
    because the reason each sentence exists is the invention note two lines up.
    Issue #63 imports them; it does not get to reword them.
    """
    assert "cannot normally cancel" in CANCELLATION_REALITY
    assert "cannot be undone" in REDEMPTION_FINALITY
    assert "CANCELLATION_REALITY" in str(spec(ToolName.CANCEL_ORDER).invention)


def test_cancel_order_shares_nothing_with_the_other_ten() -> None:
    """Its removal is one literal, and this test is what keeps that true.

    The witness on this ticket asked for it in as many words: the exit path is a
    PRD change dropping T1's cancellation clause, and that exit gets expensive
    once the tool is entangled. ``order_id`` appears nowhere else in the surface,
    so deleting the spec deletes the whole capability.
    """
    cancel = spec(ToolName.CANCEL_ORDER)
    others = [tool for tool in TOOL_SPECS if tool.name is not ToolName.CANCEL_ORDER]

    assert argument_names(cancel) == {"order_id"}
    for tool in others:
        assert "order_id" not in argument_names(tool)


def test_tool_definitions_are_the_wire_shape_the_model_endpoint_takes() -> None:
    definition = spec(ToolName.SEARCH_MENU_KNOWLEDGE).as_tool_definition()

    assert definition["type"] == "function"
    assert definition["function"]["name"] == "search_menu_knowledge"
    assert definition["function"]["parameters"]["required"] == ["query"]


def test_descriptions_are_long_enough_to_choose_between() -> None:
    """A cheap proxy for the one criterion a unit test cannot reach.

    Lane selection is measured for real by ``python -m chip_chat.agent.selection``
    against the deployed model. What is assertable offline is that no tool went
    out with a one-line description, which is how the confusable pairs get
    confused.
    """
    for tool in TOOL_SPECS:
        assert len(tool.description) > 120, tool.name
