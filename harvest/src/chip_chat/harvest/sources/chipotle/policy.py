"""Fetching the policy half of the corpus, and reading it back without a network.

The same two entry points, and the same reason for them, as the menu and
nutrition sources: :func:`harvest_policy` goes through the framework, and
:func:`load_policy` reads the cache and has no transport in its signature to
reach a network with even by accident.

Issue #21 wants four different kinds of thing, and they turn out to live in
four different places:

* **the rewards terms**, on ``/rewards-terms`` — one authored block of about
  twenty sections, and the only document that says in binding language how
  points are earned, when they expire, and what a Reward is;
* **the rewards themselves**, on ``/rewards`` — the Rewards Exchange line-up
  and its point costs, published in the page's own markup. The signed-in
  exchange is not public and is not fetched;
* **the FAQ**, from the persisted GraphQL query the ``/contact-us`` page calls.
  136 questions filed under ten categories: how points work, whether an order
  can be cancelled, who to ask about a refund, and what catering is. The page
  itself carries none of this — it is drawn in the browser — so the endpoint
  behind it is what gets harvested;
* **catering**, from the catering site's own menu API, which publishes six
  packages with national prices, minimum party sizes and the component lists
  each one is built from;
* **stores**, from the published locator. See :mod:`locator` for how fifty of
  four thousand are chosen, and why one of them is chosen by name.

**The catering API has its own subscription key.** It is not the one the www
page publishes in its ``<meta>`` tags — that key answers 401 here — so it is
read the same way, from the place the catering site hands it to its own front
end: the ``VUE_APP_SUBSCRIPTION_KEY`` in its script bundle, whose hashed
filename is itself read out of the catering page rather than remembered. Two
documents to reach one key, and no copied credential in this repository.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urljoin

from chip_chat.harvest.cache import CachedDocument, DocumentCache
from chip_chat.harvest.harvester import JSON_ACCEPT, Harvester
from chip_chat.harvest.sources.chipotle.config import (
    HOME_URL,
    SUBSCRIPTION_KEY_HEADER,
    ServicesConfig,
    parse_services_config,
)
from chip_chat.harvest.sources.chipotle.errors import (
    ChipotleSourceError,
    MissingDocumentError,
)
from chip_chat.harvest.sources.chipotle.locator import (
    DEFAULT_STORE_COUNT,
    SITEMAP_URL,
    parse_store_page,
    select_store_urls,
    sitemap_links,
    store_page_urls,
)

REWARDS_URL = "https://www.chipotle.com/rewards"
"""The rewards landing page: how points work, and what they buy."""

REWARDS_TERMS_URL = "https://www.chipotle.com/rewards-terms"
"""The rewards programme's terms and conditions."""

FAQ_URL = "https://www.chipotle.com/graphql/execute.json/chipotle/FAQ-Query;region=en-us"
"""The published FAQ, as JSON.

A persisted GraphQL query — a plain ``GET`` with no key and no body, which is
how the site's own ``/contact-us`` page loads the questions it displays. Named
here rather than discovered because the page reaches it from a compiled
bundle, with no tag to read; if it stops answering, this source stops rather
than falling back on a stale copy.
"""

CATERING_URL = "https://catering.chipotle.com/"
"""The catering site. Read for the address of its script bundle, nothing else."""

CATERING_MENU_PATH = "/cateringorder/v1/menu/tiered"
"""The catering menu endpoint the catering site calls on its own home page."""

_BUNDLE = re.compile(r'src="(?P<path>[^"]*/js/app[^"]*\.js)"')
_CATERING_KEY = re.compile(r'VUE_APP_SUBSCRIPTION_KEY\s*:\s*"(?P<key>[^"]+)"')

_Fetch = Callable[[str, Mapping[str, str] | None], CachedDocument]


@dataclass(frozen=True, slots=True)
class PolicyDocuments:
    """The raw documents issue #21 is parsed from.

    Attributes:
        services: The API host, key and paths, as read from ``home``.
        home: The public page carrying the configuration tags.
        rewards: The rewards landing page.
        rewards_terms: The rewards terms and conditions.
        faq: The published FAQ.
        catering_home: The catering site's page, read for its bundle address.
        catering_bundle: That bundle, read for the catering subscription key.
        catering_menu: The catering menu endpoint's response.
        sitemaps: The locator's sitemap index and every sitemap it names.
        stores: One locator page per chosen store.
        store_profiles: One restaurant API response per chosen store, in the
            same order, for the name the locator does not publish.
    """

    services: ServicesConfig
    home: CachedDocument
    rewards: CachedDocument
    rewards_terms: CachedDocument
    faq: CachedDocument
    catering_home: CachedDocument
    catering_bundle: CachedDocument
    catering_menu: CachedDocument
    sitemaps: tuple[CachedDocument, ...]
    stores: tuple[CachedDocument, ...]
    store_profiles: tuple[CachedDocument, ...]


