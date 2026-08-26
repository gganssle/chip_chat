"""Reading Chipotle's published store locator.

``locations.chipotle.com`` is a separate site from the ordering front end, and
a friendlier one: its ``robots.txt`` disallows nothing and names a sitemap, and
every store page carries a schema.org ``Restaurant`` node with the address,
telephone and opening hours already structured. Four thousand of those pages
exist; issue #21 needs at least thirty, so the interesting question is *which*
thirty, and the answer has to be the same on every run or the dataset stops
being reproducible.

**The choice is made from the published sitemap, not from a list somebody
typed.** Store URLs are sorted, grouped by the state in their path, and taken
one state at a time round-robin, so fifty stores are one in each of fifty
states rather than fifty branches of Los Angeles. Nothing here knows the name
of a city.

**One store is named, and it is named for a reason.** The menu harvest prices
its catalogue at restaurant 0679, and a price with no place attached is what
``docs/decisions/menu-pricing.md`` exists to avoid — so that restaurant's page is
always in the set. It is *checked*, not trusted: the page has to publish
restaurant number 679 or the harvest stops, because a locator that quietly
moved that URL to a different restaurant would otherwise attach the reference
prices to the wrong address.

The store's *name* is not here. Every one of these pages calls its restaurant
"Chipotle Mexican Grill"; it is the restaurant API that knows one of them is
"Ballard", which is why :mod:`policy` fetches that too.
"""

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from chip_chat.harvest.sources.chipotle.errors import ChipotleSourceError

LOCATOR_URL = "https://locations.chipotle.com/"
"""The store locator's origin. Its ``robots.txt`` names the sitemap below."""

SITEMAP_URL = f"{LOCATOR_URL}sitemap.xml"
"""The sitemap index, which is a list of sitemaps rather than of pages."""

REFERENCE_STORE_URL = f"{LOCATOR_URL}ca/lakewood/5310-lakewood-blvd"
"""The locator page for the restaurant the menu harvest prices at.

Hardcoded because it cannot be derived: the locator's URLs are built from
addresses and publish the restaurant number only inside the page. So the URL is
named here and the number it publishes is asserted at parse time, which turns a
constant that could go stale into one that says so.
"""

DEFAULT_STORE_COUNT = 50
"""How many stores to harvest unless the caller asks for another number.

Comfortably above the thirty issue #21 requires, and — because the selection
takes one state at a time — enough to reach every state and territory the
locator currently lists a restaurant in before it visits any of them twice.
"""

DAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
"""The week, in the order ``store_hours`` lists it."""

_DAY_CODES = {
    "Mo": "Monday",
    "Tu": "Tuesday",
    "We": "Wednesday",
    "Th": "Thursday",
    "Fr": "Friday",
    "Sa": "Saturday",
    "Su": "Sunday",
}
_HOURS_PATTERN = re.compile(
    r"^(?P<days>[A-Za-z,\- ]+?)\s+(?P<times>Closed|\d{1,2}:\d{2}-\d{1,2}:\d{2})$"
)
_ORDER_LINK = re.compile(r"[?&]restaurant=(\d+)\b")
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL)


@dataclass(frozen=True, slots=True)
class OpeningHours:
    """One day's published opening times.

    Attributes:
        day: The day of the week, spelled as :data:`DAYS` spells it.
        opens: Published opening time, ``HH:MM``.
        closes: Published closing time.
    """

    day: str
    opens: str | None
    closes: str | None


@dataclass(frozen=True, slots=True)
class StorePage:
    """One store's locator page, read.

    Attributes:
        store_id: The restaurant number the page's own order links carry.
        page_url: The page's URL.
        street_address: The street line.
        city: The city.
        region: The state or territory code.
        postal_code: The postcode.
        country: The country code.
        telephone: The published telephone number.
        latitude: Published latitude, where the page publishes coordinates.
        longitude: Published longitude.
        hours: One entry per day the page published times for.
    """

    store_id: int
    page_url: str
    street_address: str | None
    city: str | None
    region: str | None
    postal_code: str | None
    country: str | None
    telephone: str | None
    latitude: float | None
    longitude: float | None
    hours: tuple[OpeningHours, ...]


