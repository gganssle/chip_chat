"""Wiring the Chipotle fixtures into a fake site.

The fixture bodies are real responses, trimmed: two entrees with a handful of
their real modifiers, one side, one drink, one item of serving hardware, two
of the published meals, four ingredients, twelve items' nutrition, ten lines
of the allergen chart, the ``/allergens`` page's own prose, eleven of the
published FAQ answers, four catering packages, and the rewards terms with five
of their sections. The prices are the ones three real restaurants published on
the same afternoon, which is why the multi-store tests can assert that they
differ without inventing a number.

Five things are altered, and each is named here because a fixture nobody can
tell apart from a recording is a fixture that can quietly become fiction.

**The app key** is replaced with an obvious placeholder. The real one is
published on Chipotle's home page and is not a secret, but a test that asserts
on the parsing does not need the value, and a repository that carries someone
else's credential invites someone to use it.

**One nutrient is removed** — Vitamin C, from Jarritos Guava. The live
endpoint publishes all fourteen figures for all four hundred and eight items
it describes, so a fixture copied straight from it cannot demonstrate that a
figure nobody published stays null instead of turning into a zero. That is the
property issue #20 turns on, so the fixture is given a case of it.

**The menu fixtures gained a real non-food item**, Napkins & Utensils, which
the live menu publishes and which the metadata document describes nowhere. It
is the case that proves ``NOT_PUBLISHED`` is a status this dataset actually
reaches rather than one it merely declares.

**The catering subscription key** is a placeholder too, for the same reason as
the app key, and the bundle it sits in is one line rather than 260 kilobytes.

**The nutrition sheet PDF is built, not recorded** — because there is nothing
to record. Chipotle published no PDF at all on 26 August 2026, on any of the
pages this project harvests (see ``docs/chipotle-pdf-spot-check.md``), so
``nutrition-sheet.pdf`` is a one-page ruled table written for these tests. Its
figures are the fixture's own real ones, taken from ``nutrition.json``, with a
single deliberate exception: **Cheese is printed with 260 mg of sodium where the
calculator publishes 190**, which is the disagreement the reconciliation test
turns on. ``nutrition-sheet-layout.json`` beside it is *not* invented — it is
what the live Azure Document Intelligence account really returned for those
exact bytes on 26 August 2026, from ``prebuilt-layout`` at API version
``2024-11-30``, with only the per-word and per-line boxes and the ``styles``
array taken out. Nothing in this package reads those, and they were four fifths
of the file.

**The store locator is generated, not recorded.** Issue #21 needs at least
thirty stores and the parser refuses to build a dataset with fewer, so a
fixture of two recordings could not exercise it at all. :func:`locator_pages`
therefore builds two pages per state from the real page's markup — the same
schema.org ``Restaurant`` node, the same certified-fact block, the same order
links with the restaurant number in them — with invented addresses in real
states. One of them is not invented: ``ca/lakewood/5310-lakewood-blvd`` carries
restaurant 679's real address, telephone and coordinates, because that is the
restaurant the menu fixtures are priced at and the parser checks for it by
number. Three of the pages publish unusual hours — a weekday and weekend split,
a week with no Sunday at all, and a published ``Closed`` — because a locator
where every store keeps the same hours cannot show that the difference between
"closed" and "not published" survives.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from string import Template
from typing import Any

from chip_chat.harvest.sources.chipotle.config import (
    NUTRITION_PATH,
    NUTRITION_QUERY,
    ONLINE_MEALS_QUERY,
    ONLINE_MENU_QUERY,
)
from chip_chat.harvest.sources.chipotle.locator import LOCATOR_URL, SITEMAP_URL
from chip_chat.harvest.sources.chipotle.policy import (
    CATERING_MENU_PATH,
    CATERING_URL,
    FAQ_URL,
    REWARDS_TERMS_URL,
    REWARDS_URL,
)
from chip_chat.harvest.sources.chipotle.tables import to_jsonl
from chip_chat.harvest.testing import FakeTransport, fake_response
from chip_chat.harvest.transport import HttpResponse

FIXTURES = Path(__file__).parent / "fixtures" / "chipotle"

HOME_URL = "https://www.chipotle.com/"
SERVICES = "https://services.chipotle.com"
APP_KEY = "test-app-key-not-a-real-one"
INGREDIENTS_URL = f"{SERVICES}/menu-metadata/v1/menu-metadata/ingredients"
ALLERGEN_CHART_URL = f"{SERVICES}/menu-metadata/v1/menu-metadata/allergendiets"
NUTRITION_URL = f"{SERVICES}{NUTRITION_PATH}?{NUTRITION_QUERY}"
ALLERGENS_PAGE_URL = "https://www.chipotle.com/allergens"
NUTRITION_SHEET_URL = "https://www.chipotle.com/content/dam/nutrition-sheet.pdf"

REFERENCE = "0679"
COMPARISON = "1200"
CLOSED = "0100"

CATERING_KEY = "test-catering-key-not-a-real-one"
CATERING_BUNDLE_URL = f"{CATERING_URL}js/app.4f986208.js"
CATERING_MENU_URL = f"{SERVICES}{CATERING_MENU_PATH}"
RESTAURANT_PATH = "/restaurant/v3/restaurant"

STATES = (
    "al",
    "ar",
    "az",
    "ca",
    "co",
    "ct",
    "dc",
    "de",
    "fl",
    "ga",
    "ia",
    "id",
    "il",
    "in",
    "ks",
    "ky",
    "la",
    "ma",
    "md",
    "me",
    "mi",
    "mn",
    "mo",
    "ms",
    "mt",
    "nc",
    "nd",
    "ne",
    "nh",
    "nj",
    "nm",
    "nv",
    "ny",
    "oh",
    "ok",
    "or",
    "pa",
    "ri",
    "sc",
    "sd",
    "tn",
    "tx",
    "ut",
    "va",
    "vt",
    "wa",
    "wi",
    "wv",
    "wy",
)
"""Every state the real locator has a store in, so the round-robin selection
has as many to choose between as it really does."""

ALL_WEEK = '["Mo,Tu,We,Th,Fr,Sa,Su 10:45-23:00"]'
SPLIT_WEEK = '["Mo-Fr 10:45-21:00","Sa,Su 10:45-20:00"]'
NO_SUNDAY = '["Mo,Tu,We,Th,Fr,Sa 10:45-22:00"]'
CLOSED_SUNDAY = '["Mo,Tu,We,Th,Fr,Sa 10:45-22:00","Su Closed"]'

REFERENCE_STORE = {
    "state": "ca",
    "city": "Lakewood",
    "county_city": "Lakewood, Los Angeles",
    "slug": "lakewood",
    "street": "5310 Lakewood Blvd",
    "street_slug": "5310-lakewood-blvd",
    "region": "CA",
    "postal_code": "90712",
    "telephone": "+15627908786",
    "latitude": "33.854146",
    "longitude": "-118.1419424",
    "restaurant": 679,
    "opening_hours": ALL_WEEK,
}
"""Restaurant 679's real locator page, because the parser checks for it by
number and the menu fixtures are priced at it."""


def read(name: str) -> bytes:
    """Return one fixture's bytes."""
    return (FIXTURES / name).read_bytes()


