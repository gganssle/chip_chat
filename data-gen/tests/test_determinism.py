"""Issue #25's first acceptance criterion: same seed, same population.

"Byte for byte" is the phrase in the ticket and it is the phrase asserted
here — not "the same number of orders", not "the same totals", but the same
serialised bytes for every table, which is the only version of the claim that
the whole downstream lakehouse can be rebuilt on.

The second test is the one that gives the first its teeth: a different seed
has to produce a *different* population. A generator that ignored its seed
would pass the first test perfectly.
"""

import dataclasses

from population_fixtures import fixture_catalog, fixture_terms, small_config

from chip_chat.data_gen import SyntheticPopulation, generate_population
from chip_chat.harvest.blobs import InMemoryBlobStore


def written(population: SyntheticPopulation) -> dict[str, bytes]:
    """Serialise every table of a population into a mapping of key to bytes."""
    blobs = InMemoryBlobStore()
    population.write(blobs, "accounts")
    return {key: blobs.read(key) or b"" for key in blobs.keys("accounts")}


def test_the_same_seed_produces_the_same_bytes() -> None:
    """The criterion itself, asserted on the serialised form."""
    catalog, terms, config = fixture_catalog(), fixture_terms(), small_config()

    first = generate_population(catalog, terms, config)
    second = generate_population(catalog, terms, config)

    assert written(first) == written(second)
    assert first.version() == second.version()
    assert first.manifest() == second.manifest()


def test_the_same_seed_produces_the_same_rows() -> None:
    """And on the records, so a serialisation bug cannot hide a generator bug."""
    catalog, terms, config = fixture_catalog(), fixture_terms(), small_config()

    first = generate_population(catalog, terms, config)
    second = generate_population(catalog, terms, config)

    assert first == second


def test_a_different_seed_produces_a_different_population() -> None:
    """Otherwise the test above is asserting that the generator is a constant."""
    catalog, terms, config = fixture_catalog(), fixture_terms(), small_config()

    first = generate_population(catalog, terms, config)
    second = generate_population(
        catalog, terms, dataclasses.replace(config, seed=config.seed + 1)
    )

    assert first.version() != second.version()
    assert first.orders != second.orders
    assert first.demo_visitors != second.demo_visitors


def test_a_customer_is_not_disturbed_by_the_customers_before_them() -> None:
    """Streams are addressed, not shared.

    Every draw is a pure function of the seed and of what it is drawn for, so
    a customer's history does not depend on how many numbers the customers
    generated before them happened to consume. That is what makes the config
    retunable: change ``toppings_max`` and the population changes because the
    parameter changed, not because everyone's stream slid along by one.
    """
    catalog, terms, config = fixture_catalog(), fixture_terms(), small_config()
    population = generate_population(catalog, terms, config)
    orders = {order.order_id: order for order in population.orders}

    again = generate_population(catalog, terms, config)

    for order in again.orders:
        assert orders[order.order_id] == order


def test_the_manifest_names_every_one_of_its_inputs() -> None:
    """A gold mart that looks wrong is traced back, not argued about.

    Three inputs, three names: the seed, the catalogue the orders were composed
    from, and the published rewards programme the ledger was computed under. A
    balance nobody can explain should lead to the terms it was earned under.
    """
    catalog, terms, config = fixture_catalog(), fixture_terms(), small_config()

    manifest = generate_population(catalog, terms, config).manifest()

    assert manifest["seed"] == config.seed
    assert manifest["catalog_content_version"] == catalog.content_version()
    assert manifest["rewards_content_version"] == terms.content_version()
    assert manifest["population_version"] != catalog.content_version()


def test_a_different_rewards_programme_produces_a_different_population() -> None:
    """The published terms are an input, and an input that changes shows.

    Chipotle doubling the earn rate must move ``population_version``, or the
    digest is not describing the thing the ledger was computed from.
    """
    catalog, terms, config = fixture_catalog(), fixture_terms(), small_config()
    doubled = dataclasses.replace(terms, points_per_dollar=terms.points_per_dollar * 2)

    first = generate_population(catalog, terms, config)
    second = generate_population(catalog, doubled, config)

    assert first.version() != second.version()
    assert first.orders == second.orders
    assert first.loyalty_ledger != second.loyalty_ledger


def test_the_population_version_moves_when_a_row_does() -> None:
    """A digest that did not notice an edit would not be worth recording."""
    population = generate_population(fixture_catalog(), fixture_terms(), small_config())
    edited = dataclasses.replace(
        population,
        orders=(
            dataclasses.replace(population.orders[0], status="REFUNDED"),
            *population.orders[1:],
        ),
    )

    assert edited.version() != population.version()
