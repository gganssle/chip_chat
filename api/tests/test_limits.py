"""The configuration surface of the four ceilings."""

from collections.abc import Callable

import pytest

from chip_chat.api.limits import SpendLimits


def test_the_defaults_are_a_usable_configuration() -> None:
    limits = SpendLimits()

    assert limits.daily_token_ceiling > 0
    assert limits.turn_token_reservation < limits.session_token_cap
    assert limits.reset_timezone == "UTC"


def test_the_two_session_caps_describe_the_same_conversation() -> None:
    """#108's dead configuration, as an assertion rather than as a comment.

    Two ceilings on one conversation are only meaningful if they agree about
    roughly how long that conversation is. The pair shipped before #108 did not:
    120,000 tokens over 40 turns is 3,000 tokens a turn, *below* the 8,000 a
    single turn reserves before the model is called, so the token cap always
    refused first and the turn cap could not be reached by any conversation at
    all. It was configuration that looked like a control and was not one.

    The quotient is the check, and it is the one to redo whenever either number
    is retuned: the tokens a turn may average under the pair has to leave room
    for a turn that costs what turns actually cost. The upper bound keeps the
    turn cap doing its own job -- if a conversation could reach 22 turns while
    spending well under the token cap, the token cap would be the dead one and
    the mistake would simply have changed hands.
    """
    limits = SpendLimits()
    tokens_per_turn = limits.session_token_cap / limits.session_turn_cap

    assert tokens_per_turn > limits.turn_token_reservation
    # The range measured on the deployed app on 2026-08-31: a mean turn of
    # 27,437 tokens and a largest of 36,938. See docs/decisions/session-token-cap.md.
    assert 27_437 < tokens_per_turn < 2 * 36_938


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("daily_token_ceiling", lambda: SpendLimits(daily_token_ceiling=0)),
        ("session_turn_cap", lambda: SpendLimits(session_turn_cap=0)),
        ("session_token_cap", lambda: SpendLimits(session_token_cap=0)),
        ("source_requests_per_window", lambda: SpendLimits(source_requests_per_window=0)),
        ("source_window_seconds", lambda: SpendLimits(source_window_seconds=0)),
        ("turn_token_reservation", lambda: SpendLimits(turn_token_reservation=-1)),
    ],
)
def test_a_limit_that_would_not_cap_anything_is_refused(
    name: str, build: Callable[[], SpendLimits]
) -> None:
    with pytest.raises(ValueError, match=name):
        build()


def test_an_unknown_reset_timezone_fails_at_construction_not_at_midnight() -> None:
    with pytest.raises(ValueError, match="reset_timezone"):
        SpendLimits(reset_timezone="Mars/Olympus_Mons")


def test_every_ceiling_can_be_set_from_the_environment() -> None:
    limits = SpendLimits.from_env(
        {
            "CHIP_CHAT_DAILY_TOKEN_CEILING": "1234",
            "CHIP_CHAT_SESSION_TURN_CAP": "7",
            "CHIP_CHAT_SESSION_TOKEN_CAP": "999",
            "CHIP_CHAT_SOURCE_REQUESTS_PER_WINDOW": "11",
            "CHIP_CHAT_SOURCE_WINDOW_SECONDS": "30.5",
            "CHIP_CHAT_TURN_TOKEN_RESERVATION": "500",
            "CHIP_CHAT_BUDGET_RESET_TIMEZONE": "America/Los_Angeles",
        }
    )

    assert limits.daily_token_ceiling == 1234
    assert limits.session_turn_cap == 7
    assert limits.session_token_cap == 999
    assert limits.source_requests_per_window == 11
    assert limits.source_window_seconds == 30.5
    assert limits.turn_token_reservation == 500
    assert limits.reset_timezone == "America/Los_Angeles"


def test_an_empty_environment_yields_the_defaults() -> None:
    assert SpendLimits.from_env({}) == SpendLimits()
