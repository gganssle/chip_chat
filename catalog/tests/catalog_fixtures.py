"""Building catalogues out of the harvest tests' fixture site.

The catalogue is a consolidation of three harvests, so a test of it needs
three harvested datasets. Rather than record a second copy of Chipotle's
responses, these tests build them from the fixture site the harvest tests
already maintain — ``harvest/tests/chipotle_fixtures.py``, which is a trimmed
recording of the real endpoints with its five alterations named in its own
docstring. A second copy would be a second thing to keep true, and the one
that rotted would be this one.

That directory is on ``sys.path`` when the whole suite runs, because pytest
puts each test file's directory there. It is inserted explicitly here so that
``pytest catalog/tests`` on its own works too.
"""

import sys
from datetime import datetime
from functools import cache
from pathlib import Path

HARVEST_TESTS = Path(__file__).resolve().parents[2] / "harvest" / "tests"
if str(HARVEST_TESTS) not in sys.path:
    sys.path.insert(0, str(HARVEST_TESTS))

import chipotle_fixtures as chipotle  # noqa: E402

from chip_chat.catalog import MenuCatalog, build_catalog  # noqa: E402
from chip_chat.harvest.blobs import BlobStore, InMemoryBlobStore  # noqa: E402
from chip_chat.harvest.harvester import Harvester  # noqa: E402
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter  # noqa: E402
from chip_chat.harvest.sources.chipotle import (  # noqa: E402
    MenuDataset,
    NutritionDataset,
    PolicyDataset,
    harvest_menu,
    harvest_nutrition,
    harvest_policy,
    parse_menu,
    parse_nutrition,
    parse_policy,
)
from chip_chat.harvest.testing import FakeClock, FakeTransport  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def harvester(
    transport: FakeTransport,
    start: datetime | None = None,
    blobs: BlobStore | None = None,
) -> Harvester:
    """A harvester on its own gate and clock, which never really sleeps.

    Args:
        transport: The site to fetch from.
        start: When this harvest happens. A second harvest at a later instant
            is how the stability tests tell "the same menu, read again" from
            "the same bytes, hashed again".
        blobs: Where the raw bytes land. Defaults to a store that is thrown
            away; the command-line test passes a real directory so that the
            offline build has a cache to read.
    """
    clock = FakeClock() if start is None else FakeClock(start=start)
    return Harvester(
        blobs if blobs is not None else InMemoryBlobStore(),
        transport,
        clock=clock,
        contact="https://example.test/contact",
        gate=PolitenessGate(RateLimiter(2.0, clock), 1),
    )


def datasets(
    transport: FakeTransport | None = None,
    restaurants: list[str] | None = None,
    stores: int = 30,
    start: datetime | None = None,
) -> tuple[MenuDataset, NutritionDataset, PolicyDataset]:
    """Harvest the fixture site and parse all three datasets from it.

    Args:
        transport: The site to harvest, or ``None`` for the fixture site.
        restaurants: Which restaurants to price at, first one the reference.
        stores: How many stores the policy harvest reads.
        start: When the harvest happens.

    Returns:
        The menu, nutrition and policy datasets.
    """
    served = transport if transport is not None else chipotle.site()
    which = restaurants or [chipotle.REFERENCE]
    return (
        parse_menu(harvest_menu(harvester(served, start), which)),
        parse_nutrition(harvest_nutrition(harvester(served, start), which)),
        parse_policy(harvest_policy(harvester(served, start), store_count=stores)),
    )


def catalog(
    transport: FakeTransport | None = None,
    restaurants: list[str] | None = None,
    stores: int = 30,
    start: datetime | None = None,
) -> MenuCatalog:
    """Build the catalogue from the fixture site."""
    return build_catalog(*datasets(transport, restaurants, stores, start))


@cache
def fixture_catalog() -> MenuCatalog:
    """The catalogue built from the fixture site, built once for the whole run.

    Cached because building it harvests thirty store pages through the parser,
    and every test that wants it wants the same one. Every record in it is
    frozen, so there is nothing for one test to do to another's copy.
    """
    return catalog()
