"""The confirmation record for the three writes that have no draft.

``api/tests/test_drafts.py`` holds the same four properties for orders. These
are the same rules on the lighter record: one visitor, one card, consent that
only a request can grant, and consent that goes stale.
"""

import pytest

from chip_chat.api.confirmations import (
    Confirmation,
    ConfirmationCode,
    ConfirmationLedger,
    ConfirmationRejectedError,
    preferences_reference,
)
from chip_chat.api.testing import FakeClock
from chip_chat.otel import OpsAction

MINE = "dm-000001"
THEIRS = "dm-000002"
ORDER = "ord-9000001"


@pytest.fixture
def ledger(clock: FakeClock) -> ConfirmationLedger:
    """A ledger on a clock the test drives, so a TTL needs no waiting."""
    return ConfirmationLedger(clock=clock)


def offer(ledger: ConfirmationLedger, demo_id: str = MINE) -> Confirmation:
    """Offer to cancel one order, which is the least interesting of the three."""
    return ledger.offer(
        demo_id, OpsAction.CANCEL_ORDER, ORDER, {"order_id": ORDER, "total": "12.50"}
    )


# --- an offer is not consent ------------------------------------------------


def test_a_fresh_offer_is_not_confirmed(ledger: ConfirmationLedger) -> None:
    assert offer(ledger).confirmed is False


def test_claiming_an_unconfirmed_card_is_the_launch_gate(
    ledger: ConfirmationLedger,
) -> None:
    offer(ledger)
    with pytest.raises(ConfirmationRejectedError) as refused:
        ledger.claim(MINE, OpsAction.CANCEL_ORDER, ORDER)
    assert refused.value.code is ConfirmationCode.NOT_CONFIRMED


def test_confirming_replaces_the_record_rather_than_mutating_it(
    ledger: ConfirmationLedger,
) -> None:
    record = offer(ledger)
    confirmed = ledger.confirm(MINE, record.confirmation_id)
    assert confirmed.confirmed is True
    assert record.confirmed is False


def test_a_confirmed_card_can_be_claimed_once(ledger: ConfirmationLedger) -> None:
    record = offer(ledger)
    ledger.confirm(MINE, record.confirmation_id)
    claimed = ledger.claim(MINE, OpsAction.CANCEL_ORDER, ORDER)
    assert claimed.confirmation_id == record.confirmation_id

    with pytest.raises(ConfirmationRejectedError) as again:
        ledger.claim(MINE, OpsAction.CANCEL_ORDER, ORDER)
    assert again.value.code is ConfirmationCode.NOT_FOUND


# --- a card belongs to one visitor -----------------------------------------


def test_another_visitors_confirmed_card_is_not_found(
    ledger: ConfirmationLedger,
) -> None:
    record = offer(ledger, THEIRS)
    ledger.confirm(THEIRS, record.confirmation_id)

    with pytest.raises(ConfirmationRejectedError) as refused:
        ledger.claim(MINE, OpsAction.CANCEL_ORDER, ORDER)
    assert refused.value.code is ConfirmationCode.NOT_FOUND


def test_a_stranger_cannot_confirm_somebody_elses_card(
    ledger: ConfirmationLedger,
) -> None:
    record = offer(ledger, THEIRS)
    with pytest.raises(ConfirmationRejectedError) as refused:
        ledger.confirm(MINE, record.confirmation_id)
    assert refused.value.code is ConfirmationCode.NOT_FOUND


# --- consent goes stale -----------------------------------------------------


def test_an_expired_card_cannot_be_claimed(
    ledger: ConfirmationLedger, clock: FakeClock
) -> None:
    record = offer(ledger)
    ledger.confirm(MINE, record.confirmation_id)
    clock.advance(901.0)

    with pytest.raises(ConfirmationRejectedError) as refused:
        ledger.claim(MINE, OpsAction.CANCEL_ORDER, ORDER)
    assert refused.value.code is ConfirmationCode.EXPIRED


def test_an_expired_card_cannot_be_confirmed(
    ledger: ConfirmationLedger, clock: FakeClock
) -> None:
    record = offer(ledger)
    clock.advance(901.0)
    with pytest.raises(ConfirmationRejectedError) as refused:
        ledger.confirm(MINE, record.confirmation_id)
    assert refused.value.code is ConfirmationCode.EXPIRED