class _LinkedDataCollector(HTMLParser):
    """Collects the bodies of every ``application/ld+json`` script on a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "script":
            return
        types = {value for name, value in attrs if name == "type"}
        if "application/ld+json" in types:
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._parts is not None:
            self.blocks.append("".join(self._parts))
            self._parts = None

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)


def sitemap_links(xml: str) -> tuple[str, ...]:
    """Return every ``<loc>`` in a sitemap or sitemap index, in order.

    Args:
        xml: The document's text.

    Returns:
        The URLs, de-duplicated, in the order they appear.
    """
    seen: dict[str, None] = {}
    for match in _LOC.finditer(xml):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def store_page_urls(sitemaps: list[str]) -> tuple[str, ...]:
    """Return the store detail pages listed across several sitemaps.

    A store page is three path segments — state, city, street. The same
    sitemaps carry the state and city directories above them and an
    ``/order-delivery`` page below, and neither describes a restaurant.

    Args:
        sitemaps: The text of each sitemap.

    Returns:
        The store page URLs, sorted, so the selection below is stable.
    """
    urls: set[str] = set()
    for xml in sitemaps:
        for url in sitemap_links(xml):
            if _state_of(url) is not None:
                urls.add(url)
    return tuple(sorted(urls))


def select_store_urls(
    urls: tuple[str, ...],
    count: int = DEFAULT_STORE_COUNT,
    *,
    required: str = REFERENCE_STORE_URL,
) -> tuple[str, ...]:
    """Choose which store pages to harvest, spreading them across states.

    Args:
        urls: Every store page the sitemap lists, sorted.
        count: How many to choose.
        required: A page that must be in the result whatever else is. The
            reference restaurant's, so that the prices issue #19 harvested
            have an address to belong to.

    Returns:
        The chosen URLs: ``required`` first, then one per state in turn.

    Raises:
        ValueError: If ``count`` is less than one.
        ChipotleSourceError: If the sitemap no longer lists ``required``, or
            lists fewer pages than were asked for.
    """
    if count < 1:
        raise ValueError(f"count must be at least 1, got {count}")
    if required not in urls:
        raise ChipotleSourceError(
            f"the store locator's sitemap no longer lists {required}, which is "
            f"the reference restaurant's page; there is nowhere left to read "
            f"the address the harvested prices belong to"
        )
    if len(urls) < count:
        raise ChipotleSourceError(
            f"the store locator's sitemap lists {len(urls)} store pages, "
            f"fewer than the {count} asked for"
        )

    by_state: dict[str, list[str]] = {}
    for url in urls:
        if url == required:
            continue
        by_state.setdefault(_state_of(url) or "", []).append(url)

    chosen = [required]
    states = sorted(by_state)
    round_index = 0
    while len(chosen) < count:
        added = False
        for state in states:
            if len(chosen) >= count:
                break
            pages = by_state[state]
            if round_index < len(pages):
                chosen.append(pages[round_index])
                added = True
        if not added:
            break
        round_index += 1
    return tuple(chosen)


def parse_store_page(html: str, source_url: str) -> StorePage:
    """Read one locator page into a :class:`StorePage`.

    The address, telephone and opening hours come from the page's schema.org
    ``Restaurant`` node — a standard vocabulary, so a locator rebuilt on a
    different platform would still be read. The coordinates come from the
    locator platform's own extra block and are ``None`` when it is absent,
    because they are a bonus rather than something issue #21 asked for.

    Args:
        html: The page source.
        source_url: Where it came from.

    Returns:
        The store.

    Raises:
        ChipotleSourceError: If the page publishes no restaurant number, more
            than one, or no ``Restaurant`` node at all.
    """
    restaurant = _restaurant_number(html, source_url)
    restaurant_node: dict[str, object] | None = None
    coordinates: dict[str, object] | None = None
    collector = _LinkedDataCollector()
    collector.feed(html)
    for block in collector.blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        for node in payload.get("@graph") or []:
            if isinstance(node, dict) and node.get("@type") == "Restaurant":
                restaurant_node = restaurant_node or node
        subject = payload.get("credentialSubject")
        if isinstance(subject, dict) and isinstance(subject.get("geo"), dict):
            coordinates = coordinates or subject["geo"]

    if restaurant_node is None:
        raise ChipotleSourceError(
            f"{source_url} publishes no schema.org Restaurant node; the "
            f"locator's page layout has changed"
        )
    address = restaurant_node.get("address")
    address = address if isinstance(address, dict) else {}
    return StorePage(
        store_id=restaurant,
        page_url=source_url,
        street_address=_string(address.get("streetAddress")),
        city=_string(address.get("addressLocality")),
        region=_string(address.get("addressRegion")),
        postal_code=_string(address.get("postalCode")),
        country=_string(address.get("addressCountry")),
        telephone=_string(restaurant_node.get("telephone")),
        latitude=_coordinate((coordinates or {}).get("latitude")),
        longitude=_coordinate((coordinates or {}).get("longitude")),
        hours=parse_opening_hours(restaurant_node.get("openingHours"), source_url),
    )


def parse_opening_hours(published: object, source_url: str) -> tuple[OpeningHours, ...]:
    """Read schema.org ``openingHours`` strings into one entry per day.

    The published form is ``"Mo,Tu,We,Th,Fr 10:45-21:00"``, sometimes with a
    day range, sometimes with ``Closed`` in place of the times, and often more
    than one string for a week that is not the same every day.

    Args:
        published: The value of the node's ``openingHours``.
        source_url: Where it came from. Used in the error message.

    Returns:
        One entry per day named, in :data:`DAYS` order.

    Raises:
        ChipotleSourceError: If a specification is not in a form this
            understands. Guessing at opening hours is how a visitor is sent to
            a closed restaurant.
    """
    if published is None:
        return ()
    entries = published if isinstance(published, list) else [published]
    found: dict[str, OpeningHours] = {}
    for entry in entries:
        if not isinstance(entry, str):
            raise ChipotleSourceError(
                f"{source_url} publishes opening hours that are not text: {entry!r}"
            )
        match = _HOURS_PATTERN.match(entry.strip())
        if match is None:
            raise ChipotleSourceError(
                f"{source_url} publishes opening hours this source cannot read: {entry!r}"
            )
        times = match.group("times")
        opens, closes = (None, None) if times == "Closed" else times.split("-", 1)
        for day in _days_named(match.group("days"), entry, source_url):
            found[day] = OpeningHours(day=day, opens=opens, closes=closes)
    return tuple(found[day] for day in DAYS if day in found)


def _days_named(spec: str, entry: str, source_url: str) -> list[str]:
    """Expand ``Mo,Tu`` and ``Mo-Fr`` into full day names."""
    days: list[str] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            first, _, last = part.partition("-")
            start, end = _day(first, entry, source_url), _day(last, entry, source_url)
            span = DAYS[DAYS.index(start) : DAYS.index(end) + 1]
            if not span:
                raise ChipotleSourceError(
                    f"{source_url} publishes a day range that runs backwards: {entry!r}"
                )
            days.extend(span)
        else:
            days.append(_day(part, entry, source_url))
    return days


def _day(code: str, entry: str, source_url: str) -> str:
    """Return the full name of a two-letter day code."""
    day = _DAY_CODES.get(code[:2].title())
    if day is None:
        raise ChipotleSourceError(
            f"{source_url} publishes a day this source does not know: {entry!r}"
        )
    return day


def _restaurant_number(html: str, source_url: str) -> int:
    """Return the restaurant number the page's own order links carry."""
    found = {int(match.group(1)) for match in _ORDER_LINK.finditer(html)}
    if not found:
        raise ChipotleSourceError(
            f"{source_url} publishes no restaurant number in its order links; "
            f"there is no way to join this store to the prices harvested for it"
        )
    if len(found) > 1:
        raise ChipotleSourceError(
            f"{source_url} publishes more than one restaurant number "
            f"({sorted(found)}); which one the page is about is a guess"
        )
    return found.pop()


def _state_of(url: str) -> str | None:
    """Return the state segment of a store page URL, or ``None`` if it is not one."""
    if not url.startswith(LOCATOR_URL):
        return None
    segments = [part for part in urlsplit(url).path.split("/") if part]
    if len(segments) != 3:
        return None
    return segments[0]


def _string(value: object) -> str | None:
    """Return a published string, or ``None`` for anything else."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coordinate(value: object) -> float | None:
    """Return a published coordinate as a float, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None
