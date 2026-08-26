"""The seeded synthetic account generator: the fake half of the two data planes.

RFC-001 section 04 holds two populations that must never blur — the real
catalogue, harvested and versioned, and the synthetic accounts generated
against it. This package is the second, and the boundary is the point:
everything Cilantro says about food comes from what Chipotle publishes, and
everything it says about "you" comes from a customer minted here.

    from chip_chat.catalog import load_catalog
    from chip_chat.data_gen import generate_population, load_config

    population = generate_population(load_catalog(blobs), load_config())
    population.write(blobs)
    print(population.manifest()["population_version"])

Same seed, same population, byte for byte — asserted by
``test_determinism.py`` rather than claimed here. Every order composed only of
real catalogue rows — asserted by ``test_referential_integrity.py``, and made
unreachable rather than merely untested by
:class:`~chip_chat.data_gen.catalogue.OrderableMenu`, which is the only source
of an identifier in the package.

Which customers a visitor is actually assigned is issue #26's question, and
:mod:`chip_chat.data_gen.fixtures` answers it: ``persona_fixtures`` holds the
exemplars of each archetype, each admitted only by clearing the bounds its
archetype sets on its own defining behaviour, each carrying a narrative
written from its real history.

The numbers that shape the population are in ``population.toml`` and nowhere
else; ``data-gen/README.md`` says which knob does what, and the decision
records under ``docs/decisions/`` argue the columns this package carries that
RFC-001 section 04 does not list.
"""

from chip_chat.data_gen.baskets import Line, Palate, compose, mint_palate
from chip_chat.data_gen.catalogue import Buildable, OrderableMenu, SlotChoices
from chip_chat.data_gen.config import (
    MEASURES,
    PACKAGED_CONFIG,
    WEEKDAY_NAMES,
    CatalogueConfig,
    Distribution,
    FixtureSpec,
    GeneratorConfig,
    LoyaltyConfig,
    NameConfig,
    OrderConfig,
    PersonaSpec,
    TimingConfig,
    load_config,
)
from chip_chat.data_gen.errors import ConfigError, GeneratorError, ThinCatalogError
from chip_chat.data_gen.fixtures import (
    CustomerFacts,
    entree_ids,
    measure_customers,
    select_fixtures,
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
    PersonaFixture,
    SyntheticPopulation,
)
from chip_chat.otel import service_name

__all__ = [
    "DEFAULT_PREFIX",
    "MEASURES",
    "PACKAGED_CONFIG",
    "SERVICE_NAME",
    "TABLES",
    "WEEKDAY_NAMES",
    "Buildable",
    "CatalogueConfig",
    "Channel",
    "ConfigError",
    "CustomerFacts",
    "DemoVisitor",
    "Distribution",
    "FixtureSpec",
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
    "PersonaFixture",
    "PersonaSpec",
    "SlotChoices",
    "SyntheticPopulation",
    "ThinCatalogError",
    "TimingConfig",
    "__version__",
    "compose",
    "entree_ids",
    "generate_population",
    "load_config",
    "measure_customers",
    "mint_palate",
    "select_fixtures",
    "service_name",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("data-gen")
"""OpenTelemetry ``service.name`` for this component."""