def nutrition_sheet() -> bytes:
    """Return the fixture nutrition sheet's PDF bytes."""
    return read("nutrition-sheet.pdf")


def nutrition_sheet_layout() -> dict[str, Any]:
    """Return what Document Intelligence really returned for those bytes."""
    payload: dict[str, Any] = json.loads(read("nutrition-sheet-layout.json"))
    result: dict[str, Any] = payload["analyzeResult"]
    return result


def page_linking_to(url: str, *, page_url: str) -> HttpResponse:
    """Return a minimal HTML page whose only content is a link to ``url``.

    Synthetic, and obviously so. The recorded Chipotle pages are left exactly
    as they were fetched — none of them links to a PDF, which is the finding —
    so the discovery tests need a page that does, and it is built here rather
    than smuggled into a recording.
    """
    body = (
        f"<html><body><p>Download the "
        f'<a href="{url}">nutrition sheet</a>.</p></body></html>'
    ).encode()
    return fake_response(page_url, body, content_type="text/html")


def allergens_page_linking_to(url: str) -> HttpResponse:
    """Return the recorded ``/allergens`` page with one extra link in it.

    The recording itself is untouched — the link is appended at read time, and
    only here. The page has to stay parseable because the nutrition dataset
    quotes the caveats it publishes, so a stub page in its place would not do.
    """
    html = read("allergens.html").decode()
    link = f'<p><a href="{url}">Nutrition sheet (PDF)</a></p>'
    return fake_response(
        ALLERGENS_PAGE_URL,
        html.replace("</body>", f"{link}</body>").encode(),
        content_type="text/html",
    )


