"""The confirmation rule, which is the only thing here that is not a placeholder.

The data these drafts are built from is invented and will be deleted. The rule
that a write needs a confirmation the agent cannot grant itself is a launch gate
and is meant to survive, so it is tested as though it already mattered.
"""

import pytest

from chip_chat.agent.orders import OrderDesk, OrderRejectedError, RejectionCode

SESSION = "sess-1"
BOWL = [{"item_id": "BOWL-CHICKEN", "quantity": 1}]


class FakeClock:
    """A monotonic hand a test can push forward without waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def desk() -> OrderDesk:
    return OrderDesk()


def test_a_draft_is_not_confirmed_when_it_is_minted(desk: OrderDesk) -> None:
    draft = desk.propose(SESSION, BOWL)
    assert draft.confirmed is False
    assert draft.as_card()["requires_confirmation"] is True


def test_placing_an_unconfirmed_draft_is_refused(desk: OrderDesk) -> None:
    """The launch gate. A refusal, never a warning, and never repaired."""
    draft = desk.propose(SESSION, BOWL)
    with pytest.raises(OrderRejectedError) as raised:
        desk.place(SESSION, draft.draft_id)
    assert raised.value.code == RejectionCode.DRAFT_NOT_CONFIRMED


def test_a_confirmed_draft_places(desk: OrderDesk) -> None:
    draft = desk.propose(SESSION, BOWL)
    desk.confirm(SESSION, draft.draft_id)
    receipt = desk.place(SESSION, draft.draft_id)
    assert receipt.order_id.startswith("CC-")
    assert receipt.as_dict()["total"] == "10.70"


def test_a_draft_places_once(desk: OrderDesk) -> None:
    """Two presses of the button must not buy two lunches."""
    draft = desk.propose(SESSION, BOWL)
    desk.confirm(SESSION, draft.draft_id)
    desk.place(SESSION, draft.draft_id)
    with pytest.raises(OrderRejectedError) as raised:
        desk.place(SESSION, draft.draft_id)
    assert raised.value.code == RejectionCode.DRAFT_NOT_FOUND


def test_another_session_cannot_see_or_place_a_draft(desk: OrderDesk) -> None:
    """A well-formed id belonging to somebody else is a not-found, not a leak."""
    draft = desk.propose(SESSION, BOWL)
    assert desk.get("sess-2", draft.draft_id) is None
    assert desk.confirm("sess-2", draft.draft_id) is None
    with pytest.raises(OrderRejectedError) as raised:
        desk.place("sess-2", draft.draft_id)
    assert raised.value.code == RejectionCode.DRAFT_NOT_FOUND


def test_confirming_from_another_session_does_not_confirm_it(desk: OrderDesk) -> None:
    draft = desk.propose(SESSION, BOWL)
    desk.confirm("sess-2", draft.draft_id)
    with pytest.raises(OrderRejectedError) as raised:
        desk.place(SESSION, draft.draft_id)
    assert raised.value.code == RejectionCode.DRAFT_NOT_CONFIRMED


def test_a_draft_expires() -> None:
    clock = FakeClock()
    desk = OrderDesk(ttl_seconds=60.0, monotonic=clock)
    draft = desk.propose(SESSION, BOWL)
    desk.confirm(SESSION, draft.draft_id)
    clock.now = 61.0
    with pytest.raises(OrderRejectedError) as raised:
        desk.place(SESSION, draft.draft_id)
    assert raised.value.code == RejectionCode.DRAFT_EXPIRED


def test_an_item_not_on_the_menu_is_refused(desk: OrderDesk) -> None:
    with pytest.raises(OrderRejectedError) as raised:
        desk.propose(SESSION, [{"item_id": "BOWL-TOFU"}])
    assert raised.value.code == RejectionCode.ITEM_NOT_ORDERABLE
    # The rejection names what *is* orderable, so the model can ask usefully.
    assert "BOWL-CHICKEN" in raised.value.message


def test_an_empty_order_is_refused(desk: OrderDesk) -> None:
    with pytest.raises(OrderRejectedError) as raised:
        desk.propose(SESSION, [])
    assert raised.value.code == RejectionCode.EMPTY_ORDER


@pytest.mark.parametrize("quantity", [0, -1, 6, "many"])
def test_an_impossible_quantity_is_refused(desk: OrderDesk, quantity: object) -> None:
    with pytest.raises(OrderRejectedError) as raised:
        desk.propose(SESSION, [{"item_id": "BOWL-CHICKEN", "quantity": quantity}])
    assert raised.value.code == RejectionCode.QUANTITY_EXCEEDS_MAX


def test_totals_add_up_across_lines_and_quantities(desk: OrderDesk) -> None:
    draft = desk.propose(
        SESSION,
        [
            {"item_id": "BOWL-CHICKEN", "quantity": 2},
            {"item_id": "SIDE-GUACAMOLE", "quantity": 1},
        ],
    )
    assert str(draft.total) == "24.30"


def test_a_receipt_does_not_ask_for_confirmation_again(desk: OrderDesk) -> None:
    draft = desk.propose(SESSION, BOWL)
    desk.confirm(SESSION, draft.draft_id)
    receipt = desk.place(SESSION, draft.draft_id).as_dict()
    assert "requires_confirmation" not in receipt
    assert "order_id" in receipt


def test_every_card_carries_the_simulation_notice(desk: OrderDesk) -> None:
    """PRD T5. Both the draft and the receipt say it, because both are shown."""
    draft = desk.propose(SESSION, BOWL)
    assert "Simulated" in str(draft.as_card()["notice"])
    desk.confirm(SESSION, draft.draft_id)
    assert "Simulated" in str(desk.place(SESSION, draft.draft_id).as_dict()["notice"])
