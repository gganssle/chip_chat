"""Issue #25's second acceptance criterion, and the trap behind it.

"Zero orders reference an item or modifier absent from ``menu_catalog``." The
ticket is blunt about why: an order that references an item not in the
catalogue "is a bug, not a variation", and it surfaces months later as a
hallucinated menu item in a conversation. So the assertions here are over the
*whole* population rather than a sample, and they cover the modifiers as well
as the items — a real burrito with an invented salsa on it is the same failure
wearing a smaller hat.

The pricing assertions are the other half of "prices computed from the
catalogue, not invented". Every total is re-derived from ``item_prices`` at
the restaurant and channel the order names, and compared. A generator that
made prices up would pass every referential check in this file and fail these.
"""

from datetime import datetime
from decimal import Decimal

from population_fixtures import fixture_catalog, fixture_population, shipped_config

from chip_chat.catalog import ItemPrice
from chip_chat.data_gen import Channel, Order


def test_every_ordered_item_is_a_real_catalogue_item() -> None:
    """The criterion itself."""
    published = {row.item_id for row in fixture_catalog().menu_items}

    unknown = {
        line.item_id
        for line in fixture_population().order_items
        if line.item_id not in published
    }

    assert unknown == set()


def test_every_modifier_is_a_real_modifier_of_the_item_it_is_on() -> None:
    """And a real modifier of *that* item, which is the stronger claim.

    The catalogue keys a modifier ``(item_id, modifier_item_id)``, so guacamole
    on a burrito and guacamole on a bowl are different rows. Checking only that
    the identifier exists somewhere would let the generator put a modifier on
    an item Chipotle does not publish it for.
    """
    published = {row.modifier_id: row for row in fixture_catalog().modifiers}

    for line in fixture_population().order_items:
        for modifier_id in line.modifiers:
            assert modifier_id in published, modifier_id
            assert published[modifier_id].item_id == line.item_id


def test_a_line_never_carries_the_same_modifier_twice() -> None:
    """Two of one topping is not a choice the published menu offers."""
    for line in fixture_population().order_items:
        assert len(set(line.modifiers)) == len(line.modifiers)
        assert list(line.modifiers) == sorted(line.modifiers)


def test_every_order_happens_at_a_real_store() -> None:
    """A store the locator harvest carried, and one on the roster."""
    population = fixture_population()
    roster = {order.store_id for order in population.orders}
    published = {row.store_id for row in fixture_catalog().stores}

    assert roster <= published
    assert len(roster) <= shipped_config().stores


def test_every_row_hangs_off_a_customer_that_exists() -> None:
    """orders, order_items and the ledger all key back to demo_visitors."""
    population = fixture_population()
    customers = {row.demo_id for row in population.demo_visitors}
    orders = {row.order_id for row in population.orders}

    assert {row.demo_id for row in population.orders} <= customers
    assert {row.demo_id for row in population.loyalty_ledger} <= customers
    assert {row.order_id for row in population.order_items} <= orders
    assert {
        row.order_id for row in population.loyalty_ledger if row.order_id is not None
    } <= orders


def test_every_customer_is_a_persona_that_exists() -> None:
    """The archetype roster is closed; nobody is assigned an invented one."""
    population = fixture_population()
    archetypes = {row.persona_id for row in population.personas}

    assert {row.persona_id for row in population.demo_visitors} <= archetypes
    assert {row.persona_id for row in population.demo_visitors} == archetypes


def test_identifiers_are_unique() -> None:
    """A duplicated key is a join that silently doubles a customer's history."""
    population = fixture_population()

    assert len({row.demo_id for row in population.demo_visitors}) == len(
        population.demo_visitors
    )
    assert len({row.order_id for row in population.orders}) == len(population.orders)
    assert len({row.entry_id for row in population.loyalty_ledger}) == len(
        population.loyalty_ledger
    )
    lines = {(row.order_id, row.line_number) for row in population.order_items}
    assert len(lines) == len(population.order_items)


