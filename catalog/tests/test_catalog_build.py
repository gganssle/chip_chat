"""What the catalogue says, and what it refuses to say.

The assertions are on real published values — a Steak Burrito's published
figure really is 150 calories of steak, and Napkins & Utensils really have no
nutrition data at all — because a consolidation test with invented numbers in
it proves only that the consolidation is self-consistent.
"""

from dataclasses import replace
from decimal import Decimal

import pytest
from catalog_fixtures import catalog as build
from catalog_fixtures import chipotle, datasets, fixture_catalog

from chip_chat.catalog import (
    TABLES,
    AllergenDisclosure,
    MenuCatalog,
    MissingSourceError,
    build_catalog,
)
from chip_chat.harvest.sources.chipotle import AllergenStatus


def item(catalog: MenuCatalog, item_id: str):
    """One item, or a failure that names it rather than an AttributeError."""
    found = catalog.item(item_id)
    assert found is not None, f"{item_id} is not in the catalogue"
    return found


def test_every_table_is_populated() -> None:
    """A catalogue with an empty table is a join that silently missed."""
    catalog = fixture_catalog()
    counts = {name: len(rows) for name, rows in catalog.tables()}
    assert set(counts) == set(TABLES)
    assert all(count > 0 for count in counts.values()), counts


def test_identity_comes_from_the_menu() -> None:
    """The published item identifier, name and category are carried across."""
    catalog = fixture_catalog()
    burrito = item(catalog, "CMG-2")
    assert burrito.name == "Steak Burrito"
    assert burrito.category == "Entree"
    assert burrito.item_type == "Burrito"
    assert burrito.primary_filling == "Steak"


def test_a_modifier_only_item_has_no_category() -> None:
    """``category is None`` is the test for "cannot be ordered on its own"."""
    catalog = fixture_catalog()
    assert item(catalog, "CMG-5051").name == "Black Beans"
    assert item(catalog, "CMG-5051").category is None
    assert item(catalog, "CMG-1002").category == "Side"


def test_a_composed_item_says_its_calories_are_a_component() -> None:
    """CMG-2 is a Steak Burrito on the menu and 150 calories of steak here.

    The figure is published for the steak, not for the assembled burrito, and
    a row that did not say so is the easiest confidently-wrong number in this
    dataset.
    """
    catalog = fixture_catalog()
    burrito = item(catalog, "CMG-2")
    assert burrito.calories == Decimal("150")
    assert burrito.is_composed is True

    chips = item(catalog, "CMG-1002")
    assert chips.calories == Decimal("540")
    assert chips.is_composed is False


def test_an_unpublished_figure_is_null_and_not_zero() -> None:
    """Napkins have no calories published; that is not zero calories."""
    catalog = fixture_catalog()
    napkins = item(catalog, "CMG-6110")
    assert napkins.calories is None
    assert napkins.nutrition_source_url is None


def test_a_merged_row_cites_both_documents() -> None:
    """Identity and nutrition come from different endpoints, and both survive."""
    catalog = fixture_catalog()
    cheese = item(catalog, "CMG-5252")
    assert cheese.source_url == chipotle.menu_url(chipotle.REFERENCE)
    assert cheese.nutrition_source_url == chipotle.NUTRITION_URL
    assert cheese.allergen_source_url == chipotle.NUTRITION_URL


def test_prices_are_per_restaurant_and_carry_their_store() -> None:
    """There is no ``base_price`` column; a price has a restaurant on it."""
    catalog = fixture_catalog()
    prices = {(row.restaurant_id, row.item_id): row for row in catalog.item_prices}
    burrito = prices[(679, "CMG-2")]
    assert burrito.unit_price == Decimal("13.15")
    assert burrito.unit_delivery_price == Decimal("17.10")
    assert burrito.source_url == chipotle.menu_url(chipotle.REFERENCE)


def test_two_restaurants_disagree_about_a_price() -> None:
    """The whole reason money is not a column on ``menu_items``."""
    catalog = build(restaurants=[chipotle.REFERENCE, chipotle.COMPARISON])
    quoted = {
        row.restaurant_id: row.unit_price
        for row in catalog.item_prices
        if row.item_id == "CMG-2"
    }
    assert quoted[679] == Decimal("13.15")
    assert quoted[1200] == Decimal("11.65")
    assert catalog.reference_restaurant_id == 679


def test_a_modifier_is_identified_by_the_pair() -> None:
    """The same ingredient on a different item is a different modifier."""
    catalog = fixture_catalog()
    by_id = {row.modifier_id: row for row in catalog.modifiers}
    beans = by_id["CMG-2:CMG-5051"]
    assert beans.item_id == "CMG-2"
    assert beans.modifier_item_id == "CMG-5051"
    assert beans.name == "Black Beans"
    assert "CMG-101:CMG-5051" in by_id


def test_a_modifier_takes_its_own_calories_not_the_items() -> None:
    """Cheese on a burrito is 110 calories, not the burrito's 150."""
    catalog = fixture_catalog()
    by_id = {row.modifier_id: row for row in catalog.modifiers}
    assert by_id["CMG-2:CMG-5252"].delta_calories == Decimal("110")
    assert item(catalog, "CMG-2").calories == Decimal("150")


