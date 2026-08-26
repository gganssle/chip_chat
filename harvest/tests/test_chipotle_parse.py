"""What the tables say, and what they refuse to say.

The assertions here are on real published values — a Steak Burrito really was
$13.15 at one restaurant and $11.65 at another — because a parser test with
invented numbers in it proves only that the parser is self-consistent.

Two of these tests are about restraint rather than extraction: that the
Steak Burrito does not end up described as salt, and that a restaurant whose
whole menu is free is refused rather than believed.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import chipotle_fixtures as site
import pytest

from chip_chat.harvest.blobs import InMemoryBlobStore
from chip_chat.harvest.harvester import Harvester
from chip_chat.harvest.ratelimit import PolitenessGate, RateLimiter
from chip_chat.harvest.sources.chipotle import (
    TABLES,
    ChipotleSourceError,
    MenuDataset,
    harvest_menu,
    parse_menu,
    to_jsonl,
)
from chip_chat.harvest.testing import FakeClock, FakeTransport


def build(transport: FakeTransport, blobs: InMemoryBlobStore) -> Harvester:
    """A harvester on its own gate and clock."""
    clock = FakeClock()
    return Harvester(
        blobs,
        transport,
        clock=clock,
        contact="https://example.test/contact",
        gate=PolitenessGate(RateLimiter(2.0, clock), 1),
    )


def dataset(
    blobs: InMemoryBlobStore,
    restaurants: list[str] | None = None,
    transport: FakeTransport | None = None,
) -> MenuDataset:
    """Harvest the fixture site and parse what came back."""
    harvester = build(transport if transport is not None else site.site(), blobs)
    return parse_menu(harvest_menu(harvester, restaurants or [site.REFERENCE]))


def by_id(table: Sequence[Any], key: str = "item_id") -> dict[str, dict[str, Any]]:
    """Index a table by one of its columns."""
    return {str(row[key]): row for row in site.rows(table)}


def test_items_carry_their_category_and_provenance(
    blobs: InMemoryBlobStore,
) -> None:
    items = by_id(dataset(blobs).menu_items)

    burrito = items["CMG-2"]
    assert burrito["name"] == "Steak Burrito"
    assert burrito["category"] == "Entree"
    assert burrito["item_type"] == "Burrito"
    assert burrito["primary_filling"] == "Steak"
    assert burrito["source_url"] == site.menu_url(site.REFERENCE)
    assert burrito["harvested_at"]


def test_a_modifier_is_a_menu_item_with_a_null_category(
    blobs: InMemoryBlobStore,
) -> None:
    """Black beans have an identifier, a name and a price, so they get a row."""
    items = by_id(dataset(blobs).menu_items)

    assert items["CMG-5051"]["name"] == "Black Beans"
    assert items["CMG-5051"]["category"] is None
    assert items["CMG-1002"]["category"] == "Side"


def test_every_item_has_exactly_one_price(blobs: InMemoryBlobStore) -> None:
    parsed = dataset(blobs)

    priced = {row["item_id"] for row in site.rows(parsed.item_prices)}
    assert priced == {row["item_id"] for row in site.rows(parsed.menu_items)}
    assert len(parsed.item_prices) == len(parsed.menu_items)


def test_prices_are_exact_decimals_not_floats(blobs: InMemoryBlobStore) -> None:
    prices = {row.item_id: row for row in dataset(blobs).item_prices}

    assert prices["CMG-2"].unit_price == Decimal("13.15")
    assert prices["CMG-2"].unit_delivery_price == Decimal("17.1")
    assert prices["CMG-1001"].unit_price == Decimal("2.95")


def test_a_second_restaurant_adds_prices_and_not_items(
    blobs: InMemoryBlobStore,
) -> None:
    """Per-store pricing, which is the reason prices are their own table."""
    one = dataset(blobs, [site.REFERENCE])
    both = dataset(InMemoryBlobStore(), [site.REFERENCE, site.COMPARISON])

    assert both.restaurant_ids == (679, 1200)
    assert both.reference_restaurant_id == 679
    assert len(both.menu_items) == len(one.menu_items)
    assert len(both.item_prices) == 2 * len(one.item_prices)

    burrito = {
        row.restaurant_id: row.unit_price
        for row in both.item_prices
        if row.item_id == "CMG-2"
    }
    assert burrito == {679: Decimal("13.15"), 1200: Decimal("11.65")}


def test_a_restaurant_that_prices_everything_at_zero_is_refused(
    blobs: InMemoryBlobStore,
) -> None:
    """Chipotle answers for restaurants that are not taking orders.

    What it answers is a complete menu with every price set to zero, which a
    trusting parser turns into a catalogue that quotes $0.00 with confidence.
    """
    with pytest.raises(ChipotleSourceError) as raised:
        dataset(blobs, [site.CLOSED])

    assert "not open for orders" in str(raised.value)


def test_the_slots_on_an_item_carry_their_bounds(
    blobs: InMemoryBlobStore,
) -> None:
    groups = {
        (row.item_id, row.group_name): row for row in dataset(blobs).modifier_groups
    }

    rice = groups[("CMG-2", "RiceContentGroup")]
    assert (rice.min_quantity, rice.max_quantity) == (1, 1)
    assert ("CMG-2", "BeansContentGroup") in groups


def test_modifiers_name_their_slot_and_their_allowance(
    blobs: InMemoryBlobStore,
) -> None:
    modifiers = {
        (row.item_id, row.modifier_item_id): row for row in dataset(blobs).modifiers
    }

    beans = modifiers[("CMG-2", "CMG-5051")]
    assert beans.name == "Black Beans"
    assert beans.group_name == "BeansContentGroup"
    assert beans.modifier_id == "CMG-2:CMG-5051"

    extra_chicken = modifiers[("CMG-2", "CMG-1101")]
    assert extra_chicken.modifier_type == "ExtraPortion"
    assert extra_chicken.group_name is None


def test_the_portion_vocabulary_is_a_table_not_a_sentence(
    blobs: InMemoryBlobStore,
) -> None:
    """What the vision matcher is later allowed to resolve "extra" against."""
    options = [
        row
        for row in dataset(blobs).portion_options
        if (row.item_id, row.modifier_item_id) == ("CMG-2", "CMG-5051")
    ]

    assert [row.name for row in options] == ["Light", "Extra", "Side", "Half"]
    half = next(row for row in options if row.name == "Half")
    assert half.counts_toward_customization_max == 0.5
    assert half.counts_toward_content_max == -0.5


def test_a_description_is_taken_only_when_the_ingredient_is_the_item(
    blobs: InMemoryBlobStore,
) -> None:
    """The join that keeps a burrito from being described as salt.

    The ingredient corpus links salt to the Steak Burrito, because there is
    salt in a Steak Burrito. Read as a description that is nonsense, so an
    item takes an ingredient's prose only when the ingredient is named after
    it and lists it.
    """
    items = by_id(dataset(blobs).menu_items)

    assert items["CMG-5051"]["description"]
    assert "beans" in str(items["CMG-5051"]["description"]).lower()
    assert items["CMG-2"]["description"] is None
    assert items["CMG-5001"]["description"]


def test_the_ingredient_corpus_keeps_containment_and_prose_apart(
    blobs: InMemoryBlobStore,
) -> None:
    ingredients = {row.key: row for row in dataset(blobs).ingredients}

    salt = ingredients["salt"]
    assert "CMG-2" in salt.used_in_menu_item_ids
    assert salt.description
    assert ingredients["avoc"].fun_fact


def test_the_published_taxonomy_survives_as_rows(
    blobs: InMemoryBlobStore,
) -> None:
    taxonomy = {
        (row.group_title, row.item_id): row for row in dataset(blobs).item_ingredients
    }

    assert ("proteins", "CMG-1") in taxonomy
    assert ("rice and beans", "CMG-5051") in taxonomy
    assert "bbean" in taxonomy[("rice and beans", "CMG-5051")].ingredient_keys


def test_meals_carry_the_prose_and_the_lines(blobs: InMemoryBlobStore) -> None:
    parsed = dataset(blobs)
    meals = {row.name: row for row in parsed.meals}

    build_your_own = meals["Build-Your-Own Chicken"]
    assert build_your_own.description
    assert build_your_own.calories == "520 - 1220"
    assert build_your_own.dietary_tags == ("Serves 4-6 people",)
    assert build_your_own.entree_item_id == "CMG-5374"

    lines = [row for row in parsed.meal_contents if row.meal_id == build_your_own.meal_id]
    assert lines[0].item_id == "CMG-5374"
    assert [row.position for row in lines] == list(range(len(lines)))


def test_meal_prices_are_per_restaurant_too(blobs: InMemoryBlobStore) -> None:
    parsed = dataset(blobs)

    prices = {row.meal_id: row for row in parsed.meal_prices}
    meal = next(row for row in parsed.meals if row.name == "Build-Your-Own Chicken")
    assert prices[meal.meal_id].restaurant_id == 679
    assert prices[meal.meal_id].meal_price == Decimal("59")


def test_parsing_the_same_documents_twice_produces_the_same_bytes(
    blobs: InMemoryBlobStore,
) -> None:
    """Issue #19's third acceptance criterion, checked rather than asserted."""
    harvester = build(site.site(), blobs)
    documents = harvest_menu(harvester, [site.REFERENCE, site.COMPARISON])

    first = parse_menu(documents)
    second = parse_menu(documents)

    assert first.manifest() == second.manifest()
    for name in TABLES:
        assert to_jsonl(first.table(name)) == to_jsonl(second.table(name))


def test_the_dataset_writes_one_file_per_table_plus_a_manifest(
    blobs: InMemoryBlobStore,
) -> None:
    parsed = dataset(blobs)

    written = parsed.write(blobs)

    assert set(written) == {*TABLES, "manifest"}
    for name in TABLES:
        assert blobs.read(written[name]) == to_jsonl(parsed.table(name))
    assert written["manifest"].startswith("parsed/chipotle/menu/")


def test_the_parsed_tables_land_beside_the_raw_bytes_not_inside_them(
    blobs: InMemoryBlobStore,
) -> None:
    """A parse must never be mistakable for something the site published."""
    dataset(blobs).write(blobs)

    assert not any(key.startswith("raw/") for key in blobs.keys("parsed/"))
    assert not any(key.startswith("parsed/") for key in blobs.keys("raw/"))