def menu_url(restaurant_id: str) -> str:
    """Return the priced-menu URL the source builds for a restaurant."""
    return (
        f"{SERVICES}/menuinnovation/v1/restaurants/{restaurant_id}"
        f"/onlinemenu?{ONLINE_MENU_QUERY}"
    )


def meals_url(restaurant_id: str) -> str:
    """Return the priced-meals URL the source builds for a restaurant."""
    return (
        f"{SERVICES}/menuinnovation/v1/restaurants/{restaurant_id}"
        f"/onlinemeals?{ONLINE_MEALS_QUERY}"
    )


def store_pages() -> tuple[dict[str, Any], ...]:
    """Return the stores the fixture locator publishes, in sitemap order.

    Two per state — one more than the round-robin selection needs, so that a
    default-sized harvest has to come back for a second pass exactly as it does
    against the real sitemap, which lists four thousand.
    """
    stores: list[dict[str, Any]] = []
    for variant in (0, 1):
        for index, state in enumerate(STATES):
            if variant == 0 and state == REFERENCE_STORE["state"]:
                stores.append(dict(REFERENCE_STORE))
                continue
            number = 2000 + index * 2 + variant
            hours = ALL_WEEK
            if number == 2000:
                hours = SPLIT_WEEK
            elif number == 2002:
                hours = NO_SUNDAY
            elif number == 2004:
                hours = CLOSED_SUNDAY
            city = f"{state.upper()} Town {variant + 1}"
            stores.append(
                {
                    "state": state,
                    "city": city,
                    "county_city": city,
                    "slug": f"{state}-town-{variant + 1}",
                    "street": f"{100 + number} Main St",
                    "street_slug": f"{100 + number}-main-st",
                    "region": state.upper(),
                    "postal_code": f"{10000 + number:05d}",
                    "telephone": f"+1555{number:07d}",
                    "latitude": f"{40 + number / 10000:.4f}",
                    "longitude": f"{-90 - number / 10000:.4f}",
                    "restaurant": number,
                    "opening_hours": hours,
                }
            )
    return tuple(stores)


def store_page_url(store: dict[str, Any]) -> str:
    """Return the locator URL for one fixture store."""
    return f"{LOCATOR_URL}{store['state']}/{store['slug']}/{store['street_slug']}"


def restaurant_url(restaurant: int) -> str:
    """Return the profile URL the source builds for a restaurant number."""
    return f"{SERVICES}{RESTAURANT_PATH}/{restaurant}"


def locator_page(store: dict[str, Any]) -> str:
    """Return one fixture locator page, built from the real page's markup."""
    template = Template((FIXTURES / "locator-store.html").read_text())
    return template.substitute(store)