def test_a_clock_that_steps_backwards_does_not_resurrect_a_card(
    ledger: ConfirmationLedger, clock: FakeClock
) -> None:
    """Expiry reads the monotonic hand, so wall time cannot undo it."""
    record = offer(ledger)
    ledger.confirm(MINE, record.confirmation_id)
    clock.advance(901.0)
    clock.set_now(record.created_at)

    with pytest.raises(ConfirmationRejectedError):
        ledger.claim(MINE, OpsAction.CANCEL_ORDER, ORDER)


# --- one card per subject ---------------------------------------------------


def test_a_second_offer_retires_the_first(ledger: ConfirmationLedger) -> None:
    """Two live cards for one order would be two answers to one question."""
    first = offer(ledger)
    second = offer(ledger)
    assert ledger.get(MINE, first.confirmation_id) is None
    assert ledger.get(MINE, second.confirmation_id) is not None


def test_confirming_the_retired_card_does_not_authorise_the_new_one(
    ledger: ConfirmationLedger,
) -> None:
    first = offer(ledger)
    offer(ledger)
    with pytest.raises(ConfirmationRejectedError):
        ledger.confirm(MINE, first.confirmation_id)
    with pytest.raises(ConfirmationRejectedError) as refused:
        ledger.claim(MINE, OpsAction.CANCEL_ORDER, ORDER)
    assert refused.value.code is ConfirmationCode.NOT_CONFIRMED


# --- what the record carries ------------------------------------------------


def test_the_payload_is_frozen(ledger: ConfirmationLedger) -> None:
    """What the card said cannot be edited after the visitor was shown it."""
    payload = {"order_id": ORDER, "lines": [{"item_id": "burrito"}]}
    record = ledger.offer(MINE, OpsAction.CANCEL_ORDER, ORDER, payload)
    payload["order_id"] = "ord-9000002"

    assert record.payload["order_id"] == ORDER
    with pytest.raises(TypeError):
        record.payload["order_id"] = "ord-9000002"  # type: ignore[index]


def test_the_card_says_it_requires_confirmation(ledger: ConfirmationLedger) -> None:
    card = offer(ledger).as_card()
    assert card["requires_confirmation"] is True
    assert card["confirmed"] is False
    assert card["action"] == OpsAction.CANCEL_ORDER.value


def test_place_order_has_no_route_through_this_ledger(
    ledger: ConfirmationLedger,
) -> None:
    """A second place an order could be confirmed is a second gate to defeat."""
    with pytest.raises(ValueError, match="draft"):
        ledger.offer(MINE, OpsAction.PLACE_ORDER, "draft-abc")


@pytest.mark.parametrize("bad", ["", "   \t"])
def test_a_card_needs_a_visitor(ledger: ConfirmationLedger, bad: str) -> None:
    with pytest.raises(ValueError, match="visitor"):
        ledger.offer(bad.strip(), OpsAction.CANCEL_ORDER, ORDER)


# --- a preference edit is identified by what it says ------------------------


def test_the_same_preferences_digest_the_same_whatever_the_key_order() -> None:
    left = {"display_name": "Sam", "home_store": 41}
    right = {"home_store": 41, "display_name": "Sam"}
    assert preferences_reference(left) == preferences_reference(right)


def test_changing_one_value_changes_the_reference() -> None:
    assert preferences_reference({"home_store": 41}) != preferences_reference(
        {"home_store": 42}
    )


def test_an_explicit_null_is_not_the_same_as_an_absent_key() -> None:
    """Section 7.4: absent leaves a field alone, null clears it."""
    assert preferences_reference({"display_name": None}) != preferences_reference({})


def test_the_reference_is_readable_as_a_preference_card() -> None:
    assert preferences_reference({"home_store": 41}).startswith("prefs-")


# --- housekeeping -----------------------------------------------------------


def test_expired_records_are_swept_rather_than_accumulating(
    ledger: ConfirmationLedger, clock: FakeClock
) -> None:
    for index in range(5):
        ledger.offer(MINE, OpsAction.REDEEM_POINTS, f"reward-{index}")
    assert len(ledger) == 5

    clock.advance(901.0)
    ledger.offer(MINE, OpsAction.REDEEM_POINTS, "reward-late")
    assert len(ledger) == 1


def test_discarding_somebody_elses_card_does_nothing(
    ledger: ConfirmationLedger,
) -> None:
    record = offer(ledger, THEIRS)
    assert ledger.discard(MINE, record.confirmation_id) is False
    assert ledger.discard(THEIRS, record.confirmation_id) is True
