"""Chipotle's published menu, nutrition and allergen data.

Two datasets, one source, four steps between them. Issue #19 pulls what
Chipotle publishes about its food — items, descriptions, the modifier
taxonomy, and prices. Issue #20 pulls the safety-critical half: nutrition per
item, the allergen chart, and the prose Chipotle publishes about what that
chart does not cover. Both land raw bytes through the harvest framework and
parse them in a second step that never touches a network.

::

    from pathlib import Path

    from chip_chat.harvest import Harvester, HttpxTransport, LocalBlobStore
    from chip_chat.harvest.sources.chipotle import (
        harvest_menu,
        harvest_nutrition,
        parse_menu,
        parse_nutrition,
    )

    blobs = LocalBlobStore(Path("landing"))
    with Harvester(blobs, HttpxTransport()) as harvester:
        parse_menu(harvest_menu(harvester)).write(blobs)
        parse_nutrition(harvest_nutrition(harvester)).write(blobs)

Run it a second time and it makes no requests. Run
:func:`~chip_chat.harvest.sources.chipotle.menu.load_menu` and
:func:`~chip_chat.harvest.sources.chipotle.nutrition.load_nutrition` instead
of the harvest functions and it cannot.
"""

from chip_chat.harvest.sources.chipotle.caveats import CaveatBlock, parse_caveats
from chip_chat.harvest.sources.chipotle.config import (
    ALLERGENS_URL,
    HOME_URL,
    NUTRITION_PATH,
    NUTRITION_QUERY,
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
from chip_chat.harvest.sources.chipotle.nutrition import (
    NutritionDocuments,
    harvest_nutrition,
    load_nutrition,
)
from chip_chat.harvest.sources.chipotle.nutrition_parse import parse_nutrition
from chip_chat.harvest.sources.chipotle.nutrition_records import (
    DEFAULT_PARSED_PREFIX as DEFAULT_NUTRITION_PREFIX,
)
from chip_chat.harvest.sources.chipotle.nutrition_records import (
    TABLES as NUTRITION_TABLES,
)
from chip_chat.harvest.sources.chipotle.nutrition_records import (
    AllergenChartRow,
    AllergenStatus,
    Caveat,
    DietaryTag,
    DietStatus,
    ItemAllergen,
    ItemDiet,
    ItemGroupCalories,
    ItemNutrient,
    Nutrient,
    NutritionDataset,
    TagKind,
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
)
from chip_chat.harvest.sources.chipotle.tables import to_jsonl

__all__ = [
    "ALLERGENS_URL",
    "DEFAULT_NUTRITION_PREFIX",
    "DEFAULT_PARSED_PREFIX",
    "DEFAULT_RESTAURANT_IDS",
    "HOME_URL",
    "NUTRITION_PATH",
    "NUTRITION_QUERY",
    "NUTRITION_TABLES",
    "ONLINE_MENU_PATH",
    "ONLINE_MENU_QUERY",
    "REFERENCE_RESTAURANT_ID",
    "SUBSCRIPTION_KEY_HEADER",
    "TABLES",
    "AllergenChartRow",
    "AllergenStatus",
    "Caveat",
    "CaveatBlock",
    "ChipotleSourceError",
    "DietStatus",
    "DietaryTag",
    "Ingredient",
    "ItemAllergen",
    "ItemDiet",
    "ItemGroupCalories",
    "ItemIngredients",
    "ItemNutrient",
    "ItemPrice",
    "MenuDataset",
    "MenuDocuments",
    "MenuItem",
    "MissingDocumentError",
    "Modifier",
    "ModifierGroup",
    "Nutrient",
    "NutritionDataset",
    "NutritionDocuments",
    "PortionOption",
    "ServicesConfig",
    "TagKind",
    "harvest_menu",
    "harvest_nutrition",
    "load_menu",
    "load_nutrition",
    "normalise_restaurant_ids",
    "parse_caveats",
    "parse_menu",
    "parse_nutrition",
    "parse_services_config",
    "to_jsonl",
]
