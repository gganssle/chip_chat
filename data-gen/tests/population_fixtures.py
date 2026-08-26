"""One catalogue, one rewards programme, and one population, shared by all.

The catalogue is the fixture committed by issue #24, which exists for exactly
this: "so that issue #25's generator and issue #54's matcher have something to
resolve against on a laptop with no network". Reading it here rather than
rebuilding it from the harvest fixtures means these tests fail when the
committed fixture and the loader disagree, which is a thing worth finding out.

It is a *small* catalogue — two entrees, five modifiers each, thirty stores —
so the tests below assert properties rather than quantities wherever the
quantity would be an artefact of its size. That is also how issue #28 proves
the population is not thin against it: every check in
:mod:`chip_chat.data_gen.texture` is a coverage, a ratio, a share or an effect
size measured against what this catalogue makes possible, so the same bounds
mean the same thing here and against a real harvest.

The published rewards terms come from the harvest tests' fixture site, written
to a blob store and read back through
:func:`~chip_chat.data_gen.rewards.load_rewards_terms` — so every test that
touches the ledger exercises the loader as well as the arithmetic, and a
policy table that stopped serialising the way the loader reads it fails here
rather than in production.

The population is generated from the *shipped* ``population.toml``, not from a
test-local copy. A config the tests never load is a config that can rot.
"""

import dataclasses
import sys
from functools import cache
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
CATALOG_FIXTURES = REPOSITORY / "catalog" / "tests" / "fixtures"
CATALOG_PREFIX = "catalog"
PACKAGED = REPOSITORY / "data-gen" / "src" / "chip_chat" / "data_gen" / "population.toml"
POLICY_STORES = 30
"""How many stores the fixture policy harvest reads. Above issue #21's floor,
which the parser enforces, and enough that the run stays quick."""

if str(CATALOG_FIXTURES.parent) not in sys.path:  # pragma: no cover - import path
    sys.path.insert(0, str(CATALOG_FIXTURES.parent))

import catalog_fixtures  # noqa: E402

from chip_chat.catalog import MenuCatalog, load_catalog  # noqa: E402
from chip_chat.data_gen import (  # noqa: E402
    GeneratorConfig,
    RewardsTerms,
    SyntheticPopulation,
    generate_population,
    load_config,
    load_rewards_terms,
)
from chip_chat.harvest.blobs import (  # noqa: E402
    InMemoryBlobStore,
    LocalBlobStore,
)
from chip_chat.harvest.sources.chipotle import (  # noqa: E402
    PolicyDataset,
    harvest_policy,
    parse_policy,
)


@cache
def fixture_catalog() -> MenuCatalog:
    """Return the catalogue committed by issue #24."""
    return load_catalog(LocalBlobStore(CATALOG_FIXTURES), CATALOG_PREFIX)


@cache
def fixture_policy() -> PolicyDataset:
    """Return the parsed policy harvest of issue #21, from the fixture site.

    Built from the harvest tests' fixture site rather than from a second
    recording, for the reason ``catalog/tests/catalog_fixtures.py`` gives about
    the catalogue: a second copy is a second thing to keep true, and the one
    that rots would be this one.
    """
    harvester = catalog_fixtures.harvester(catalog_fixtures.chipotle.site())
    return parse_policy(harvest_policy(harvester, store_count=POLICY_STORES))


@cache
def fixture_terms() -> RewardsTerms:
    """Return the published rewards programme, read back from a written harvest.

    Written and then read rather than constructed, so that every test touching
    the ledger exercises :func:`~chip_chat.data_gen.rewards.load_rewards_terms`
    as well as the arithmetic it hands over.
    """
    blobs = InMemoryBlobStore()
    fixture_policy().write(blobs)
    return load_rewards_terms(blobs)


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
    return generate_population(fixture_catalog(), fixture_terms(), shipped_config())


@cache
def small_population() -> SyntheticPopulation:
    """Return a scaled-down population, generated once for the whole suite."""
    return generate_population(fixture_catalog(), fixture_terms(), small_config())


def personas_by_id() -> dict[str, str]:
    """Return ``demo_id`` to ``persona_id`` for the fixture population."""
    return {row.demo_id: row.persona_id for row in fixture_population().demo_visitors}


def orders_by_customer() -> dict[str, list[str]]:
    """Return ``demo_id`` to their order identifiers, oldest first."""
    grouped: dict[str, list[str]] = {}
    for order in fixture_population().orders:
        grouped.setdefault(order.demo_id, []).append(order.order_id)
    return grouped
