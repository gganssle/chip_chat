"""That only real, available, correctly-priced things reach a basket.

:class:`~chip_chat.data_gen.catalogue.OrderableMenu` is the single source of
every identifier in this package, which is what makes "no order references an
item absent from the catalogue" a property rather than a test result. These
are its edges: what it refuses to offer, what it refuses to price, and what it
refuses to do at all.
"""

import dataclasses
from decimal import Decimal

import pytest
from population_fixtures import fixture_catalog, shipped_config

from chip_chat.data_gen import Channel, OrderableMenu
from chip_chat.data_gen.errors import ThinCatalogError


def menu() -> OrderableMenu:
    """The orderable view of the fixture catalogue."""
    return OrderableMenu(fixture_catalog(), shipped_config().catalogue)


def test_only_entrees_are_entrees() -> None:
    """Guacamole is a published menu item and is not something you order alone."""
    shape = shipped_config().catalogue
    entrees = menu().entrees(Channel.IN_STORE)

    assert entrees
    for buildable in entrees:
        assert buildable.item.category == shape.entree_category


def test_the_things_that_are_not_food_are_not_orderable() -> None:
    """Napkins and utensils are published, orderable in principle, and noise."""
    orderable = {
        item.item_id
        for channel in Channel
        for item in (*menu().sides(channel), *menu().drinks(channel))
    } | {
        buildable.item.item_id
        for channel in Channel
        for buildable in menu().entrees(channel)
    }
    excluded = {
        row.item_id
        for row in fixture_catalog().menu_items
        if row.category in shipped_config().catalogue.excluded_categories
    }

    assert excluded
    assert orderable & excluded == set()


def test_a_modifier_is_grouped_into_the_slot_the_catalogue_published_it_in() -> None:
    """Rice and beans are one-of; toppings are any-of."""
    shape = shipped_config().catalogue
    buildable = menu().entrees(Channel.IN_STORE)[0]

    assert [slot.slot for slot in buildable.required] == list(
        dict.fromkeys(
            slot
            for slot in shape.required_slots
            if any(row.slot == slot for row in buildable.required)
        )
    )
    for slot in buildable.required:
        assert slot.slot in shape.required_slots
        assert slot.choices
    for slot in buildable.optional:
        assert slot.slot in shape.optional_slots


def test_a_published_extra_portion_is_an_extra_and_not_a_slot() -> None:
    """``Extra Chicken`` is a real modifier, in no RFC-001 section 07 slot."""
    shape = shipped_config().catalogue
    buildable = menu().entrees(Channel.IN_STORE)[0]

    assert buildable.extras
    for modifier in buildable.extras:
        assert modifier.modifier_type == shape.extra_portion_modifier_type
        assert modifier.slot is None


def test_an_unavailable_item_is_not_offered() -> None:
    """Availability is published, and is not the generator's to assume."""
    catalog = fixture_catalog()
    entree = menu().entrees(Channel.IN_STORE)[0].item.item_id
    withdrawn = dataclasses.replace(
        catalog,
        item_prices=tuple(
            dataclasses.replace(row, is_available=False) if row.item_id == entree else row
            for row in catalog.item_prices
        ),
    )

    offered = OrderableMenu(withdrawn, shipped_config().catalogue)

    assert entree not in {row.item.item_id for row in offered.entrees(Channel.IN_STORE)}


def test_an_item_not_sold_for_delivery_is_not_offered_for_delivery() -> None:
    """And is still offered at the counter, because that is what is published."""
    catalog = fixture_catalog()
    entree = menu().entrees(Channel.IN_STORE)[0].item.item_id
    counter_only = dataclasses.replace(
        catalog,
        item_prices=tuple(
            dataclasses.replace(row, eligible_for_delivery=False)
            if row.item_id == entree
            else row
            for row in catalog.item_prices
        ),
    )

    offered = OrderableMenu(counter_only, shipped_config().catalogue)

    assert offered.sellable(entree, Channel.IN_STORE)
    assert not offered.sellable(entree, Channel.DELIVERY)


def test_delivery_is_priced_at_the_published_delivery_price() -> None:
    """Two published prices, and the order says which one it used."""
    catalog = fixture_catalog()
    priced = menu()
    row = catalog.item_prices[0]

    assert (
        priced.price(row.restaurant_id, row.item_id, Channel.IN_STORE) == row.unit_price
    )
    assert (
        priced.price(row.restaurant_id, row.item_id, Channel.DELIVERY)
        == row.unit_delivery_price
    )


def test_an_unpriced_item_costs_nothing_rather_than_something_invented() -> None:
    """A modifier with no published price is included, not guessed at."""
    assert menu().price(
        fixture_catalog().reference_restaurant_id, "CMG-NOT-PUBLISHED", Channel.IN_STORE
    ) == Decimal("0")


def test_a_store_the_harvest_did_not_price_quotes_the_reference_restaurant() -> None:
    """Chipotle publishes a menu per restaurant; the harvest priced one."""
    catalog = fixture_catalog()
    priced = menu()
    unpriced = next(
        store.store_id
        for store in catalog.stores
        if store.store_id not in catalog.restaurant_ids
    )

    assert (
        priced.pricing(catalog.reference_restaurant_id) == catalog.reference_restaurant_id
    )
    assert priced.pricing(unpriced) == catalog.reference_restaurant_id


def test_the_store_roster_always_contains_the_priced_restaurant() -> None:
    """Otherwise no order in the population is priced at its own store."""
    catalog = fixture_catalog()
    roster = menu().stores(catalog.stores, wanted=3)

    assert len(roster) == 3
    assert catalog.reference_restaurant_id in {store.store_id for store in roster}
    assert [store.store_id for store in roster] == sorted(
        store.store_id for store in roster
    )


def test_a_catalogue_with_nothing_orderable_in_it_is_refused() -> None:
    """Refusing is the only honest answer; the alternative is inventing food."""
    empty = dataclasses.replace(fixture_catalog(), menu_items=(), item_prices=())

    with pytest.raises(ThinCatalogError, match="nothing to compose an order from"):
        OrderableMenu(empty, shipped_config().catalogue)


def test_a_catalogue_with_no_stores_in_it_is_refused() -> None:
    """An order has to happen somewhere."""
    with pytest.raises(ThinCatalogError, match="carries no stores"):
        menu().stores((), wanted=30)


def test_a_modifier_can_be_looked_up_and_an_invented_one_cannot() -> None:
    """The lookup is reachable only by a caller that read an identifier."""
    real = fixture_catalog().modifiers[0]

    assert menu().modifier(real.modifier_id) == real
    with pytest.raises(KeyError):
        menu().modifier("CMG-101:CMG-INVENTED")