def harvest_policy(
    harvester: Harvester,
    *,
    home_url: str = HOME_URL,
    store_count: int = DEFAULT_STORE_COUNT,
) -> PolicyDocuments:
    """Land the policy documents in the blob store and return them.

    Every request goes through ``harvester``, so a second run over a warm
    landing zone makes no network calls at all — and a run that follows the
    menu harvest re-uses the home page it already fetched.

    Args:
        harvester: The framework instance doing the fetching.
        home_url: The page the API configuration is read from.
        store_count: How many stores to read from the locator.

    Returns:
        The documents, ready to parse.

    Raises:
        ChipotleSourceError: If a page no longer publishes what this source
            reads from it.
        RobotsDisallowedError: If ``robots.txt`` forbids one of the URLs.
        FetchError: If a request could not be completed.
    """

    def fetch(url: str, headers: Mapping[str, str] | None) -> CachedDocument:
        return harvester.fetch(url, headers=headers)

    return _collect(fetch, home_url, store_count)


def load_policy(
    cache: DocumentCache,
    *,
    home_url: str = HOME_URL,
    store_count: int = DEFAULT_STORE_COUNT,
) -> PolicyDocuments:
    """Read the policy documents back out of the cache, offline.

    The store selection is derived from the cached sitemaps by the same
    function the harvest used, so an offline re-parse addresses exactly the
    pages the harvest landed rather than a list it had to remember.

    Args:
        cache: The cache a previous harvest wrote to.
        home_url: The page the API configuration was read from.
        store_count: How many stores that harvest read.

    Returns:
        The documents, ready to parse.

    Raises:
        MissingDocumentError: If any of them was never harvested.
        ChipotleSourceError: If a cached document no longer parses.
    """

    def fetch(url: str, headers: Mapping[str, str] | None) -> CachedDocument:
        document = cache.get(url)
        if document is None:
            raise MissingDocumentError(url)
        return document

    return _collect(fetch, home_url, store_count)


def bundle_url(html: str, source_url: str) -> str:
    """Return the address of the catering site's script bundle.

    Args:
        html: The catering page's source.
        source_url: Where it came from, and what a relative path resolves
            against.

    Returns:
        The absolute URL of the bundle.

    Raises:
        ChipotleSourceError: If the page loads no such bundle.
    """
    match = _BUNDLE.search(html)
    if match is None:
        raise ChipotleSourceError(
            f"{source_url} loads no application bundle; there is nowhere left "
            f"to read the catering API's subscription key from"
        )
    return urljoin(source_url, match.group("path"))


def catering_key(javascript: str, source_url: str) -> str:
    """Return the subscription key the catering site hands its own front end.

    Args:
        javascript: The bundle's source.
        source_url: Where it came from. Used only in the error message.

    Returns:
        The key.

    Raises:
        ChipotleSourceError: If the bundle no longer carries one. Falling back
            on a remembered value would be both a stale credential and one
            this repository had helped itself to.
    """
    match = _CATERING_KEY.search(javascript)
    if match is None:
        raise ChipotleSourceError(
            f"{source_url} no longer publishes a catering subscription key; "
            f"the catering menu cannot be read without one"
        )
    return match.group("key")


def _collect(
    fetch: _Fetch,
    home_url: str,
    store_count: int,
) -> PolicyDocuments:
    """Assemble the document set, however ``fetch`` chooses to obtain one.

    Three of these documents can only be addressed once an earlier one has
    been read — the catering key comes out of a bundle whose name comes out of
    a page, and a store's profile is keyed by a number only its locator page
    publishes. So this reads as it goes, exactly as the menu source reads the
    API configuration out of the home page before it can ask for a menu.
    """
    home = fetch(home_url, None)
    services = parse_services_config(home.text, home.source_url, home.harvested_at)
    json_headers = {"Accept": JSON_ACCEPT, **services.headers}

    catering_home = fetch(CATERING_URL, None)
    catering_bundle = fetch(
        bundle_url(catering_home.text, catering_home.source_url), None
    )
    key = catering_key(catering_bundle.text, catering_bundle.source_url)
    catering_menu = fetch(
        f"{services.base_url}{CATERING_MENU_PATH}",
        {"Accept": JSON_ACCEPT, SUBSCRIPTION_KEY_HEADER: key},
    )

    index = fetch(SITEMAP_URL, None)
    sitemaps = [index]
    sitemaps.extend(fetch(url, None) for url in sitemap_links(index.text))
    chosen = select_store_urls(
        store_page_urls([document.text for document in sitemaps[1:]]), store_count
    )

    stores = tuple(fetch(url, None) for url in chosen)
    profiles = tuple(
        fetch(
            services.restaurant_url(
                str(parse_store_page(document.text, document.source_url).store_id)
            ),
            json_headers,
        )
        for document in stores
    )

    return PolicyDocuments(
        services=services,
        home=home,
        rewards=fetch(REWARDS_URL, None),
        rewards_terms=fetch(REWARDS_TERMS_URL, None),
        faq=fetch(FAQ_URL, {"Accept": JSON_ACCEPT}),
        catering_home=catering_home,
        catering_bundle=catering_bundle,
        catering_menu=catering_menu,
        sitemaps=tuple(sitemaps),
        stores=stores,
        store_profiles=profiles,
    )
