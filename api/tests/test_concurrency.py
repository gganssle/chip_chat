"""The pooled case: many sessions at once may not collectively cross the ceiling.

RFC-001 asks for this test by name, and the reason is that a sequential one
proves nothing about it. Read the counter, decide, call the model, add what it
cost -- that shape passes every sequential test ever written and still lets
twenty simultaneous visitors a few tokens below the ceiling all read a number
under the limit and all proceed.

So each test here starts its threads on a barrier, which makes them arrive
inside the check together rather than merely near each other.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

from chip_chat.api.guard import SpendGuard
from chip_chat.api.killswitch import ManualKillSwitch
from chip_chat.api.ledger import BudgetLedger, Reservation
from chip_chat.api.limits import SpendLimits
from chip_chat.api.testing import FakeClock, RecordingModel
from chip_chat.otel import chat_turn
from chip_chat.otel.testing import span_recorder

SESSIONS = 40


def test_concurrent_sessions_cannot_collectively_exceed_the_daily_ceiling(
    clock: FakeClock,
) -> None:
    limits = SpendLimits(daily_token_ceiling=10_000, turn_token_reservation=1_000)
    ledger = BudgetLedger(limits, clock)
    affordable = limits.daily_token_ceiling // limits.turn_token_reservation
    barrier = threading.Barrier(SESSIONS)

    def one_visitor(index: int) -> Reservation:
        barrier.wait()
        return ledger.reserve(f"session-{index}")

    with ThreadPoolExecutor(max_workers=SESSIONS) as pool:
        reservations = list(pool.map(one_visitor, range(SESSIONS)))

    granted = [reservation for reservation in reservations if reservation.granted]
    assert len(granted) == affordable
    assert ledger.global_usage().used <= limits.daily_token_ceiling


def test_the_tokens_the_granted_turns_spend_stay_under_the_ceiling(
    clock: FakeClock,
) -> None:
    """The reservation is pessimistic, so settling must not push the total over."""
    limits = SpendLimits(daily_token_ceiling=10_000, turn_token_reservation=1_000)
    ledger = BudgetLedger(limits, clock)
    barrier = threading.Barrier(SESSIONS)

    def one_visitor(index: int) -> None:
        barrier.wait()
        reservation = ledger.reserve(f"session-{index}")
        if reservation.granted:
            reservation.settle(limits.turn_token_reservation)

    with ThreadPoolExecutor(max_workers=SESSIONS) as pool:
        list(pool.map(one_visitor, range(SESSIONS)))

    assert ledger.global_usage().used == limits.daily_token_ceiling


def test_no_model_is_called_by_the_turns_the_ceiling_refused(
    clock: FakeClock, model: RecordingModel
) -> None:
    """The assertion that matters, made against concurrent traffic.

    Forty visitors arrive together, ten fit under the ceiling, and the model
    records ten calls. Asserting on the count rather than on the replies is the
    point: a guard that had degenerated into after-the-fact reporting would
    serve the same friendly copy and still have bought thirty turns of tokens.
    """
    limits = SpendLimits(
        daily_token_ceiling=10_000,
        session_turn_cap=10,
        session_token_cap=10_000,
        source_requests_per_window=SESSIONS,
        turn_token_reservation=1_000,
    )
    guard = SpendGuard(limits, kill_switch=ManualKillSwitch(), clock=clock)
    affordable = limits.daily_token_ceiling // limits.turn_token_reservation
    barrier = threading.Barrier(SESSIONS)

    def one_visitor(index: int) -> None:
        session_id = f"session-{index}"
        barrier.wait()
        with (
            chat_turn(session_id=session_id, turn_index=0, message="hi") as turn,
            guard.turn(session_id=session_id, source_address="10.0.0.1") as budget,
        ):
            if not budget.allowed:
                turn.record_output(budget.message or "")
                return
            model.complete("hi", session_id)
            budget.record_usage(
                prompt_tokens=model.prompt_tokens,
                completion_tokens=model.completion_tokens,
            )

    with span_recorder("api"), ThreadPoolExecutor(max_workers=SESSIONS) as pool:
        list(pool.map(one_visitor, range(SESSIONS)))

    assert model.call_count == affordable
    assert model.tokens_billed <= limits.daily_token_ceiling
