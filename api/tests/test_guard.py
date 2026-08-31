"""The guard itself: the check that happens before a model is ever called.

The assertions that matter are the ones about :class:`RecordingModel`. A guard
that had quietly become observability -- counting tokens after the fact, serving
the right copy once the damage was done -- would pass every test written against
the response text and fail every test written against ``model.calls``.
"""

from dataclasses import replace

import pytest

from chip_chat.api.guard import SpendGuard, TurnBudget
from chip_chat.api.killswitch import ManualKillSwitch
from chip_chat.api.ledger import BudgetLedger
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import (
    SESSION_STOP_MESSAGE,
    STOP_STATE_MESSAGE,
    BudgetScope,
    StopReason,
)
from chip_chat.api.testing import FakeClock, RecordingModel
from chip_chat.otel import (
    ChipChatAttributes,
    GuardOutcome,
    SpanSchemaError,
    chat_turn,
)
from chip_chat.otel.testing import SpanRecorder, span_recorder

SOURCE = "203.0.113.7"


@pytest.fixture
def guard(
    limits: SpendLimits, kill_switch: ManualKillSwitch, clock: FakeClock
) -> SpendGuard:
    """A guard with small ceilings, an armed-but-open switch, and a frozen clock."""
    return SpendGuard(limits, kill_switch=kill_switch, clock=clock)


def take_a_turn(
    guard: SpendGuard,
    model: RecordingModel,
    *,
    session_id: str = "session-a",
    turn_index: int = 0,
    source_address: str = SOURCE,
) -> TurnBudget:
    """Drive one turn the way a request handler would, and report what happened."""
    with (
        chat_turn(session_id=session_id, turn_index=turn_index, message="hi") as turn,
        guard.turn(session_id=session_id, source_address=source_address) as budget,
    ):
        if not budget.allowed:
            turn.record_output(budget.message or "")
            return budget
        model.complete("hi", session_id)
        budget.record_usage(
            prompt_tokens=model.prompt_tokens,
            completion_tokens=model.completion_tokens,
        )
        return budget


def exhaust_the_ceiling(guard: SpendGuard, model: RecordingModel) -> None:
    """Spend the day, using a fresh session per turn so no session cap fires first."""
    affordable = guard.limits.daily_token_ceiling // guard.limits.turn_token_reservation
    for index in range(affordable):
        take_a_turn(
            guard,
            model,
            session_id=f"filler-{index}",
            source_address=f"198.51.100.{index}",
        )


def budget_check_attributes(recorder: SpanRecorder) -> dict[str, object]:
    """The attributes of the single ``guard.budget_check`` recorded."""
    return dict(recorder.attributes_of("guard.budget_check"))


def test_an_allowed_turn_reaches_the_model(
    guard: SpendGuard, model: RecordingModel
) -> None:
    with span_recorder("api"):
        budget = take_a_turn(guard, model)

    assert budget.allowed
    assert budget.message is None
    assert model.call_count == 1


def test_the_budget_check_hangs_under_the_turn_it_guards(
    guard: SpendGuard, model: RecordingModel
) -> None:
    """The span is schema, so the shape is worth asserting rather than assuming."""
    with span_recorder("api") as spans:
        take_a_turn(guard, model)

    assert spans.tree_text() == "chat.turn\n  guard.budget_check"


def test_an_allowed_check_records_the_ceiling_it_evaluated(
    guard: SpendGuard, model: RecordingModel
) -> None:
    with span_recorder("api") as spans:
        take_a_turn(guard, model)
    attributes = budget_check_attributes(spans)

    assert attributes[ChipChatAttributes.GUARD_OUTCOME] == GuardOutcome.ALLOWED
    assert attributes[ChipChatAttributes.BUDGET_SCOPE] == BudgetScope.GLOBAL
    assert attributes[ChipChatAttributes.BUDGET_TOKENS_LIMIT] == (
        guard.limits.daily_token_ceiling
    )
    assert ChipChatAttributes.GUARD_REASON not in attributes


def test_no_model_is_called_once_the_ceiling_is_tripped(
    guard: SpendGuard, model: RecordingModel
) -> None:
    """The acceptance criterion, asserted on the mock rather than on the copy."""
    with span_recorder("api"):
        exhaust_the_ceiling(guard, model)
        calls_before = model.call_count

        budget = take_a_turn(guard, model, session_id="one-too-many")

    assert not budget.allowed
    assert model.call_count == calls_before, "the refused turn bought tokens"


