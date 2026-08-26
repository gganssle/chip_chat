"""What the harvest asks the site for, and what a second run asks for.

The politeness properties the framework guarantees are proved in
``test_harvester.py``. What is proved here is that this source does not
squander them: that it fetches four documents rather than four hundred, that
a warm landing zone costs the site nothing at all, and that the offline path
really is offline — it is handed a cache and no transport, so there is
nothing there to fetch with even by accident.
"""

import chipotle_fixtures as site
import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.cache import DocumentCache
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.sources.chipotle import (
    MissingDocumentError,
    harvest_menu,
    load_menu,
    normalise_restaurant_ids,
    parse_menu,
)
from chip_chat.harvest.testing import FakeClock, FakeTransport


def build(transport: FakeTransport, blobs: InMemoryBlobStore) -> Harvester:
    """A harvester on its own gate and clock."""
    clock = FakeClock()
    return Harvester(
        blobs,
        transport,
        clock=clock,
        contact="https://example.test/contact",
        gate=PolitenessGate(RateLimiter(2.0, clock), 1),
    )


def test_one_restaurant_costs_four_documents(blobs: InMemoryBlobStore) -> None:
    transport = site.site()

    documents = harvest_menu(build(transport, blobs), [site.REFERENCE])

    fetched = [url for url in transport.urls if not url.endswith("robots.txt")]
    assert fetched == [
        site.HOME_URL,
        site.INGREDIENTS_URL,
        site.menu_url(site.REFERENCE),
        site.meals_url(site.REFERENCE),
    ]
    assert len(documents.menus) == 1
    assert len(documents.meals) == 1


def test_a_second_restaurant_costs_two_more(blobs: InMemoryBlobStore) -> None:
    """The page and the ingredient corpus are read once, not once per store."""
    transport = site.site()

    harvest_menu(build(transport, blobs), [site.REFERENCE, site.COMPARISON])

    fetched = [url for url in transport.urls if not url.endswith("robots.txt")]
    assert fetched.count(site.HOME_URL) == 1
    assert fetched.count(site.INGREDIENTS_URL) == 1
    assert len(fetched) == 6


def test_a_warm_landing_zone_makes_no_requests(blobs: InMemoryBlobStore) -> None:
    """The property the whole fetch-once design exists for, for this source."""
    harvest_menu(build(site.site(), blobs), [site.REFERENCE])

    warm_transport = site.site()
    warm = build(warm_transport, blobs)
    harvest_menu(warm, [site.REFERENCE])

    assert warm_transport.requests == []
    assert warm.requests_made == 0


def test_the_offline_path_reads_what_the_harvest_wrote(
    blobs: InMemoryBlobStore,
) -> None:
    harvested = harvest_menu(build(site.site(), blobs), [site.REFERENCE])

    loaded = load_menu(DocumentCache(blobs), [site.REFERENCE])

    assert loaded.services == harvested.services
    assert parse_menu(loaded).manifest() == parse_menu(harvested).manifest()


def test_the_offline_path_says_so_when_nothing_was_harvested(
    blobs: InMemoryBlobStore,
) -> None:
    with pytest.raises(MissingDocumentError) as raised:
        load_menu(DocumentCache(blobs), [site.REFERENCE])

    assert site.HOME_URL in str(raised.value)


def test_the_offline_path_names_the_restaurant_it_is_missing(
    blobs: InMemoryBlobStore,
) -> None:
    harvest_menu(build(site.site(), blobs), [site.REFERENCE])

    with pytest.raises(MissingDocumentError) as raised:
        load_menu(DocumentCache(blobs), [site.REFERENCE, site.COMPARISON])

    assert site.COMPARISON in str(raised.value)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ([679], ("679",)),
        (["  0679  "], ("0679",)),
        (["0679", "0679"], ("0679",)),
        (["0679", 1200], ("0679", "1200")),
    ],
)
def test_restaurant_ids_are_taken_as_written(
    given: list[str | int], expected: tuple[str, ...]
) -> None:
    """A zero-padded identifier stays padded: it is part of the cache key."""
    assert normalise_restaurant_ids(given) == expected


@pytest.mark.parametrize("given", [[], ["../secrets"], ["06/79"], [""], ["A1"]])
def test_a_restaurant_id_that_is_not_digits_is_refused(given: list[str]) -> None:
    with pytest.raises(ValueError, match=r"restaurant id|at least one"):
        normalise_restaurant_ids(given)