def test_a_modifier_carries_the_portions_it_accepts() -> None:
    """ "Extra cheese" resolves to a published portion word or is refused."""
    catalog = fixture_catalog()
    by_id = {row.modifier_id: row for row in catalog.modifiers}
    assert by_id["CMG-2:CMG-5252"].portion_options == ("Light", "Extra")
    assert by_id["CMG-2:CMG-5001"].portion_options == (
        "Light",
        "Extra",
        "Side",
        "Half",
    )


def test_a_modifier_carries_its_slots_bounds() -> None:
    """How many rices a burrito takes is a published number, and it is one."""
    catalog = fixture_catalog()
    by_id = {row.modifier_id: row for row in catalog.modifiers}
    rice = by_id["CMG-2:CMG-5001"]
    assert (rice.min_quantity, rice.max_quantity) == (1, 1)


def test_a_store_joins_its_name_to_its_address() -> None:
    """Two documents, two provenances, one row a visitor could be shown."""
    catalog = fixture_catalog()
    stores = {row.store_id: row for row in catalog.stores}
    reference = stores[679]
    assert reference.name == "Lakewood Mall"
    assert reference.city == "Lakewood"
    assert reference.region == "CA"
    assert reference.source_url != reference.profile_source_url


def test_every_store_publishes_seven_days() -> None:
    """A day nobody published hours for is an entry saying so."""
    catalog = fixture_catalog()
    for store in catalog.stores:
        assert len(store.hours) == 7
        assert next(entry.day_of_week for entry in store.hours) == "Monday"
    unpublished = [
        entry
        for store in catalog.stores
        for entry in store.hours
        if not entry.is_published
    ]
    assert unpublished, "the fixture site has a store with a day it never published"
    assert all(entry.opens is None for entry in unpublished)


def test_the_reference_restaurant_is_a_store_you_can_name() -> None:
    """A price without a place is a price that cannot be cited."""
    catalog = fixture_catalog()
    priced = {row.restaurant_id for row in catalog.item_prices}
    located = {row.store_id for row in catalog.stores}
    assert catalog.reference_restaurant_id in priced
    assert catalog.reference_restaurant_id in located


def test_prices_are_confined_to_catalogue_items() -> None:
    """Nothing may be priced that the catalogue does not have a row for."""
    catalog = fixture_catalog()
    known = {row.item_id for row in catalog.menu_items}
    assert {row.item_id for row in catalog.item_prices} <= known
    assert {row.modifier_item_id for row in catalog.modifiers} <= known
    assert {row.item_id for row in catalog.item_allergens} <= known


def test_a_menu_with_no_items_is_refused() -> None:
    """An empty catalogue is a failed harvest, not a small menu."""
    menu, nutrition, policy = datasets()
    empty = replace(menu, menu_items=())
    with pytest.raises(MissingSourceError, match="no items"):
        build_catalog(empty, nutrition, policy)


def test_a_renamed_calorie_nutrient_stops_the_build() -> None:
    """Better a build that fails than a catalogue with no calories in it."""
    menu, nutrition, policy = datasets()
    without = replace(
        nutrition,
        nutrients=tuple(row for row in nutrition.nutrients if row.nutrient_key != "tcal"),
    )
    with pytest.raises(MissingSourceError, match="tcal"):
        build_catalog(menu, without, policy)


def test_an_item_the_nutrition_harvest_never_saw_stops_the_build() -> None:
    """Two harvests over different restaurants is a join that quietly missed.

    It would fail in the safe direction — the item would read as
    ``NOT_PUBLISHED`` — but ``item_allergens`` would be short, and "we asked
    and nothing is published" is not "we never asked".
    """
    menu, nutrition, policy = datasets()
    silent = replace(
        nutrition,
        item_allergens=tuple(
            row for row in nutrition.item_allergens if row.item_id != "CMG-2"
        ),
    )
    with pytest.raises(MissingSourceError, match="CMG-2"):
        build_catalog(menu, silent, policy)


def test_the_published_allergen_vocabulary_is_carried() -> None:
    """The codes are the chart's own, and so are the labels — or the nulls."""
    catalog = fixture_catalog()
    codes = {row.allergen_code: row for row in catalog.allergens}
    assert "dair" in codes
    assert codes["dair"].name is not None
    assert set(codes) == {row.allergen_code for row in catalog.item_allergens}


def test_the_caveats_travel_with_the_data() -> None:
    """A table that answers allergen questions without them overclaims."""
    catalog = fixture_catalog()
    assert catalog.caveats
    assert any(
        "cross-contact" in row.text.lower() or "contact" in row.text.lower()
        for row in catalog.caveats
    )
    assert all(row.source_url for row in catalog.caveats)


def test_an_item_nobody_published_allergens_for_says_so() -> None:
    """Napkins reach ``NOT_PUBLISHED`` rather than an empty and reassuring set."""
    catalog = fixture_catalog()
    napkins = item(catalog, "CMG-6110")
    assert napkins.allergens == ()
    assert napkins.allergen_disclosure is AllergenDisclosure.NOT_PUBLISHED
    assert napkins.allergen_status("dair") is AllergenStatus.NOT_PUBLISHED