def test_a_tripped_ceiling_blocks_the_check_with_a_groupable_reason(
    guard: SpendGuard, model: RecordingModel
) -> None:
    with span_recorder("api"):
        exhaust_the_ceiling(guard, model)

    with span_recorder("api") as spans:
        take_a_turn(guard, model, session_id="one-too-many")
    attributes = budget_check_attributes(spans)

    assert attributes[ChipChatAttributes.GUARD_OUTCOME] == GuardOutcome.BLOCKED
    assert attributes[ChipChatAttributes.GUARD_REASON] == StopReason.DAILY_CEILING
    assert attributes[ChipChatAttributes.BUDGET_SCOPE] == BudgetScope.GLOBAL


def test_the_stop_state_is_the_copy_the_prd_specifies(
    guard: SpendGuard, model: RecordingModel
) -> None:
    """The day is spent, so the sentence about the day is the right one."""
    with span_recorder("api"):
        exhaust_the_ceiling(guard, model)
        budget = take_a_turn(guard, model, session_id="one-too-many")

    assert budget.message == "Cilantro's had a busy day — come back tomorrow"
    assert budget.message == STOP_STATE_MESSAGE


def test_a_refused_turn_costs_the_ceiling_nothing(
    guard: SpendGuard, model: RecordingModel, kill_switch: ManualKillSwitch
) -> None:
    with span_recorder("api"):
        take_a_turn(guard, model)
        spent = guard.ledger.global_usage().used
        kill_switch.throw()
        take_a_turn(guard, model, session_id="refused")

    assert guard.ledger.global_usage().used == spent


def test_the_kill_switch_stops_the_app_without_a_restart(
    guard: SpendGuard, model: RecordingModel, kill_switch: ManualKillSwitch
) -> None:
    """One guard object, three turns, and a switch flipped between them."""
    with span_recorder("api"):
        assert take_a_turn(guard, model).allowed

        kill_switch.throw()
        stopped = take_a_turn(guard, model, turn_index=1)

        kill_switch.reset()
        resumed = take_a_turn(guard, model, turn_index=2)

    assert not stopped.allowed
    assert stopped.stop is not None
    assert stopped.stop.reason is StopReason.KILL_SWITCH
    assert stopped.message == STOP_STATE_MESSAGE
    assert resumed.allowed
    assert model.call_count == 2, "only the two allowed turns reached the model"


def test_the_kill_switch_beats_a_perfectly_healthy_budget(
    guard: SpendGuard, model: RecordingModel, kill_switch: ManualKillSwitch
) -> None:
    kill_switch.throw()

    with span_recorder("api") as spans:
        take_a_turn(guard, model)
    attributes = budget_check_attributes(spans)

    assert model.calls == []
    assert attributes[ChipChatAttributes.GUARD_REASON] == StopReason.KILL_SWITCH


def test_a_conversation_is_stopped_mid_flight_not_only_on_entry(
    guard: SpendGuard, model: RecordingModel, kill_switch: ManualKillSwitch
) -> None:
    """PRD S4 asks for the friendly state in both places."""
    with span_recorder("api"):
        for index in range(3):
            assert take_a_turn(guard, model, turn_index=index).allowed

        kill_switch.throw()
        interrupted = take_a_turn(guard, model, turn_index=3)

    assert not interrupted.allowed
    assert interrupted.message == STOP_STATE_MESSAGE
    assert model.call_count == 3


def test_a_rate_limited_address_never_reaches_the_model(
    limits: SpendLimits, kill_switch: ManualKillSwitch, clock: FakeClock
) -> None:
    """A loop from one host, with a fresh session each time to dodge the session cap."""
    guard = SpendGuard(
        replace(limits, session_turn_cap=1000), kill_switch=kill_switch, clock=clock
    )
    model = RecordingModel()

    with span_recorder("api"):
        for index in range(20):
            take_a_turn(guard, model, session_id=f"minted-{index}")

    assert model.call_count == limits.source_requests_per_window


def test_a_rate_limited_address_is_reported_as_such(
    guard: SpendGuard, model: RecordingModel
) -> None:
    with span_recorder("api"):
        for index in range(guard.limits.source_requests_per_window):
            take_a_turn(guard, model, session_id=f"minted-{index}")

    with span_recorder("api") as spans:
        budget = take_a_turn(guard, model, session_id="one-more")
    attributes = budget_check_attributes(spans)

    assert budget.stop is not None
    assert budget.stop.reason is StopReason.SOURCE_RATE_LIMIT
    assert attributes[ChipChatAttributes.BUDGET_SCOPE] == BudgetScope.SOURCE_ADDRESS