def test_every_order_has_at_least_one_line() -> None:
    """An order with no lines is a total computed from nothing."""
    population = fixture_population()
    lines: dict[str, int] = {}
    for line in population.order_items:
        lines[line.order_id] = lines.get(line.order_id, 0) + 1

    assert {order.order_id for order in population.orders} == set(lines)
    assert all(count >= 1 for count in lines.values())


def test_every_total_is_the_published_prices_added_up() -> None:
    """ "Prices computed from the catalogue, not invented", re-derived."""
    catalog, population = fixture_catalog(), fixture_population()
    prices = {(row.restaurant_id, row.item_id): row for row in catalog.item_prices}
    modifiers = {row.modifier_id: row for row in catalog.modifiers}
    orders = {row.order_id: row for row in population.orders}
    totals: dict[str, Decimal] = {}

    for line in population.order_items:
        order = orders[line.order_id]
        published = prices[(order.priced_restaurant_id, line.item_id)]
        unit = (
            published.unit_delivery_price
            if order.channel is Channel.DELIVERY
            else published.unit_price
        )
        assert line.unit_price == unit.quantize(Decimal("0.01"))
        extra = sum(
            (
                _price(prices, order, modifiers[modifier_id].modifier_item_id)
                for modifier_id in line.modifiers
            ),
            start=Decimal("0"),
        )
        assert line.line_total == (line.qty * (unit + extra)).quantize(Decimal("0.01"))
        totals[order.order_id] = (
            totals.get(order.order_id, Decimal("0")) + line.line_total
        )

    for order in population.orders:
        assert order.total == totals[order.order_id]
        assert order.total > Decimal("0")


def _price(
    prices: dict[tuple[int, str], ItemPrice], order: Order, item_id: str
) -> Decimal:
    """Return one published price, or zero where the catalogue publishes none."""
    row = prices.get((order.priced_restaurant_id, item_id))
    if row is None:
        return Decimal("0")
    if order.channel is Channel.DELIVERY:
        return row.unit_delivery_price
    return row.unit_price


def test_a_delivery_order_only_contains_things_sold_for_delivery() -> None:
    """Availability is published per channel, and is not the generator's to assume."""
    catalog, population = fixture_catalog(), fixture_population()
    reference = catalog.reference_restaurant_id
    prices = {(row.restaurant_id, row.item_id): row for row in catalog.item_prices}
    orders = {row.order_id: row for row in population.orders}

    for line in population.order_items:
        published = prices[(reference, line.item_id)]
        assert published.is_available
        if orders[line.order_id].channel is Channel.DELIVERY:
            assert published.eligible_for_delivery


def test_a_store_quotes_prices_someone_published() -> None:
    """Either its own restaurant's, or the catalogue's reference restaurant's."""
    catalog, population = fixture_catalog(), fixture_population()
    priced = set(catalog.restaurant_ids)

    for order in population.orders:
        if order.store_id in priced:
            assert order.priced_restaurant_id == order.store_id
        else:
            assert order.priced_restaurant_id == catalog.reference_restaurant_id


def test_no_order_falls_outside_the_generated_window() -> None:
    """Eighteen months means eighteen months."""
    population = fixture_population()

    for order in population.orders:
        assert population.window_starts_at <= order.placed_at <= population.window_ends_at


def test_every_customer_row_is_consistent_with_their_orders() -> None:
    """``last_seen`` is their last order, and never before they were created."""
    population = fixture_population()
    latest: dict[str, datetime] = {}
    for order in population.orders:
        current = latest.get(order.demo_id)
        if current is None or order.placed_at > current:
            latest[order.demo_id] = order.placed_at

    for visitor in population.demo_visitors:
        assert visitor.created_at <= visitor.last_seen
        assert visitor.thread_id is None
        if visitor.demo_id in latest:
            assert visitor.last_seen == latest[visitor.demo_id]
        else:
            assert visitor.last_seen == visitor.created_at
