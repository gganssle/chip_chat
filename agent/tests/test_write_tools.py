"""The four write tools, and the two things the desk seam decides for them.

``agent/tests/test_tools.py`` drives the five tools the week-one slice offers
against :class:`~chip_chat.agent.orders.OrderDesk`. This file drives what a
*different* desk changes, using a double rather than the deployed one --
``agent/`` may not import ``api/``, and :class:`chip_chat.agent.desk.Desk` is a
Protocol precisely so that it does not have to.

Two properties, and both are about a trace rather than about a write.

**Which process opens ``ops.<action>``.** A desk that performs the write here is
the write, so this package emits the span. A desk that posts to the deployed ops
API is not: that service opens its own ``ops.<action>`` as a child of *this*
``tool.<name>`` from the trace context on the request. Emitting one on both sides
would put two gate decisions in one trace, and -- worse -- the ops API's edge
refuses any write whose parent span is not a tool span, so an ops span opened
here would make every remote write fail with ``TRACE_CONTEXT_REQUIRED``.

**Which tools the model is offered.** ``cancel_order``, ``redeem_points`` and
``update_preferences`` name rows in ``CHIP_CHAT.ACCOUNTS`` that three hardcoded
items cannot stand in for. They are offered when a desk says it can answer them
and withdrawn when it cannot, which is the same rule
:data:`chip_chat.agent.lanes.CONDITIONAL_TOOLS` applies to the read lanes and for
the same reason.
"""

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest

from chip_chat.agent.desk import ActionOutcome, OrderableMenu
from chip_chat.agent.model import ToolInvocation
from chip_chat.agent.orders import HARDCODED_MENU, OrderDesk, OrderRejectedError
from chip_chat.agent.tools import DESK_WRITES, dispatch, offered_schemas, offered_tools
from chip_chat.otel import OpsAction, ToolName
from chip_chat.otel.spans import agent_step, chat_turn
from chip_chat.otel.testing import SpanRecorder, span_recorder

SESSION = "sess-1"


class RemoteDesk:
    """A desk whose writes happen somewhere else. Satisfies ``Desk`` structurally.

    Records what it was asked, and answers the way
    :class:`~chip_chat.api.orderdesk.OpsDesk` does: a card the first time one of
    the three is called and a receipt once something has been confirmed. It never
    opens a span, because in the deployed system the span is the ops API's.
    """

    writes_here = False

    def __init__(self, *, confirmed: bool = False) -> None:
        self.confirmed = confirmed
        self.asked: list[tuple[str, Any]] = []

    def offers_every_write(self) -> bool:
        return True

    def orderable_menu(self) -> OrderableMenu:
        return OrderableMenu(item_ids=("CMG-101",), described="CMG-101 = Chicken Bowl")

    def available(self) -> bool:
        return True

    def propose(self, session_id: str, items: Sequence[Mapping[str, Any]]) -> Any:
        raise AssertionError("not exercised here")

    def confirm(self, session_id: str, reference: str) -> object | None:
        self.confirmed = True
        return object()

    def place(self, session_id: str, draft_id: str) -> Any:
        self.asked.append(("place", draft_id))
        if not self.confirmed:
            raise OrderRejectedError(
                "DRAFT_NOT_CONFIRMED", f"draft {draft_id!r} was never confirmed"
            )
        return _Receipt({"order_id": "ord-1", "reference_id": draft_id})

    def act(
        self, session_id: str, action: OpsAction, arguments: Mapping[str, Any]
    ) -> ActionOutcome:
        self.asked.append((action.value, dict(arguments)))
        if self.confirmed:
            return ActionOutcome(receipt={"ok": True, "action": action.value})
        return ActionOutcome(card={"confirmation_id": "cf-1", "action": action.value})


class _Receipt:
    def __init__(self, body: Mapping[str, Any]) -> None:
        self._body = body

    def as_dict(self) -> Mapping[str, Any]:
        return dict(self._body)


@pytest.fixture
def spans() -> Iterator[SpanRecorder]:
    with span_recorder("agent") as recorder:
        yield recorder


@pytest.fixture(autouse=True)
def turn(spans: SpanRecorder) -> Iterator[None]:
    with (
        chat_turn(session_id=SESSION, turn_index=0, message="do it"),
        agent_step(index=0),
    ):
        yield


def call(tool: ToolName, arguments: Mapping[str, Any], desk: Any) -> Mapping[str, Any]:
    """Dispatch one tool call against ``desk``, inside the open turn."""
    return dispatch(
        ToolInvocation(call_id="c1", name=tool.value, arguments=dict(arguments)),
        session_id=SESSION,
        desk=desk,
    )


# ---------------------------------------------------------------------------
# What is offered
# ---------------------------------------------------------------------------


def test_the_week_one_desk_offers_one_write_and_the_deployed_one_offers_four() -> None:
    """PRD T1 is met by the deployment and honestly unmet by the slice.

    A tool the model can see and nothing can answer is worse than an absent one,
    so the three that need a real account are withdrawn where there is none --
    and ``place_order`` stays in both, because withdrawing it would take the
    confirmation gate off the only path a visitor has to it.
    """
    local = set(offered_tools(desk=OrderDesk()))
    remote = set(offered_tools(desk=RemoteDesk()))

    assert set(DESK_WRITES).isdisjoint(local)
    assert set(DESK_WRITES) <= remote
    assert ToolName.PLACE_ORDER in local
    assert ToolName.PLACE_ORDER in remote


