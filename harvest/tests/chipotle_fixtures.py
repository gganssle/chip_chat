"""Wiring the Chipotle fixtures into a fake site.

The fixture bodies are real responses, trimmed: two entrees with a handful of
their real modifiers, one side, one drink, two of the published meals, and
four ingredients. The prices are the ones three real restaurants published on
the same afternoon, which is why the multi-store tests can assert that they
differ without inventing a number.

The one thing altered is the app key, replaced with an obvious placeholder.
The real one is published on Chipotle's home page and is not a secret, but a
test that asserts on the parsing does not need the value, and a repository
that carries someone else's credential invites someone to use it.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from chip_chat.harvest.sources.chipotle.config import (
    ONLINE_MEALS_QUERY,
    ONLINE_MENU_QUERY,
)
from chip_chat.harvest.sources.chipotle.records import to_jsonl
from chip_chat.harvest.testing import FakeTransport, fake_response
from chip_chat.harvest.transport import HttpResponse

FIXTURES = Path(__file__).parent / "fixtures" / "chipotle"

HOME_URL = "https://www.chipotle.com/"
SERVICES = "https://services.chipotle.com"
APP_KEY = "test-app-key-not-a-real-one"
INGREDIENTS_URL = f"{SERVICES}/menu-metadata/v1/menu-metadata/ingredients"

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
