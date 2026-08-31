"""The daily counter: reserving, settling, and the day boundary.

Nothing here waits for time to pass. The ledger reads a clock the test drives,
which is the only way the midnight cases below can be asserted at all.
"""

from dataclasses import replace
from datetime import UTC, datetime

from chip_chat.api.ledger import BudgetLedger
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import (
    SESSION_STOP_MESSAGE,
    STOP_STATE_MESSAGE,
    BudgetScope,
    StopReason,
)
from chip_chat.api.testing import FakeClock


def test_a_turn_reserves_before_it_spends(limits: SpendLimits, clock: FakeClock) -> None:
    """The reservation is what makes the check safe, so it must be visible."""
    ledger = BudgetLedger(limits, clock)

    reservation = ledger.reserve("session-a")

    assert reservation.granted
    assert ledger.global_usage().used == limits.turn_token_reservation


def test_settling_replaces_the_reservation_with_the_real_cost(
    limits: SpendLimits, clock: FakeClock
) -> None:
    ledger = BudgetLedger(limits, clock)

    reservation = ledger.reserve("session-a")
    reservation.settle(120)

    assert ledger.global_usage().used == 120
    assert ledger.session_tokens("session-a") == 120


def test_a_turn_that_called_no_model_gives_its_reservation_back(
    limits: SpendLimits, clock: FakeClock
) -> None:
    ledger = BudgetLedger(limits, clock)

    ledger.reserve("session-a").release()

    assert ledger.global_usage().used == 0


def test_settling_twice_does_not_charge_twice(
    limits: SpendLimits, clock: FakeClock
) -> None:
    """The context manager and an explicit settle must be able to coexist."""
    ledger = BudgetLedger(limits, clock)

    reservation = ledger.reserve("session-a")
    reservation.settle(500)
    reservation.settle(500)

    assert ledger.global_usage().used == 500


def test_the_daily_ceiling_refuses_the_turn_that_would_cross_it(
    limits: SpendLimits, clock: FakeClock
) -> None:
    ledger = BudgetLedger(limits, clock)
    affordable = limits.daily_token_ceiling // limits.turn_token_reservation
    for index in range(affordable):
        ledger.reserve(f"session-{index}")

    refused = ledger.reserve("one-too-many")

    assert not refused.granted
    assert refused.stop is not None
    assert refused.stop.reason is StopReason.DAILY_CEILING
    assert refused.stop.usage.scope is BudgetScope.GLOBAL
    assert refused.stop.usage.limit == limits.daily_token_ceiling


def test_a_ceiling_refusal_reports_the_stop_state_copy(
    limits: SpendLimits, clock: FakeClock
) -> None:
    ledger = BudgetLedger(replace(limits, daily_token_ceiling=1), clock)

    refused = ledger.reserve("session-a")

    assert refused.stop is not None
    assert refused.stop.message == "Cilantro's had a busy day — come back tomorrow"
    assert refused.stop.message == STOP_STATE_MESSAGE


def test_a_session_refusal_does_not_tell_the_visitor_to_come_back_tomorrow(
    limits: SpendLimits, clock: FakeClock
) -> None:
    """#108: the day is fine, this conversation is not, and the two differ.

    The daily ceiling here is untouched -- only the session's turn cap is spent
    -- so a visitor who is told to come back tomorrow has been told something
    the ledger itself contradicts: their next reserve on a fresh session
    succeeds in the very next statement.
    """
    ledger = BudgetLedger(limits, clock)
    for _ in range(limits.session_turn_cap):
        ledger.reserve("chatty").settle(1)

    refused = ledger.reserve("chatty")

    assert refused.stop is not None
    assert (
        refused.stop.message
        == "That's a good long conversation — start a new one to keep going"
    )
    assert refused.stop.message == SESSION_STOP_MESSAGE
    assert "tomorrow" not in refused.stop.message
    assert ledger.reserve("someone-else").granted


