"""Reading the API host and key out of the page, and refusing to guess.

The source knows nothing about Chipotle's API that Chipotle's own page does
not tell it. These tests hold that line: if the page stops publishing the
configuration, the harvest stops too, rather than falling back on a value
remembered from the last time it worked.
"""

import chipotle_fixtures as site
import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.sources.chipotle import (
    ChipotleSourceError,
    harvest_menu,
    parse_services_config,
)
from chip_chat.harvest.sources.chipotle.config import SUBSCRIPTION_KEY_HEADER
from chip_chat.harvest.testing import EPOCH, FakeClock, FakeTransport


def build(transport: FakeTransport, blobs: InMemoryBlobStore) -> Harvester:
    """A harvester on its own gate and clock, so no test disturbs another."""
    clock = FakeClock()
    return Harvester(
        blobs,
        transport,
        clock=clock,
        contact="https://example.test/contact",
        gate=PolitenessGate(RateLimiter(2.0, clock), 1),
    )


def test_the_host_key_and_paths_come_from_the_page() -> None:
    config = parse_services_config(site.read("home.html").decode(), site.HOME_URL, EPOCH)

    assert config.base_url == site.SERVICES
    assert config.app_key == site.APP_KEY
    assert config.ingredients_url == site.INGREDIENTS_URL
    assert config.allergens_path == "/menu-metadata/v1/menu-metadata/allergendiets"
    assert config.source_url == site.HOME_URL
    assert config.harvested_at == EPOCH


def test_the_restaurant_urls_are_built_from_the_discovered_host() -> None:
    config = parse_services_config(site.read("home.html").decode(), site.HOME_URL, EPOCH)

    assert config.online_menu_url("0679") == site.menu_url("0679")
    assert config.online_meals_url("0679") == site.meals_url("0679")


def test_a_page_without_the_tags_stops_the_harvest() -> None:
    """A redesign should be a loud failure, not a silent fallback."""
    with pytest.raises(ChipotleSourceError) as raised:
        parse_services_config(
            site.read("home-without-config.html").decode(), site.HOME_URL, EPOCH
        )

    assert "services host" in str(raised.value)
    assert "app key" in str(raised.value)


def test_the_key_is_sent_to_the_api_and_never_to_the_page(
    blobs: InMemoryBlobStore,
) -> None:
    transport = site.site()

    harvest_menu(build(transport, blobs), [site.REFERENCE])

    by_url = {request.url: request.headers for request in transport.requests}
    assert SUBSCRIPTION_KEY_HEADER not in by_url[site.HOME_URL]
    assert by_url[site.INGREDIENTS_URL][SUBSCRIPTION_KEY_HEADER] == site.APP_KEY
    assert by_url[site.menu_url(site.REFERENCE)][SUBSCRIPTION_KEY_HEADER] == site.APP_KEY