def test_a_session_that_will_not_stop_talking_is_capped(
    guard: SpendGuard, model: RecordingModel
) -> None:
    with span_recorder("api"):
        for index in range(guard.limits.session_turn_cap):
            take_a_turn(
                guard, model, source_address=f"198.51.100.{index}", turn_index=index
            )
        budget = take_a_turn(guard, model, source_address="198.51.100.99")

    assert budget.stop is not None
    assert budget.stop.reason is StopReason.SESSION_TURN_CAP


def test_a_capped_session_is_not_told_the_day_is_over(
    guard: SpendGuard, model: RecordingModel
) -> None:
    """#108, through the guard rather than through the ledger.

    The visitor who reported this had spent one conversation, not the app's day,
    and was told to come back tomorrow. The test asserts both halves of why that
    was wrong: the sentence does not say tomorrow, and the day it claimed was
    gone is demonstrably still there -- the very next turn, on a fresh session,
    reaches the model.
    """
    with span_recorder("api"):
        for index in range(guard.limits.session_turn_cap):
            take_a_turn(
                guard, model, source_address=f"198.51.100.{index}", turn_index=index
            )
        capped = take_a_turn(guard, model, source_address="198.51.100.99")
        calls_before = model.call_count
        fresh = take_a_turn(guard, model, session_id="a-new-conversation")

    assert capped.message == SESSION_STOP_MESSAGE
    assert "tomorrow" not in SESSION_STOP_MESSAGE
    assert fresh.allowed
    assert model.call_count == calls_before + 1


def test_a_turn_that_raised_gives_its_reservation_back(guard: SpendGuard) -> None:
    """A crash must not hold tokens against the ceiling until midnight."""

    def a_turn_that_blows_up() -> None:
        with guard.turn(session_id="boom", source_address=SOURCE) as budget:
            assert budget.allowed
            raise RuntimeError("tool blew up")

    with (
        span_recorder("api"),
        chat_turn(session_id="boom", turn_index=0),
        pytest.raises(RuntimeError, match="tool blew up"),
    ):
        a_turn_that_blows_up()

    assert guard.ledger.global_usage().used == 0


def test_settling_twice_cannot_double_charge(guard: SpendGuard) -> None:
    """A caller that settles by hand and a context manager that also settles."""
    with (
        span_recorder("api"),
        chat_turn(session_id="session-a", turn_index=0),
        guard.turn(session_id="session-a", source_address=SOURCE) as budget,
    ):
        budget.record_usage(prompt_tokens=100, completion_tokens=50)
        budget.settle()

    assert guard.ledger.global_usage().used == 150


def test_entry_is_open_while_there_is_budget(guard: SpendGuard) -> None:
    assert guard.entry_state() is None


def test_entry_is_closed_by_the_kill_switch(
    guard: SpendGuard, kill_switch: ManualKillSwitch
) -> None:
    kill_switch.throw()

    stop = guard.entry_state()

    assert stop is not None
    assert stop.reason is StopReason.KILL_SWITCH
    assert stop.message == STOP_STATE_MESSAGE


def test_entry_is_closed_once_the_day_is_spent(
    guard: SpendGuard, model: RecordingModel
) -> None:
    with span_recorder("api"):
        exhaust_the_ceiling(guard, model)

    stop = guard.entry_state()

    assert stop is not None
    assert stop.reason is StopReason.DAILY_CEILING


def test_entry_state_emits_no_span(guard: SpendGuard) -> None:
    """There is no turn to hang one under, and inventing one would be a lie."""
    with span_recorder("api") as spans:
        guard.entry_state()

    assert spans.names() == ()


def test_a_guard_adopts_the_ceilings_of_the_ledger_it_was_handed(
    clock: FakeClock,
) -> None:
    """Otherwise `guard.limits` reports numbers nothing is enforcing."""
    limits = SpendLimits(daily_token_ceiling=4_321, turn_token_reservation=1_000)
    guard = SpendGuard(ledger=BudgetLedger(limits, clock))

    assert guard.limits.daily_token_ceiling == 4_321


def test_the_check_refuses_to_run_outside_the_turn_it_belongs_to(
    guard: SpendGuard,
) -> None:
    """`guard.budget_check` is a child of `chat.turn`, and that is enforced."""
    with span_recorder("api"), pytest.raises(SpanSchemaError, match=r"chat\.turn"):
        guard.reserve(session_id="session-a", source_address=SOURCE)
