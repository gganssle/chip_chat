"""Chipotle's published menu, nutrition, allergen and policy data.

Three datasets, one source, two steps each. Issue #19 pulls what Chipotle
publishes about its food — items, descriptions, the modifier taxonomy, and
prices. Issue #20 pulls the safety-critical half: nutrition per item, the
allergen chart, and the prose Chipotle publishes about what that chart does
not cover. Issue #21 pulls the policy half: the rewards terms and the rewards
themselves, the ordering, refund and catering answers, the catering menu, and
enough real stores that a home store means something. All three land raw bytes
through the harvest framework and parse them in a second step that never
touches a network.

::

    from pathlib import Path

    from chip_chat.harvest import Harvester, HttpxTransport, LocalBlobStore
    from chip_chat.harvest.sources.chipotle import (
        harvest_menu,
        harvest_nutrition,
        harvest_policy,
        parse_menu,
        parse_nutrition,
        parse_policy,
    )

    blobs = LocalBlobStore(Path("landing"))
    with Harvester(blobs, HttpxTransport()) as harvester:
        parse_menu(harvest_menu(harvester)).write(blobs)
        parse_nutrition(harvest_nutrition(harvester)).write(blobs)
        parse_policy(harvest_policy(harvester)).write(blobs)

Run it a second time and it makes no requests. Run
:func:`~chip_chat.harvest.sources.chipotle.menu.load_menu`,
:func:`~chip_chat.harvest.sources.chipotle.nutrition.load_nutrition` and
:func:`~chip_chat.harvest.sources.chipotle.policy.load_policy` instead of the
harvest functions and it cannot.
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
from chip_chat.harvest.sources.chipotle.locator import (
    DEFAULT_STORE_COUNT,
    REFERENCE_STORE_URL,
    OpeningHours,
    StorePage,
    parse_opening_hours,
    parse_store_page,
    select_store_urls,
    store_page_urls,
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
from chip_chat.harvest.sources.chipotle.policy import (
    CATERING_URL,
    FAQ_URL,
    REWARDS_TERMS_URL,
    REWARDS_URL,
    PolicyDocuments,
    harvest_policy,
    load_policy,
)
from chip_chat.harvest.sources.chipotle.policy_parse import parse_policy
from chip_chat.harvest.sources.chipotle.policy_records import (
    DEFAULT_PARSED_PREFIX as DEFAULT_POLICY_PREFIX,
)
from chip_chat.harvest.sources.chipotle.policy_records import (
    MINIMUM_STORES,
    CateringOption,
    CateringPackage,
    FaqCategory,
    FaqEntry,
    PolicyDataset,
    PolicyDocument,
    PolicySection,
    Reward,
    Store,
    StoreHours,
    StoreProfile,
)
from chip_chat.harvest.sources.chipotle.policy_records import (
    TABLES as POLICY_TABLES,
)
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
from chip_chat.harvest.sources.chipotle.sections import Document, Section, parse_document
from chip_chat.harvest.sources.chipotle.tables import to_jsonl

__all__ = [
    "ALLERGENS_URL",
    "CATERING_URL",
    "DEFAULT_NUTRITION_PREFIX",
    "DEFAULT_PARSED_PREFIX",
    "DEFAULT_POLICY_PREFIX",
    "DEFAULT_RESTAURANT_IDS",
    "DEFAULT_STORE_COUNT",
    "FAQ_URL",
    "HOME_URL",
    "MINIMUM_STORES",
    "NUTRITION_PATH",
    "NUTRITION_QUERY",
    "NUTRITION_TABLES",
    "ONLINE_MENU_PATH",
    "ONLINE_MENU_QUERY",
    "POLICY_TABLES",
    "REFERENCE_RESTAURANT_ID",
    "REFERENCE_STORE_URL",
    "REWARDS_TERMS_URL",
    "REWARDS_URL",
    "SUBSCRIPTION_KEY_HEADER",
    "TABLES",
    "AllergenChartRow",
    "AllergenStatus",
    "CateringOption",
    "CateringPackage",
    "Caveat",
    "CaveatBlock",
    "ChipotleSourceError",
    "DietStatus",
    "DietaryTag",
    "Document",
    "FaqCategory",
    "FaqEntry",
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
    "OpeningHours",
    "PolicyDataset",
    "PolicyDocument",
    "PolicyDocuments",
    "PolicySection",
    "PortionOption",
    "Reward",
    "Section",
    "ServicesConfig",
    "Store",
    "StoreHours",
    "StorePage",
    "StoreProfile",
    "TagKind",
    "harvest_menu",
    "harvest_nutrition",
    "harvest_policy",
    "load_menu",
    "load_nutrition",
    "load_policy",
    "normalise_restaurant_ids",
    "parse_caveats",
    "parse_document",
    "parse_menu",
    "parse_nutrition",
    "parse_opening_hours",
    "parse_policy",
    "parse_services_config",
    "parse_store_page",
    "select_store_urls",
    "store_page_urls",
    "to_jsonl",
]
