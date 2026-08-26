"""Where Chipotle publishes the address of its own menu API.

The ordering front end does not have the API host or its subscription key
compiled into a bundle. The public page hands both to the browser in a handful
of ``<meta>`` tags, and the browser then calls the API with them. This module
reads the same tags.

That is a deliberate choice, not a flourish. A harvester with a copied
subscription key pasted into a constant is a harvester that breaks silently on
the day the key is rotated, and one that has helped itself to a credential
rather than to a published fact. Reading the tags means the only thing this
source knows that any visitor's browser does not is which of the endpoints it
wants.

The tags, as published — the menu and nutrition sources need the first two,
and the policy source of issue #21 needs the third::

    <meta property="servicesconfig"
          data-host="https://services.chipotle.com"
          data-appkey="..."/>
    <meta property="mmd"
          data-ingredients="/menu-metadata/v1/menu-metadata/ingredients"
          data-allergens="/menu-metadata/v1/menu-metadata/allergendiets"
          data-baseUrl="https://services.chipotle.com"
          data-appKey="..."/>

    <meta property="rest"
          data-endpoint="/restaurant/v3/restaurant"
          data-appKey="..."/>
"""

from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote

from chip_chat.harvest.sources.chipotle.errors import ChipotleSourceError

HOME_URL = "https://www.chipotle.com/"
"""The page carrying the configuration tags. Nothing else is read from it."""

SUBSCRIPTION_KEY_HEADER = "Ocp-Apim-Subscription-Key"
"""The header the API gateway wants the published app key in."""

ONLINE_MENU_PATH = "/menuinnovation/v1/restaurants/{restaurant_id}/onlinemenu"
"""The per-restaurant menu, priced.

This one path is not advertised in a meta tag — it is the endpoint the
ordering flow itself calls — so it is named here rather than discovered. Its
query is fixed: ``channelId=web`` asks for the same menu a browser is served,
and ``includeUnavailableItems=true`` keeps an item that a particular
restaurant has run out of in the catalogue, flagged, instead of quietly
shrinking the menu to whatever was in stock at harvest time.
"""

ONLINE_MENU_QUERY = "channelId=web&includeUnavailableItems=true"

ONLINE_MEALS_PATH = "/menuinnovation/v1/restaurants/{restaurant_id}/onlinemeals"
"""The preconfigured meals one restaurant sells, priced.

Also called rather than advertised. Worth a second request per restaurant
because it is the only endpoint that publishes prose: Chipotle describes
*meals*, not items, and this is where those descriptions live.

The meta tag on the page still advertises an older, restaurant-free
``/menuinnovation/v1/onlinemeals``, which now answers 404. The tag is stale;
this path is what the ordering flow uses.
"""

ONLINE_MEALS_QUERY = "channelId=web"

NUTRITION_PATH = "/menu-metadata/v1/menu-metadata/nutrition"
"""The menu metadata with every item's published nutrition attached.

Not advertised either. The same metadata document the ``mmd`` tag's
``data-ingredients`` sibling serves is published a second time under this
path with a ``nutrition`` object on every item, and the nutrition calculator
asks for this one. Its query is the pair the site's own front end computes for
a US visitor on the web channel.
"""

NUTRITION_QUERY = "channel=web&region=US"

ALLERGENS_URL = "https://www.chipotle.com/allergens"
"""The page carrying Chipotle's own allergen caveats.

The chart on it is drawn client-side from :attr:`ServicesConfig.allergens_url`,
but the prose around the chart is served in the HTML and is published nowhere
else. That prose is the part issue #20 requires to survive verbatim: it is
where Chipotle says that the chart does not reflect contact during preparation
and that absence from it is not a guarantee.
"""

_CONFIG_TAGS = ("servicesconfig", "mmd", "rest")


