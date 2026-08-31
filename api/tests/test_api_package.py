"""The package surface: what the service built on top of this may import."""

import pytest

from chip_chat.api import SERVICE_NAME, STOP_STATE_MESSAGE, SpendGuard, SpendLimits
from chip_chat.api.outcome import (
    SESSION_SCOPED_REASONS,
    SESSION_STOP_MESSAGE,
    StopReason,
    stop_message,
)


def test_service_name_comes_from_the_shared_otel_package() -> None:
    assert SERVICE_NAME == "chip-chat-api"


def test_the_spend_cap_is_reachable_from_the_package_root() -> None:
    """The request path should never need to know which module a piece lives in."""
    guard = SpendGuard(SpendLimits())

    assert guard.entry_state() is None


def test_the_stop_state_copy_is_a_single_definition() -> None:
    assert STOP_STATE_MESSAGE == "Cilantro's had a busy day — come back tomorrow"


def test_the_session_stop_copy_is_a_single_definition() -> None:
    """#108's second sentence, pinned the same way the first one is.

    Two definitions now rather than one, and the constraint that used to be
    stated as "there is exactly one" is really the constraint below it: each
    sentence is defined once and derived from the reason, so no refusal site can
    invent its own wording or pick the wrong one of the two.
    """
    assert SESSION_STOP_MESSAGE == (
        "That's a good long conversation — start a new one to keep going"
    )


@pytest.mark.parametrize("reason", sorted(SESSION_SCOPED_REASONS))
def test_a_session_scoped_stop_never_tells_the_visitor_to_come_back_tomorrow(
    reason: StopReason,
) -> None:
    """The falsehood #108 was reported for, asserted as a falsehood.

    A conversation that reached its own ceiling is fixed by starting another
    one, immediately. "Come back tomorrow" is not a softer way of saying that;
    it is a different and untrue claim, and the testers who read it believed it.
    """
    message = stop_message(reason)

    assert message == SESSION_STOP_MESSAGE
    assert "tomorrow" not in message.lower()
    assert message != STOP_STATE_MESSAGE


@pytest.mark.parametrize("reason", sorted(set(StopReason) - set(SESSION_SCOPED_REASONS)))
def test_every_other_stop_keeps_the_copy_the_prd_specified(reason: StopReason) -> None:
    """PRD S4's sentence still covers the day, the switch and the limiters.

    Written as an exhaustive parametrisation over the enum rather than over a
    list of reasons, so that a reason added later has to be classified here
    before this file will pass.
    """
    assert stop_message(reason) == STOP_STATE_MESSAGE


@pytest.mark.parametrize("message", [STOP_STATE_MESSAGE, SESSION_STOP_MESSAGE])
def test_neither_sentence_leaks_the_mechanism_or_apologises(message: str) -> None:
    """The register S4 actually defends, held across both sentences.

    This is the assertion the old "there is exactly one sentence" docstring was
    really making. It survives the split intact.
    """
    lowered = message.lower()

    assert "quota" not in lowered
    assert "error" not in lowered
    assert "sorry" not in lowered
    assert "limit" not in lowered
