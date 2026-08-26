"""Fetching Chipotle's published nutrition and allergen data, and re-reading it.

The same two entry points, and the same reason for them, as the menu source:
:func:`harvest_nutrition` goes through the framework, and
:func:`load_nutrition` reads the cache and has no transport in its signature
to reach a network with even by accident.

Four documents, three of them new:

* the public page, for the API host and key — read exactly as the menu source
  reads it, because a second copy of that logic is a second thing to go stale;
* the menu metadata *with nutrition*, which is where the per-item figures, the
  dietary tag vocabulary and the nutrient labels all live;
* the allergen and diet endpoint, whose path the page publishes in a
  ``<meta>`` tag and which is the chart a customer sees at ``/allergens``;
* the ``/allergens`` page itself, for the prose around that chart — the part
  that says what the chart does not cover.

And one borrowed: the restaurant's priced menu, already fetched by the menu
source and served from the same cache. It is here for its item list rather
than its prices. Without it this dataset could only make statements about the
items the allergen documents happen to mention, and "we have nothing to say
about this item" would be a row that does not exist rather than a row that
says so — which is precisely the failure PRD K3 rules out.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from chip_chat.harvest.cache import CachedDocument, DocumentCache
from chip_chat.harvest.harvester import JSON_ACCEPT, Harvester
from chip_chat.harvest.sources.chipotle.config import (
    ALLERGENS_URL,
    HOME_URL,
    ServicesConfig,
    parse_services_config,
)
from chip_chat.harvest.sources.chipotle.errors import (
    ChipotleSourceError,
    MissingDocumentError,
)
from chip_chat.harvest.sources.chipotle.menu import (
    DEFAULT_RESTAURANT_IDS,
    normalise_restaurant_ids,
)

_Fetch = Callable[[str, Mapping[str, str] | None], CachedDocument]


@dataclass(frozen=True, slots=True)
class NutritionDocuments:
    """The raw documents issue #20 is parsed from.

    Attributes:
        services: The API host, key and paths, as read from ``home``.
        home: The public page carrying the configuration tags.
        nutrition: The menu metadata with per-item nutrition attached.
        allergen_chart: The allergen and diet endpoint's response.
        allergen_page: The ``/allergens`` page, for its published prose.
        menus: One priced menu per restaurant, for the item list.
    """

    services: ServicesConfig
    home: CachedDocument
    nutrition: CachedDocument
    allergen_chart: CachedDocument
    allergen_page: CachedDocument
    menus: tuple[CachedDocument, ...]


def harvest_nutrition(
    harvester: Harvester,
    restaurant_ids: Sequence[str | int] = DEFAULT_RESTAURANT_IDS,
    *,
    home_url: str = HOME_URL,
    allergens_url: str = ALLERGENS_URL,
) -> NutritionDocuments:
    """Land the nutrition documents in the blob store and return them.

    Every request goes through ``harvester``, so a second run over a warm
    landing zone makes no network calls at all — and a run that follows the
    menu harvest re-uses the menu it already fetched.

    Args:
        harvester: The framework instance doing the fetching.
        restaurant_ids: Restaurants whose item lists define what this dataset
            makes statements about.
        home_url: The page the API configuration is read from.
        allergens_url: The page the published caveats are read from.

    Returns:
        The documents, ready to parse.

    Raises:
        ChipotleSourceError: If the page no longer publishes the API
            configuration or the allergen endpoint's path.
        RobotsDisallowedError: If ``robots.txt`` forbids one of the URLs.
        FetchError: If a request could not be completed.
    """

    def fetch(url: str, headers: Mapping[str, str] | None) -> CachedDocument:
        return harvester.fetch(url, headers=headers)

    return _collect(fetch, restaurant_ids, home_url, allergens_url)


def load_nutrition(
    cache: DocumentCache,
    restaurant_ids: Sequence[str | int] = DEFAULT_RESTAURANT_IDS,
    *,
    home_url: str = HOME_URL,
    allergens_url: str = ALLERGENS_URL,
) -> NutritionDocuments:
    """Read the nutrition documents back out of the cache, offline.

    Args:
        cache: The cache a previous harvest wrote to.
        restaurant_ids: The restaurants that harvest listed.
        home_url: The page the API configuration was read from.
        allergens_url: The page the published caveats were read from.

    Returns:
        The documents, ready to parse.

    Raises:
        MissingDocumentError: If any of them was never harvested.
        ChipotleSourceError: If the cached page carries no API configuration.
    """

    def fetch(url: str, headers: Mapping[str, str] | None) -> CachedDocument:
        document = cache.get(url)
        if document is None:
            raise MissingDocumentError(url)
        return document

    return _collect(fetch, restaurant_ids, home_url, allergens_url)


def _collect(
    fetch: _Fetch,
    restaurant_ids: Sequence[str | int],
    home_url: str,
    allergens_url: str,
) -> NutritionDocuments:
    """Assemble the document set, however ``fetch`` chooses to obtain one."""
    identifiers = normalise_restaurant_ids(restaurant_ids)
    home = fetch(home_url, None)
    services = parse_services_config(home.text, home.source_url, home.harvested_at)
    if services.allergens_url is None:
        raise ChipotleSourceError(
            f"{home.source_url} no longer publishes the allergen endpoint in "
            f"its <meta> tags; there is nowhere left to read allergen data from"
        )
    json_headers = {"Accept": JSON_ACCEPT, **services.headers}
    return NutritionDocuments(
        services=services,
        home=home,
        nutrition=fetch(services.nutrition_url, json_headers),
        allergen_chart=fetch(services.allergens_url, json_headers),
        allergen_page=fetch(allergens_url, None),
        menus=tuple(
            fetch(services.online_menu_url(identifier), json_headers)
            for identifier in identifiers
        ),
    )
