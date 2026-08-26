"""Fixtures shared by the harvest tests.

The doubles themselves live in :mod:`chip_chat.harvest.testing` so that the
source-specific harvesters in later issues can use the same ones.
"""

from pathlib import Path

import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.testing import FakeClock

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def robots_text() -> str:
    """The fixture ``robots.txt`` every robots test runs against."""
    return (FIXTURES / "robots.txt").read_text()


@pytest.fixture
def crawl_delay_robots_text() -> str:
    """A fixture ``robots.txt`` that asks for a five-second crawl delay."""
    return (FIXTURES / "robots-crawl-delay.txt").read_text()


@pytest.fixture
def clock() -> FakeClock:
    """A clock that advances when slept on."""
    return FakeClock()


@pytest.fixture
def blobs() -> InMemoryBlobStore:
    """An empty blob store, shared across a test's harvesters."""
    return InMemoryBlobStore()
