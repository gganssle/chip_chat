"""That points are earned on real totals and spent at a stated cost.

Issue #27 reconciles this ledger against Chipotle's published rewards terms.
Everything here is arranged so that reconciliation is a join: the arithmetic
is config, and every entry names the order it moved on. What these tests
assert is that the ledger is internally honest — nobody spends a balance they
never had, a refunded order earns nothing, and the entries are in time order.
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

from population_fixtures import shipped_config

from chip_chat.data_gen import Channel, Order
from chip_chat.data_gen.loyalty import ledger_for
from chip_chat.data_gen.rng import substream

START = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)


def an_order(index: int, total: str, status: str = "COMPLETED") -> Order:
    """Return one order, at a stated total and status."""
    return Order(
        order_id=f"ord-{index:07d}",
        demo_id="demo-0001",
        store_id=679,
        placed_at=START + timedelta(days=index),
        status=status,
        total=Decimal(total),
        channel=Channel.IN_STORE,
        priced_restaurant_id=679,
    )


def ledger(orders, seed_points: int = 0, **overrides):
    """Build one customer's ledger over ``orders``."""
    config = shipped_config()
    loyalty = dataclasses.replace(config.loyalty, **overrides)
    return ledger_for(
        substream(1, "loyalty"),
        "demo-0001",
        seed_points,
        START,
        orders,
        loyalty,
        config.orders,
        count(1),
    )


def test_an_opening_balance_is_an_entry_and_not_a_column() -> None:
    """A balance that is not in the ledger is a balance nothing can reconcile."""
    entries = ledger([], seed_points=250)

    assert len(entries) == 1
    assert entries[0].delta == 250
    assert entries[0].reason == shipped_config().loyalty.seed_reason
    assert entries[0].order_id is None
    assert entries[0].created_at == START


def test_an_archetype_with_no_opening_balance_gets_no_entry() -> None:
    assert ledger([], seed_points=0) == ()


def test_points_are_earned_at_the_configured_rate() -> None:
    entries = ledger([an_order(1, "12.40")], redemption_probability=0.0)

    assert [entry.delta for entry in entries] == [124]
    assert entries[0].order_id == "ord-0000001"
    assert entries[0].created_at == START + timedelta(days=1)


def test_a_refunded_order_is_history_and_earns_nothing() -> None:
    """It stays in ``orders``; it just does not move any points."""
    entries = ledger(
        [an_order(1, "12.40", status="REFUNDED"), an_order(2, "9.00")],
        redemption_probability=0.0,
    )

    assert [entry.order_id for entry in entries] == ["ord-0000002"]


def test_a_redemption_costs_what_the_config_says_and_never_overdraws() -> None:
    orders = [an_order(index, "20.00") for index in range(1, 20)]

    entries = ledger(orders, redemption_probability=1.0)

    balance = 0
    for entry in entries:
        balance += entry.delta
        assert balance >= 0
    threshold = shipped_config().loyalty.redemption_threshold
    redemptions = [entry for entry in entries if entry.delta < 0]
    assert redemptions
    assert all(entry.delta == -threshold for entry in redemptions)
    assert all(entry.order_id is not None for entry in redemptions)


def test_nobody_redeems_who_never_reaches_the_threshold() -> None:
    entries = ledger([an_order(1, "1.00")], redemption_probability=1.0)

    assert all(entry.delta > 0 for entry in entries)


def test_the_ledger_is_in_time_order() -> None:
    """The running balance is only meaningful if the entries are."""
    orders = [an_order(index, "30.00") for index in range(1, 12)]

    entries = ledger(orders, seed_points=100, redemption_probability=1.0)

    assert [entry.created_at for entry in entries] == sorted(
        entry.created_at for entry in entries
    )


def test_entries_are_numbered_from_the_shared_counter() -> None:
    """Identifiers are unique across the whole population, not per customer."""
    config = shipped_config()
    numbers = count(41)
    first = ledger_for(
        substream(1, "a"),
        "demo-0001",
        10,
        START,
        [],
        config.loyalty,
        config.orders,
        numbers,
    )
    second = ledger_for(
        substream(1, "b"),
        "demo-0002",
        10,
        START,
        [],
        config.loyalty,
        config.orders,
        numbers,
    )

    assert first[0].entry_id == "loy-0000041"
    assert second[0].entry_id == "loy-0000042"