def test_one_session_cannot_take_the_day_in_turns(
    limits: SpendLimits, clock: FakeClock
) -> None:
    ledger = BudgetLedger(limits, clock)
    for _ in range(limits.session_turn_cap):
        ledger.reserve("chatty").settle(1)

    refused = ledger.reserve("chatty")

    assert refused.stop is not None
    assert refused.stop.reason is StopReason.SESSION_TURN_CAP
    assert refused.stop.usage.scope is BudgetScope.SESSION


def test_one_session_cannot_take_the_day_in_tokens(clock: FakeClock) -> None:
    limits = SpendLimits(
        daily_token_ceiling=100_000,
        session_turn_cap=100,
        session_token_cap=2_500,
        source_requests_per_window=100,
        turn_token_reservation=1_000,
    )
    ledger = BudgetLedger(limits, clock)
    for _ in range(2):
        ledger.reserve("greedy").settle(1_000)

    refused = ledger.reserve("greedy")

    assert refused.stop is not None
    assert refused.stop.reason is StopReason.SESSION_TOKEN_CAP
    # Both session-scoped reasons carry the same remedy, because the remedy is
    # the same and naming which counter ran out would say which one to re-roll.
    assert refused.stop.message == SESSION_STOP_MESSAGE


def test_a_session_cap_does_not_bind_a_different_session(
    limits: SpendLimits, clock: FakeClock
) -> None:
    ledger = BudgetLedger(limits, clock)
    for _ in range(limits.session_turn_cap):
        ledger.reserve("chatty").settle(1)

    assert ledger.reserve("someone-else").granted


def test_the_counter_resets_at_the_configured_day_boundary(
    limits: SpendLimits, clock: FakeClock
) -> None:
    ledger = BudgetLedger(limits, clock)
    ledger.reserve("session-a").settle(4_000)
    assert ledger.global_usage().used == 4_000

    clock.set_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC))

    assert ledger.global_usage().used == 0
    assert ledger.session_turns("session-a") == 0


def test_the_day_boundary_is_the_configured_zone_and_not_utc(clock: FakeClock) -> None:
    """The timezone question, which is the easy one to get subtly wrong.

    The clock starts at noon UTC on 1 January, which is four in the morning in
    Los Angeles. UTC midnight arrives while it is still afternoon there, and a
    ceiling that reset then would hand a fresh day of spend to whoever was
    watching the clock in the wrong hemisphere.
    """
    limits = SpendLimits(
        daily_token_ceiling=10_000,
        turn_token_reservation=1_000,
        reset_timezone="America/Los_Angeles",
    )
    ledger = BudgetLedger(limits, clock)
    ledger.reserve("session-a").settle(4_000)

    clock.set_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC))
    assert ledger.global_usage().used == 4_000, "UTC midnight is not Los Angeles midnight"

    clock.set_now(datetime(2026, 1, 2, 8, 1, tzinfo=UTC))
    assert ledger.global_usage().used == 0


def test_a_turn_that_spanned_midnight_is_charged_to_the_new_day(
    limits: SpendLimits, clock: FakeClock
) -> None:
    """Never under-count. Erring the other way is how a ceiling leaks."""
    ledger = BudgetLedger(limits, clock)
    reservation = ledger.reserve("night-owl")

    clock.set_now(datetime(2026, 1, 2, 0, 0, tzinfo=UTC))
    reservation.settle(900)

    assert ledger.global_usage().used == 900


def test_exhausted_reports_whether_another_turn_is_affordable(
    limits: SpendLimits, clock: FakeClock
) -> None:
    ledger = BudgetLedger(limits, clock)
    affordable = limits.daily_token_ceiling // limits.turn_token_reservation

    for index in range(affordable - 1):
        ledger.reserve(f"session-{index}")
    assert not ledger.exhausted()

    ledger.reserve("last-one")
    assert ledger.exhausted()
