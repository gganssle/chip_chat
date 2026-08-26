"""The menu catalogue: the single source of truth for what is orderable.

Three harvests go in — the menu of issue #19, the nutrition and allergen data
of issue #20, the stores of issue #21 — and eight tables come out. Three
subsystems resolve against those tables and none of them may name a food that
is not in them: the synthetic order generator of issue #25 composes orders only
from real catalogue rows, the vision matcher of issue #54 resolves described
slots to catalogue SKUs through a vocabulary generated from
:attr:`~chip_chat.catalog.records.MenuCatalog.vocabulary`, and the retrieval
chunker of issue #35 treats one row of ``menu_items`` as one chunk.

    from chip_chat.catalog import build_catalog, load_catalog

    catalog = build_catalog(menu, nutrition, policy)
    catalog.write(blobs)
    print(catalog.manifest()["content_version"])

Read ``catalog/README.md`` before answering an allergen question out of any of
it, and :class:`~chip_chat.catalog.records.AllergenDisclosure` before treating
an absent allergen code as an absent allergen.
"""

from chip_chat.catalog.build import CALORIE_NUTRIENT_KEY, build_catalog
from chip_chat.catalog.errors import (
    CatalogError,
    CatalogLoadError,
    MissingSourceError,
    VocabularyCollisionError,
)
from chip_chat.catalog.load import load_catalog
from chip_chat.catalog.records import (
    DEFAULT_PREFIX,
    PROVENANCE_SUFFIXES,
    TABLES,
    Allergen,
    AllergenDisclosure,
    Caveat,
    Derivation,
    ItemAllergen,
    ItemPrice,
    MenuCatalog,
    MenuItem,
    Modifier,
    Slot,
    Store,
    StoreHours,
    VocabularyTerm,
)
from chip_chat.catalog.vocabulary import build_vocabulary, render_module, slot_of
from chip_chat.otel import service_name

__all__ = [
    "CALORIE_NUTRIENT_KEY",
    "DEFAULT_PREFIX",
    "PROVENANCE_SUFFIXES",
    "SERVICE_NAME",
    "TABLES",
    "Allergen",
    "AllergenDisclosure",
    "CatalogError",
    "CatalogLoadError",
    "Caveat",
    "Derivation",
    "ItemAllergen",
    "ItemPrice",
    "MenuCatalog",
    "MenuItem",
    "MissingSourceError",
    "Modifier",
    "Slot",
    "Store",
    "StoreHours",
    "VocabularyCollisionError",
    "VocabularyTerm",
    "__version__",
    "build_catalog",
    "build_vocabulary",
    "load_catalog",
    "render_module",
    "slot_of",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("catalog")
"""OpenTelemetry ``service.name`` for this component."""
