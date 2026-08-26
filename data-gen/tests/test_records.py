"""That the tables serialise the way the lakehouse will read them.

Issue #33 ingests this population with Auto Loader, so the shape on disk is
the interface. These tests hold the two things that shape has to promise: the
manifest describes what was actually written, and the money survives being
written down.
"""

import json

import pytest
from population_fixtures import small_population

from chip_chat.data_gen import DEFAULT_PREFIX, TABLES, SyntheticPopulation
from chip_chat.harvest.blobs import InMemoryBlobStore


def test_every_table_is_written_under_the_prefix() -> None:
    blobs = InMemoryBlobStore()

    written = small_population().write(blobs)

    assert set(written) == {*TABLES, "manifest"}
    for name in TABLES:
        assert written[name] == f"{DEFAULT_PREFIX}/{name}.jsonl"
    assert written["manifest"] == f"{DEFAULT_PREFIX}/manifest.json"


def test_the_manifest_counts_what_was_written() -> None:
    """A manifest that describes a different population is worse than none."""
    blobs = InMemoryBlobStore()
    population = small_population()

    population.write(blobs)

    manifest = json.loads(blobs.read(f"{DEFAULT_PREFIX}/manifest.json") or b"{}")
    for name in TABLES:
        rows = (blobs.read(f"{DEFAULT_PREFIX}/{name}.jsonl") or b"").splitlines()
        assert manifest["tables"][name]["rows"] == len(rows)
        assert manifest["tables"][name]["rows"] == len(population.table(name))


def test_money_is_written_as_a_decimal_string_and_not_a_float() -> None:
    """A total that round-tripped through a float would be quietly wrong."""
    blobs = InMemoryBlobStore()
    small_population().write(blobs)

    first = json.loads(
        (blobs.read(f"{DEFAULT_PREFIX}/orders.jsonl") or b"").splitlines()[0]
    )
    line = json.loads(
        (blobs.read(f"{DEFAULT_PREFIX}/order_items.jsonl") or b"").splitlines()[0]
    )

    assert isinstance(first["total"], str)
    assert first["total"].count(".") == 1
    assert isinstance(line["unit_price"], str)
    assert isinstance(line["line_total"], str)


def test_a_timestamp_is_written_with_its_zone() -> None:
    """A naive timestamp in a lakehouse is a timestamp in someone else's day."""
    blobs = InMemoryBlobStore()
    small_population().write(blobs)

    first = json.loads(
        (blobs.read(f"{DEFAULT_PREFIX}/orders.jsonl") or b"").splitlines()[0]
    )

    assert first["placed_at"].endswith("+00:00")


def test_a_table_is_reachable_by_name_and_an_invented_one_is_not() -> None:
    population = small_population()

    assert population.table("orders") is population.orders
    assert [name for name, _ in population.tables()] == list(TABLES)
    with pytest.raises(KeyError, match="no such table"):
        population.table("customers")


def test_the_population_is_frozen() -> None:
    """Rows that can be edited after the digest is taken are rows that will be."""
    population = small_population()

    with pytest.raises(AttributeError):
        population.orders[0].status = "REFUNDED"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        population.demo_visitors[0].demo_id = "demo-9999"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        population.personas[0].seed_points = 1  # type: ignore[misc]


def test_the_population_records_which_catalogue_composed_it() -> None:
    """A gold mart that looks wrong is traced back to an input, not argued about."""
    population: SyntheticPopulation = small_population()

    assert len(population.catalog_content_version) == 64
    assert population.catalog_content_version != population.version()