def locator_pages() -> dict[str, object]:
    """Return the fixture locator: a sitemap index, a sitemap, and the pages."""
    stores = store_pages()
    index = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<sitemap><loc>{LOCATOR_URL}sitemap1.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    locations = [f"{LOCATOR_URL}", f"{LOCATOR_URL}404.html"]
    for store in stores:
        locations.append(f"{LOCATOR_URL}{store['state']}")
        locations.append(f"{LOCATOR_URL}{store['state']}/{store['slug']}")
        locations.append(store_page_url(store))
        locations.append(f"{store_page_url(store)}/order-delivery")
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(f"<url><loc>{url}</loc></url>" for url in locations)
        + "</urlset>"
    )
    responses: dict[str, object] = {
        f"{LOCATOR_URL}robots.txt": fake_response(
            f"{LOCATOR_URL}robots.txt",
            f"User-agent: *\nSitemap: {SITEMAP_URL}\n".encode(),
            content_type="text/plain",
        ),
        SITEMAP_URL: fake_response(
            SITEMAP_URL, index.encode(), content_type="application/xml"
        ),
        f"{LOCATOR_URL}sitemap1.xml": fake_response(
            f"{LOCATOR_URL}sitemap1.xml",
            sitemap.encode(),
            content_type="application/xml",
        ),
    }
    for store in stores:
        url = store_page_url(store)
        responses[url] = fake_response(
            url, locator_page(store).encode(), content_type="text/html"
        )
        profile = restaurant_url(int(store["restaurant"]))
        responses[profile] = fake_response(
            profile,
            json.dumps(
                {
                    "restaurantNumber": store["restaurant"],
                    "restaurantName": f"{store['city']} Mall",
                    "restaurantLocationType": "RESTAURANT",
                    "restaurantStatus": "OPEN",
                    "openDate": "2005-04-25T00:00:00",
                    "realEstateCategory": "Freestanding",
                    "operationalRegion": "Pacific South",
                    "operationalSubRegion": "PS LA South",
                    "designatedMarketAreaName": store["city"].upper(),
                }
            ).encode(),
        )
    return responses


def site(
    home: str = "home.html", extra: dict[str, object] | None = None
) -> FakeTransport:
    """Return a transport serving the fixture site.

    Three origins answer 404 for ``robots.txt``, which is what they really do
    and what the framework reads as "this site published no rules". The
    locator answers with the rules it really publishes, which forbid nothing.

    Args:
        home: Which home page fixture to serve.
        extra: Responses to add or override.

    Returns:
        The transport.
    """
    responses: dict[str, object] = {
        f"{HOME_URL}robots.txt": _missing(f"{HOME_URL}robots.txt"),
        f"{SERVICES}/robots.txt": _missing(f"{SERVICES}/robots.txt"),
        f"{CATERING_URL}robots.txt": _missing(f"{CATERING_URL}robots.txt"),
        REWARDS_URL: fake_response(
            REWARDS_URL, read("rewards.html"), content_type="text/html"
        ),
        REWARDS_TERMS_URL: fake_response(
            REWARDS_TERMS_URL, read("rewards-terms.html"), content_type="text/html"
        ),
        FAQ_URL: fake_response(FAQ_URL, read("faq.json")),
        CATERING_URL: fake_response(
            CATERING_URL, read("catering-home.html"), content_type="text/html"
        ),
        CATERING_BUNDLE_URL: fake_response(
            CATERING_BUNDLE_URL,
            read("catering-app.js"),
            content_type="application/javascript",
        ),
        CATERING_MENU_URL: fake_response(CATERING_MENU_URL, read("catering-menu.json")),
        HOME_URL: fake_response(HOME_URL, read(home), content_type="text/html"),
        INGREDIENTS_URL: fake_response(INGREDIENTS_URL, read("ingredients.json")),
        NUTRITION_URL: fake_response(NUTRITION_URL, read("nutrition.json")),
        ALLERGEN_CHART_URL: fake_response(ALLERGEN_CHART_URL, read("allergendiets.json")),
        ALLERGENS_PAGE_URL: fake_response(
            ALLERGENS_PAGE_URL, read("allergens.html"), content_type="text/html"
        ),
    }
    for restaurant_id in (REFERENCE, COMPARISON, CLOSED):
        responses[menu_url(restaurant_id)] = fake_response(
            menu_url(restaurant_id), read(f"onlinemenu-{restaurant_id}.json")
        )
        responses[meals_url(restaurant_id)] = fake_response(
            meals_url(restaurant_id), read("onlinemeals-0679.json")
        )
    responses.update(locator_pages())
    responses.update(extra or {})
    return FakeTransport(responses)


def _missing(url: str) -> HttpResponse:
    """A 404, which is what both origins answer for ``robots.txt``."""
    return fake_response(url, b"not found", status_code=404, content_type="text/html")


def rows(table: Sequence[Any]) -> list[dict[str, Any]]:
    """Return a table as plain dictionaries, for readable assertions."""
    payload = to_jsonl(table)
    return [json.loads(line) for line in payload.decode().splitlines()]
