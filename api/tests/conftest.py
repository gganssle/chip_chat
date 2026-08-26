"""Fixtures shared by the spend-cap tests.

The doubles live in :mod:`chip_chat.api.testing` rather than here so that the
service built on the guard can make the same assertions about its own request
path -- particularly the one that matters, that no model was called.

The catalogue below is the other half: the draft store prices against real
catalogue rows, so its tests need a real catalogue rather than a menu invented
to suit them.
"""

from functools import cache
from pathlib import Path

import pytest

from chip_chat.api.drafts import DraftStore
from chip_chat.api.killswitch import ManualKillSwitch
from chip_chat.api.limits import SpendLimits
from chip_chat.api.testing import FakeClock, RecordingModel
from chip_chat.catalog import MenuCatalog, load_catalog
from chip_chat.harvest.blobs import LocalBlobStore

_CATALOG_FIXTURES = Path(__file__).resolve().parents[2] / "catalog" / "tests" / "fixtures"
_CATALOG_PREFIX = "catalog"


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


@cache
def _fixture_catalog() -> MenuCatalog:
    """The catalogue committed by issue #24, read once for the whole run.

    The same fixture ``data-gen``'s tests resolve against, and read through the
    shipped loader rather than rebuilt from the harvest recordings: a draft store
    that disagrees with the committed catalogue is a thing worth finding out
    here rather than in a deployment.
    """
    return load_catalog(LocalBlobStore(_CATALOG_FIXTURES), _CATALOG_PREFIX)


@pytest.fixture
def catalog() -> MenuCatalog:
    """The fixture catalogue: two entrees, five modifiers each, thirty stores."""
    return _fixture_catalog()


@pytest.fixture
def drafts(catalog: MenuCatalog, clock: FakeClock) -> DraftStore:
    """A draft store on a clock the test drives, so a TTL needs no waiting."""
    return DraftStore(catalog, clock=clock)
