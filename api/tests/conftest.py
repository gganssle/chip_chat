"""Fixtures shared by the spend-cap tests.

The doubles live in :mod:`chip_chat.api.testing` rather than here so that the
service built on the guard can make the same assertions about its own request
path -- particularly the one that matters, that no model was called.
"""

import pytest

from chip_chat.api.killswitch import ManualKillSwitch
from chip_chat.api.limits import SpendLimits
from chip_chat.api.testing import FakeClock, RecordingModel


@pytest.fixture
def clock() -> FakeClock:
    """A clock frozen at a fixed instant, advanced explicitly by each test."""
    return FakeClock()


@pytest.fixture
def limits() -> SpendLimits:
    """Small ceilings, so a test can trip them for real rather than reason about them."""
    return SpendLimits(
        daily_token_ceiling=10_000,
        session_turn_cap=5,
        session_token_cap=6_000,
        source_requests_per_window=3,
        source_window_seconds=60.0,
        turn_token_reservation=1_000,
    )


@pytest.fixture
def kill_switch() -> ManualKillSwitch:
    """A circuit breaker in the run position."""
    return ManualKillSwitch()


@pytest.fixture
def model() -> RecordingModel:
    """The mock that would record a model call, and must not."""
    return RecordingModel(prompt_tokens=800, completion_tokens=200)