class _MetaTagCollector(HTMLParser):
    """Collects the attributes of the ``<meta>`` tags the config lives in.

    ``html.parser`` lowercases attribute names, which is why the lookups below
    do not have to care that the page spells the same attribute ``data-appkey``
    in one tag and ``data-appKey`` in the other.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, dict[str, str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        attributes = {name: value or "" for name, value in attrs}
        name = attributes.get("property", "")
        if name in _CONFIG_TAGS and name not in self.tags:
            self.tags[name] = attributes


@dataclass(frozen=True, slots=True)
class ServicesConfig:
    """The API host, key and paths as the site published them.

    Attributes:
        base_url: Origin of the services API, without a trailing slash.
        app_key: The subscription key the page hands its own front end.
        ingredients_path: Path of the ingredient metadata endpoint, which is
            where the published descriptions and the ingredient taxonomy live.
        allergens_path: Path of the allergen and diet endpoint. Harvested by
            the nutrition source, not this one; carried so that source does
            not have to re-read the page.
        restaurant_path: Path of the restaurant endpoint, which is where a
            store's name and operational region are published. Harvested by
            the policy source of issue #21, and carried for the same reason.
        source_url: The page these values were read from.
        harvested_at: When that page was fetched.
    """

    base_url: str
    app_key: str
    ingredients_path: str
    allergens_path: str | None
    restaurant_path: str | None
    source_url: str
    harvested_at: datetime

    @property
    def headers(self) -> dict[str, str]:
        """The headers every call to the services API needs."""
        return {SUBSCRIPTION_KEY_HEADER: self.app_key}

    @property
    def ingredients_url(self) -> str:
        """Absolute URL of the ingredient metadata endpoint."""
        return f"{self.base_url}{self.ingredients_path}"

    @property
    def allergens_url(self) -> str | None:
        """Absolute URL of the allergen and diet endpoint, where published."""
        if self.allergens_path is None:
            return None
        return f"{self.base_url}{self.allergens_path}"

    def restaurant_url(self, restaurant_id: str) -> str:
        """Return the URL publishing one restaurant's name and region.

        Args:
            restaurant_id: The restaurant's identifier.

        Returns:
            The absolute URL.

        Raises:
            ChipotleSourceError: If the page published no restaurant endpoint.
        """
        if self.restaurant_path is None:
            raise ChipotleSourceError(
                f"{self.source_url} no longer publishes the restaurant endpoint "
                f"in its <meta> tags; there is nowhere left to read a store's "
                f"name from"
            )
        identifier = quote(restaurant_id, safe="")
        return f"{self.base_url}{self.restaurant_path}/{identifier}"

    @property
    def nutrition_url(self) -> str:
        """Absolute URL of the menu metadata with nutrition attached."""
        return f"{self.base_url}{NUTRITION_PATH}?{NUTRITION_QUERY}"

    def online_menu_url(self, restaurant_id: str) -> str:
        """Return the priced menu URL for one restaurant.

        Args:
            restaurant_id: The restaurant's identifier, as Chipotle's own
                ordering flow spells it.

        Returns:
            The absolute URL, query included.
        """
        return self._restaurant_url(ONLINE_MENU_PATH, restaurant_id, ONLINE_MENU_QUERY)

    def online_meals_url(self, restaurant_id: str) -> str:
        """Return the priced preconfigured-meals URL for one restaurant.

        Args:
            restaurant_id: The restaurant's identifier.

        Returns:
            The absolute URL, query included.
        """
        return self._restaurant_url(ONLINE_MEALS_PATH, restaurant_id, ONLINE_MEALS_QUERY)

    def _restaurant_url(self, path: str, restaurant_id: str, query: str) -> str:
        """Build a per-restaurant endpoint URL, escaping the identifier."""
        formatted = path.format(restaurant_id=quote(restaurant_id, safe=""))
        return f"{self.base_url}{formatted}?{query}"


def parse_services_config(
    html: str, source_url: str, harvested_at: datetime
) -> ServicesConfig:
    """Read the services configuration out of a page's ``<meta>`` tags.

    Args:
        html: The page source.
        source_url: Where it came from. Recorded on the result.
        harvested_at: When it was fetched. Recorded on the result.

    Returns:
        The configuration.

    Raises:
        ChipotleSourceError: If the page no longer carries a host, a key, or
            an ingredients path. Falling back to a remembered value here would
            turn a page redesign into a harvest that keeps running against a
            stale endpoint and never says so.
    """
    collector = _MetaTagCollector()
    collector.feed(html)
    services = collector.tags.get("servicesconfig", {})
    metadata = collector.tags.get("mmd", {})
    restaurants = collector.tags.get("rest", {})

    base_url = (services.get("data-host") or metadata.get("data-baseurl") or "").strip()
    app_key = (services.get("data-appkey") or metadata.get("data-appkey") or "").strip()
    ingredients_path = (metadata.get("data-ingredients") or "").strip()
    allergens_path = (metadata.get("data-allergens") or "").strip()
    restaurant_path = (restaurants.get("data-endpoint") or "").strip()

    missing = [
        name
        for name, value in (
            ("services host", base_url),
            ("app key", app_key),
            ("ingredients path", ingredients_path),
        )
        if not value
    ]
    if missing:
        raise ChipotleSourceError(
            f"{source_url} no longer publishes the {', '.join(missing)} "
            f"in a <meta> tag; the page layout has changed"
        )

    return ServicesConfig(
        base_url=base_url.rstrip("/"),
        app_key=app_key,
        ingredients_path=_absolute_path(ingredients_path),
        allergens_path=_absolute_path(allergens_path) if allergens_path else None,
        restaurant_path=(
            _absolute_path(restaurant_path).rstrip("/") if restaurant_path else None
        ),
        source_url=source_url,
        harvested_at=harvested_at,
    )


def _absolute_path(path: str) -> str:
    """Return ``path`` with a leading slash, however the page spelled it."""
    return path if path.startswith("/") else f"/{path}"
