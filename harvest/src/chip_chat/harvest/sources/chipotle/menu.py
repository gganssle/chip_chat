"""Fetching Chipotle's published menu, and reading it back without a network.

Two entry points, one shape. :func:`harvest_menu` goes through the framework,
which means it consults ``robots.txt``, waits at the politeness gate, and
lands every raw body in the blob store. :func:`load_menu` reads the same
documents straight out of the cache and cannot touch a network even if it
wanted to — there is no transport in its signature to touch one with.

The second exists so that the parser's reproducibility is a property of the
code rather than a habit. A parse that can only be reached through a fetch is
one hurried afternoon away from becoming a parse that re-fetches.

Both derive their URLs the same way: read the public page, take the API host,
key and ingredient path out of its ``<meta>`` tags, and build the rest. So a
harvest and an offline re-parse are looking at the same endpoints by
construction, not by two constants that agree today.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from chip_chat.harvest.cache import CachedDocument, DocumentCache
from chip_chat.harvest.harvester import JSON_ACCEPT, Harvester
from chip_chat.harvest.sources.chipotle.config import (
    HOME_URL,
    ServicesConfig,
    parse_services_config,
)
from chip_chat.harvest.sources.chipotle.errors import MissingDocumentError

REFERENCE_RESTAURANT_ID = "0679"
"""The restaurant whose prices the demo quotes.

Chipotle prices per store, so a price is only meaningful with a restaurant
attached to it. This is that restaurant: one real, operating store, named here
so that "a Steak Burrito is $13.15" is short for "at restaurant 0679, as
published on the date in ``harvested_at``". The identifier is Chipotle's own
and opaque; issue #21 harvests the store metadata that gives it a street
address. See ``docs/decisions/menu-pricing.md``.
"""

DEFAULT_RESTAURANT_IDS: tuple[str, ...] = (REFERENCE_RESTAURANT_ID,)
"""Restaurants harvested unless the caller names others.

One by default. The dataset models per-store pricing regardless — every price
row carries its restaurant — so harvesting more stores is an argument, not a
schema change, and the default stays as light on a live site as it can be.
"""

_Fetch = Callable[[str, Mapping[str, str] | None], CachedDocument]


@dataclass(frozen=True, slots=True)
class MenuDocuments:
    """The raw documents issue #19 is parsed from.

    Attributes:
        services: The API host, key and paths, as read from ``home``.
        home: The public page carrying the configuration tags.
        ingredients: The ingredient metadata endpoint's response.
        menus: One priced menu per restaurant, in the order requested.
        meals: One priced meal list per restaurant, in the same order.
    """

    services: ServicesConfig
    home: CachedDocument
    ingredients: CachedDocument
    menus: tuple[CachedDocument, ...]
    meals: tuple[CachedDocument, ...]


def normalise_restaurant_ids(
    restaurant_ids: Sequence[str | int],
) -> tuple[str, ...]:
    """Return restaurant identifiers as the strings the API path wants.

    Args:
        restaurant_ids: Identifiers, as strings or integers. Chipotle writes
            them zero-padded in some places and bare in others; whichever form
            is given is the form the URL uses, so a harvest and a later
            offline parse address the same cache entry.

    Returns:
        The identifiers, de-duplicated, in the order given.

    Raises:
        ValueError: If the list is empty or an identifier is not digits.
    """
    seen: dict[str, None] = {}
    for raw in restaurant_ids:
        identifier = str(raw).strip()
        if not identifier.isdigit():
            raise ValueError(f"restaurant id must be digits, got {raw!r}")
        seen.setdefault(identifier, None)
    if not seen:
        raise ValueError("at least one restaurant id is required")
    return tuple(seen)


def harvest_menu(
    harvester: Harvester,
    restaurant_ids: Sequence[str | int] = DEFAULT_RESTAURANT_IDS,
    *,
    home_url: str = HOME_URL,
) -> MenuDocuments:
    """Land the menu documents in the blob store and return them.

    Every request goes through ``harvester``, so a second run over a warm
    landing zone makes no network calls at all.

    Args:
        harvester: The framework instance doing the fetching.
        restaurant_ids: Restaurants to price the catalogue at.
        home_url: The page the API configuration is read from.

    Returns:
        The documents, ready to parse.

    Raises:
        ChipotleSourceError: If the page no longer publishes the API
            configuration.
        RobotsDisallowedError: If ``robots.txt`` forbids one of the URLs.
        FetchError: If a request could not be completed.
    """

    def fetch(url: str, headers: Mapping[str, str] | None) -> CachedDocument:
        return harvester.fetch(url, headers=headers)

    return _collect(fetch, restaurant_ids, home_url)


def load_menu(
    cache: DocumentCache,
    restaurant_ids: Sequence[str | int] = DEFAULT_RESTAURANT_IDS,
    *,
    home_url: str = HOME_URL,
) -> MenuDocuments:
    """Read the menu documents back out of the cache, offline.

    Args:
        cache: The cache a previous harvest wrote to.
        restaurant_ids: The restaurants that harvest priced.
        home_url: The page the API configuration was read from.

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

    return _collect(fetch, restaurant_ids, home_url)


def _collect(
    fetch: _Fetch,
    restaurant_ids: Sequence[str | int],
    home_url: str,
) -> MenuDocuments:
    """Assemble the document set, however ``fetch`` chooses to obtain one."""
    identifiers = normalise_restaurant_ids(restaurant_ids)
    home = fetch(home_url, None)
    services = parse_services_config(home.text, home.source_url, home.harvested_at)
    json_headers = {"Accept": JSON_ACCEPT, **services.headers}
    ingredients = fetch(services.ingredients_url, json_headers)
    menus = tuple(
        fetch(services.online_menu_url(identifier), json_headers)
        for identifier in identifiers
    )
    meals = tuple(
        fetch(services.online_meals_url(identifier), json_headers)
        for identifier in identifiers
    )
    return MenuDocuments(
        services=services,
        home=home,
        ingredients=ingredients,
        menus=menus,
        meals=meals,
    )
