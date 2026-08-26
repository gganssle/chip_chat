"""Wiring the Chipotle fixtures into a fake site.

The fixture bodies are real responses, trimmed: two entrees with a handful of
their real modifiers, one side, one drink, one item of serving hardware, two
of the published meals, four ingredients, twelve items' nutrition, ten lines
of the allergen chart, and the ``/allergens`` page's own prose. The prices are
the ones three real restaurants published on the same afternoon, which is why
the multi-store tests can assert that they differ without inventing a number.

Three things are altered, and each is named here because a fixture nobody can
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
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from chip_chat.harvest.sources.chipotle.config import (
    NUTRITION_PATH,
    NUTRITION_QUERY,
    ONLINE_MEALS_QUERY,
    ONLINE_MENU_QUERY,
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

REFERENCE = "0679"
COMPARISON = "1200"
CLOSED = "0100"


def read(name: str) -> bytes:
    """Return one fixture's bytes."""
    return (FIXTURES / name).read_bytes()


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


def site(
    home: str = "home.html", extra: dict[str, object] | None = None
) -> FakeTransport:
    """Return a transport serving the fixture site.

    Both origins answer 404 for ``robots.txt``, which is what they really do
    and what the framework reads as "this site published no rules".

    Args:
        home: Which home page fixture to serve.
        extra: Responses to add or override.

    Returns:
        The transport.
    """
    responses: dict[str, object] = {
        f"{HOME_URL}robots.txt": _missing(f"{HOME_URL}robots.txt"),
        f"{SERVICES}/robots.txt": _missing(f"{SERVICES}/robots.txt"),
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
    responses.update(extra or {})
    return FakeTransport(responses)


def _missing(url: str) -> HttpResponse:
    """A 404, which is what both origins answer for ``robots.txt``."""
    return fake_response(url, b"not found", status_code=404, content_type="text/html")


def rows(table: Sequence[Any]) -> list[dict[str, Any]]:
    """Return a table as plain dictionaries, for readable assertions."""
    payload = to_jsonl(table)
    return [json.loads(line) for line in payload.decode().splitlines()]
