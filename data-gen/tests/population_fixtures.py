"""One catalogue and one population, built once and shared by every test.

The catalogue is the fixture committed by issue #24, which exists for exactly
this: "so that issue #25's generator and issue #54's matcher have something to
resolve against on a laptop with no network". Reading it here rather than
rebuilding it from the harvest fixtures means these tests fail when the
committed fixture and the loader disagree, which is a thing worth finding out.

It is a *small* catalogue — two entrees, five modifiers each, thirty stores —
so the tests below assert properties rather than quantities wherever the
quantity would be an artefact of its size. Proving the population is not thin
against a real catalogue is issue #28, and it needs a real harvest to mean
anything.

The population is generated from the *shipped* ``population.toml``, not from a
test-local copy. A config the tests never load is a config that can rot.
"""

import dataclasses
import sys
from functools import cache
from pathlib import Path

from chip_chat.catalog import MenuCatalog, load_catalog
from chip_chat.data_gen import (
    GeneratorConfig,
    SyntheticPopulation,
    generate_population,
    load_config,
)
from chip_chat.harvest.blobs import LocalBlobStore

REPOSITORY = Path(__file__).resolve().parents[2]
CATALOG_FIXTURES = REPOSITORY / "catalog" / "tests" / "fixtures"
CATALOG_PREFIX = "catalog"
PACKAGED = REPOSITORY / "data-gen" / "src" / "chip_chat" / "data_gen" / "population.toml"

if str(CATALOG_FIXTURES.parent) not in sys.path:  # pragma: no cover - import path
    sys.path.insert(0, str(CATALOG_FIXTURES.parent))


@cache
def fixture_catalog() -> MenuCatalog:
    """Return the catalogue committed by issue #24."""
    return load_catalog(LocalBlobStore(CATALOG_FIXTURES), CATALOG_PREFIX)


@cache
def shipped_config() -> GeneratorConfig:
    """Return the parameters the package ships with."""
    return load_config()


SMALL_CUSTOMERS = 60
"""How many customers a test that is not about size should generate.

Every property these tests assert — reproducibility, referential integrity,
the pricing arithmetic — holds at any size, and a suite that regenerates five
hundred customers a dozen times spends a minute proving it. The full
population is generated once, by :func:`fixture_population`, and the tests
that are actually about five hundred customers use that one.
"""


@cache
def small_config() -> GeneratorConfig:
    """Return the shipped parameters, scaled down to a size tests can afford."""
    return dataclasses.replace(shipped_config(), customers=SMALL_CUSTOMERS)


@cache
def fixture_population() -> SyntheticPopulation:
    """Return the whole population, generated once for the whole suite."""
    return generate_population(fixture_catalog(), shipped_config())


@cache
def small_population() -> SyntheticPopulation:
    """Return a scaled-down population, generated once for the whole suite."""
    return generate_population(fixture_catalog(), small_config())


def personas_by_id() -> dict[str, str]:
    """Return ``demo_id`` to ``persona_id`` for the fixture population."""
    return {row.demo_id: row.persona_id for row in fixture_population().demo_visitors}


def orders_by_customer() -> dict[str, list[str]]:
    """Return ``demo_id`` to their order identifiers, oldest first."""
    grouped: dict[str, list[str]] = {}
    for order in fixture_population().orders:
        grouped.setdefault(order.demo_id, []).append(order.order_id)
    return grouped
