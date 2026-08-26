"""The seeded synthetic account generator: the fake half of the two data planes.

RFC-001 section 04 holds two populations that must never blur — the real
catalogue, harvested and versioned, and the synthetic accounts generated
against it. This package is the second, and the boundary is the point:
everything Cilantro says about food comes from what Chipotle publishes, and
everything it says about "you" comes from a customer minted here.

    from chip_chat.catalog import load_catalog
    from chip_chat.data_gen import generate_population, load_config
    from chip_chat.data_gen.rewards import load_rewards_terms

    catalog, terms = load_catalog(blobs), load_rewards_terms(blobs)
    population = generate_population(catalog, terms, load_config())
    population.write(blobs)
    print(population.manifest()["population_version"])

Same seed, same population, byte for byte — asserted by
``test_determinism.py`` rather than claimed here. Every order composed only of
real catalogue rows — asserted by ``test_referential_integrity.py``, and made
unreachable rather than merely untested by
:class:`~chip_chat.data_gen.catalogue.OrderableMenu`, which is the only source
of an identifier in the package.

The ledger's arithmetic is in neither place. Issue #27 reads the earn rate,
the expiry window, the daily earning cap and every reward price off Chipotle's
published terms — see :mod:`chip_chat.data_gen.rewards` — so ``[loyalty]`` in
the config holds only what the ``reason`` column says and how eagerly an
archetype spends.

The remaining numbers that shape the population are in ``population.toml`` and
nowhere else; ``data-gen/README.md`` says which knob does what, and
``docs/decisions/synthetic-population.md`` argues the seven columns this
package carries that RFC-001 section 04 does not list.
"""

from chip_chat.data_gen.baskets import Line, Palate, compose, mint_palate
from chip_chat.data_gen.catalogue import Buildable, OrderableMenu, SlotChoices
from chip_chat.data_gen.config import (
    PACKAGED_CONFIG,
    WEEKDAY_NAMES,
    CatalogueConfig,
    Distribution,
    GeneratorConfig,
    LoyaltyConfig,
    NameConfig,
    OrderConfig,
    PersonaSpec,
    TimingConfig,
    load_config,
)
from chip_chat.data_gen.errors import (
    ConfigError,
    GeneratorError,
    RewardsTermsError,
    ThinCatalogError,
)
from chip_chat.data_gen.generate import generate_population
from chip_chat.data_gen.records import (
    DEFAULT_PREFIX,
    TABLES,
    Channel,
    DemoVisitor,
    LoyaltyEntry,
    Order,
    OrderItem,
    Persona,
    SyntheticPopulation,
)
from chip_chat.data_gen.rewards import (
    Citation,
    RewardsTerms,
    load_rewards_terms,
    rewards_by_name,
)
from chip_chat.otel import service_name

__all__ = [
    "DEFAULT_PREFIX",
    "PACKAGED_CONFIG",
    "SERVICE_NAME",
    "TABLES",
    "WEEKDAY_NAMES",
    "Buildable",
    "CatalogueConfig",
    "Channel",
    "Citation",
    "ConfigError",
    "DemoVisitor",
    "Distribution",
    "GeneratorConfig",
    "GeneratorError",
    "Line",
    "LoyaltyConfig",
    "LoyaltyEntry",
    "NameConfig",
    "Order",
    "OrderConfig",
    "OrderItem",
    "OrderableMenu",
    "Palate",
    "Persona",
    "PersonaSpec",
    "RewardsTerms",
    "RewardsTermsError",
    "SlotChoices",
    "SyntheticPopulation",
    "ThinCatalogError",
    "TimingConfig",
    "__version__",
    "compose",
    "generate_population",
    "load_config",
    "load_rewards_terms",
    "mint_palate",
    "rewards_by_name",
    "service_name",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("data-gen")
"""OpenTelemetry ``service.name`` for this component."""