def test_the_schema_offers_the_vocabulary_the_desk_can_actually_price() -> None:
    """The enum *and* the description, which is the half that was missing.

    A model shown ten opaque ids and nothing else cannot compose a draft the
    store will accept. :mod:`chip_chat.agent.desk` records what that cost when it
    was found out on a deployment.
    """
    definitions = {
        schema["function"]["name"]: schema
        for schema in offered_schemas(desk=RemoteDesk())
    }
    items = definitions["propose_order"]["function"]["parameters"]["properties"]["items"]

    assert items["items"]["properties"]["item_id"]["enum"] == ["CMG-101"]
    assert "Chicken Bowl" in items["description"]


def test_the_week_one_schema_is_still_the_three_hardcoded_items() -> None:
    """Nothing about the slice changed, which is what makes it a slice."""
    definitions = {
        schema["function"]["name"]: schema for schema in offered_schemas(desk=OrderDesk())
    }
    items = definitions["propose_order"]["function"]["parameters"]["properties"]["items"]

    assert items["items"]["properties"]["item_id"]["enum"] == sorted(
        HARDCODED_MENU.item_ids
    )


# ---------------------------------------------------------------------------
# Which process opens the span
# ---------------------------------------------------------------------------


def test_a_local_desk_emits_the_ops_span_here(spans: SpanRecorder) -> None:
    """Unchanged: the week-one desk *is* the write, so this package records it."""
    desk = OrderDesk()
    draft = desk.propose(SESSION, [{"item_id": "BOWL-CHICKEN"}])
    desk.confirm(SESSION, draft.draft_id)

    call(ToolName.PLACE_ORDER, {"draft_id": draft.draft_id}, desk)

    assert "ops.place_order" in spans.names()


def test_a_remote_desk_emits_no_ops_span_here(spans: SpanRecorder) -> None:
    """The whole of :attr:`Desk.writes_here`.

    Not tidiness: ``continue_turn(..., parent=SpanName.TOOL)`` on the ops API's
    edge refuses a write whose parent is not a tool span, so an ops span opened
    here would make every remote write fail with ``TRACE_CONTEXT_REQUIRED``.
    """
    desk = RemoteDesk(confirmed=True)

    result = call(ToolName.PLACE_ORDER, {"draft_id": "draft-1"}, desk)

    assert "tool.place_order" in spans.names()
    assert "ops.place_order" not in spans.names()
    assert result["receipt"]["order_id"] == "ord-1"


def test_a_refused_remote_write_is_a_result_the_model_can_read(
    spans: SpanRecorder,
) -> None:
    """A refusal is a result, not an exception, and still emits no ops span here."""
    desk = RemoteDesk()

    result = call(ToolName.PLACE_ORDER, {"draft_id": "draft-1"}, desk)

    assert result["rejected"] == "DRAFT_NOT_CONFIRMED"
    assert "ops.place_order" not in spans.names()


# ---------------------------------------------------------------------------
# The three that name a row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ToolName.CANCEL_ORDER, {"order_id": "ord-9000001"}),
        (ToolName.REDEEM_POINTS, {"reward_id": "chips"}),
        (ToolName.UPDATE_PREFERENCES, {"prefs": {"display_name": "Sam"}}),
    ],
    ids=lambda value: getattr(value, "value", "args"),
)
def test_an_unconfirmed_write_comes_back_as_a_card_and_a_next_step(
    tool: ToolName, arguments: Mapping[str, Any]
) -> None:
    """One tool call, two possible answers, and the model is told which it got.

    That the model is invited to call again is the gate holding rather than the
    gate being talked past: the second call succeeds only because a request
    carrying the visitor's session arrived in between, and there is no argument
    on any of these three through which the model could assert one.
    """
    result = call(tool, arguments, RemoteDesk())

    assert result["requires_confirmation"] is True
    assert result["card"]["confirmation_id"] == "cf-1"
    assert "Confirm" in result["next_step"]
    assert "receipt" not in result


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ToolName.CANCEL_ORDER, {"order_id": "ord-9000001"}),
        (ToolName.REDEEM_POINTS, {"reward_id": "chips"}),
        (ToolName.UPDATE_PREFERENCES, {"prefs": {"display_name": "Sam"}}),
    ],
    ids=lambda value: getattr(value, "value", "args"),
)
def test_a_confirmed_write_comes_back_as_a_receipt_carrying_the_notice(
    tool: ToolName, arguments: Mapping[str, Any]
) -> None:
    """PRD T5: every action is simulated and the receipt says so."""
    result = call(tool, arguments, RemoteDesk(confirmed=True))

    assert result["receipt"]["ok"] is True
    assert "Simulated" in result["notice"]


def test_the_week_one_desk_refuses_the_three_rather_than_inventing_them() -> None:
    """Unreachable on any deployment, and typed anyway.

    ``offered_tools`` does not offer these against a desk that cannot answer
    them, so this is the refusal a caller that ignored that would meet -- a
    result the model could read, rather than an ``AttributeError``.
    """
    result = call(ToolName.REDEEM_POINTS, {"reward_id": "chips"}, OrderDesk())

    assert result["rejected"] == "TOOL_NOT_IMPLEMENTED"
