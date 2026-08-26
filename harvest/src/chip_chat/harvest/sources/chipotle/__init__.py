"""Chipotle's published menu: items, descriptions, modifiers and prices.

Implements issue #19. What Chipotle publishes about its food is pulled from
the two JSON endpoints its own front end calls — the per-restaurant priced
menu, and the ingredient metadata that carries the descriptions and the
published ingredient taxonomy — landed raw through the harvest framework, and
parsed in a second step that never touches a network.

::

    from pathlib import Path

    from chip_chat.harvest import Harvester, HttpxTransport, LocalBlobStore
    from chip_chat.harvest.sources.chipotle import harvest_menu, parse_menu

    blobs = LocalBlobStore(Path("landing"))
    with Harvester(blobs, HttpxTransport()) as harvester:
        dataset = parse_menu(harvest_menu(harvester))
    dataset.write(blobs)

Run it a second time and it makes no requests. Run
:func:`~chip_chat.harvest.sources.chipotle.menu.load_menu` instead of
:func:`~chip_chat.harvest.sources.chipotle.menu.harvest_menu` and it cannot.
"""

from chip_chat.harvest.sources.chipotle.config import (
    HOME_URL,
    ONLINE_MENU_PATH,
    ONLINE_MENU_QUERY,
    SUBSCRIPTION_KEY_HEADER,
    ServicesConfig,
    parse_services_config,
)
from chip_chat.harvest.sources.chipotle.errors import (
    ChipotleSourceError,
    MissingDocumentError,
)
from chip_chat.harvest.sources.chipotle.menu import (
    DEFAULT_RESTAURANT_IDS,
    REFERENCE_RESTAURANT_ID,
    MenuDocuments,
    harvest_menu,
    load_menu,
    normalise_restaurant_ids,
)
from chip_chat.harvest.sources.chipotle.parse import parse_menu
from chip_chat.harvest.sources.chipotle.records import (
    DEFAULT_PARSED_PREFIX,
    TABLES,
    Ingredient,
    ItemIngredients,
    ItemPrice,
    MenuDataset,
    MenuItem,
    Modifier,
    ModifierGroup,
    PortionOption,
    to_jsonl,
)

__all__ = [
    "DEFAULT_PARSED_PREFIX",
    "DEFAULT_RESTAURANT_IDS",
    "HOME_URL",
    "ONLINE_MENU_PATH",
    "ONLINE_MENU_QUERY",
    "REFERENCE_RESTAURANT_ID",
    "SUBSCRIPTION_KEY_HEADER",
    "TABLES",
    "ChipotleSourceError",
    "Ingredient",
    "ItemIngredients",
    "ItemPrice",
    "MenuDataset",
    "MenuDocuments",
    "MenuItem",
    "MissingDocumentError",
    "Modifier",
    "ModifierGroup",
    "PortionOption",
    "ServicesConfig",
    "harvest_menu",
    "load_menu",
    "normalise_restaurant_ids",
    "parse_menu",
    "parse_services_config",
    "to_jsonl",
]
