"""That the ledger runs Chipotle's published arithmetic and nothing else.

Issue #25 shipped this ledger with an invented earn rate and an invented
redemption threshold, said so, and left the reconciliation to issue #27. These
are the tests of that reconciliation at the level of one customer: the rate is
the published rate, the price is the published price, the two published limits
are honoured, and no one ever spends a balance they did not have. The whole
population is checked in ``test_ledger_population.py``.

The terms come from the harvest's own fixture site, so "published" here means
the same bytes the policy tests parse, not a number written down twice.
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count

import pytest
from population_fixtures import fixture_terms, shipped_config

from chip_chat.data_gen import Channel, Order, RewardsTerms
from chip_chat.data_gen.loyalty import ledger_for
from chip_chat.data_gen.rng import substream

START = datetime(2025, 3, 1, 12, 0, tzinfo=UTC)


def an_order(index: int, total: str, status: str = "COMPLETED", days: int = 0) -> Order:
    """Return one order, at a stated total, status and offset from the start."""
    return Order(
        order_id=f"ord-{index:07d}",
        demo_id="demo-0001",
        store_id=679,
        placed_at=START + timedelta(days=days or index),
        status=status,
        total=Decimal(total),
        channel=Channel.IN_STORE,
        priced_restaurant_id=679,
    )


def ledger(
    orders,
    seed_points: int = 0,
    redeems: float = 0.0,
    terms: RewardsTerms | None = None,
    **overrides,
):
    """Build one customer's ledger over ``orders``, under the published terms."""
    config = shipped_config()
    loyalty = dataclasses.replace(config.loyalty, **overrides)
    return ledger_for(
        substream(1, "loyalty"),
        "demo-0001",
        seed_points,
        START,
        orders,
        terms or fixture_terms(),
        loyalty,
        config.orders,
        redeems,
        count(1),
    )


def balances(entries) -> list[int]:
    """Return the running balance after each entry."""
    running, seen = 0, []
    for entry in entries:
        running += entry.delta
        seen.append(running)
    return seen


# --------------------------------------------------------------------------
# The published earn rate
# --------------------------------------------------------------------------


def test_points_are_earned_at_the_rate_chipotle_publishes() -> None:
    """Not at a rate this repository chose. The two are asserted equal here."""
    terms = fixture_terms()

    entries = ledger([an_order(1, "12.40")], terms=terms)

    assert terms.points_per_dollar == 10
    assert [entry.delta for entry in entries] == [124]
    assert entries[0].order_id == "ord-0000001"
    assert entries[0].reward_name is None


def test_the_earn_rate_is_read_and_not_hardcoded() -> None:
    """A different published rate has to produce a different ledger.

    Without this, "the rate comes from the published terms" and "the rate
    happens to equal the published terms" are the same test.
    """
    terms = dataclasses.replace(fixture_terms(), points_per_dollar=3)

    entries = ledger([an_order(1, "12.40")], terms=terms)

    assert [entry.delta for entry in entries] == [37]


def test_a_partial_dollar_earns_no_partial_point() -> None:
    """A register prints whole points; a ledger of thirds of a point prints none."""
    entries = ledger([an_order(1, "9.99")])

    assert [entry.delta for entry in entries] == [99]


def test_an_order_too_small_to_earn_a_point_writes_no_entry() -> None:
    """An entry moving zero points is a row that says nothing."""
    assert ledger([an_order(1, "0.05")]) == ()


# --------------------------------------------------------------------------
# What qualifies
# --------------------------------------------------------------------------


def test_a_refunded_order_is_history_and_earns_nothing() -> None:
    """It stays in ``orders``; it just does not move any points."""
    entries = ledger([an_order(1, "12.40", status="REFUNDED"), an_order(2, "9.00")])

    assert [entry.order_id for entry in entries] == ["ord-0000002"]


def test_only_the_published_number_of_purchases_a_day_earn() -> None:
    """ "Limited to three qualifying purchases per day" — the terms, verbatim.

    The fourth order of the day is a real order that earns nothing, which is
    what the published limit means and is not something the generator may
    decide for itself.
    """
    terms = fixture_terms()
    same_day = [an_order(index, "20.00", days=1) for index in range(1, 6)]

    entries = ledger(same_day, terms=terms)

    assert terms.daily_qualifying_purchases == 3
    assert [entry.order_id for entry in entries] == [
        "ord-0000001",
        "ord-0000002",
        "ord-0000003",
    ]


def test_the_daily_limit_resets_on_the_next_day() -> None:
    """It is a limit per day, not a limit per customer."""
    orders = [an_order(index, "20.00", days=1) for index in range(1, 5)]
    orders.append(an_order(9, "20.00", days=2))

    entries = ledger(orders)

    assert [entry.order_id for entry in entries] == [
        "ord-0000001",
        "ord-0000002",
        "ord-0000003",
        "ord-0000009",
    ]


# --------------------------------------------------------------------------
# Redemptions, against the published Rewards Exchange
# --------------------------------------------------------------------------


def test_every_redemption_names_a_published_reward_and_costs_its_price() -> None:
    """Issue #27's third consistency requirement, asserted on one customer."""
    terms = fixture_terms()
    published = {reward.name: reward.point_cost for reward in terms.rewards}

    entries = ledger([an_order(index, "20.00") for index in range(1, 30)], redeems=1.0)
    spent = [entry for entry in entries if entry.delta < 0]

    assert spent
    for entry in spent:
        assert entry.reward_name in published
        assert entry.delta == -published[entry.reward_name]
        assert entry.order_id is not None


def test_the_cheapest_published_reward_is_the_threshold() -> None:
    """There is no threshold constant left to disagree with the Exchange."""
    terms = fixture_terms()
    cheapest = terms.cheapest.point_cost

    entries = ledger([an_order(index, "2.00") for index in range(1, 12)], redeems=1.0)

    assert cheapest == 85
    for balance, entry in zip(balances(entries), entries, strict=True):
        if entry.delta < 0:
            assert balance + -entry.delta >= cheapest


def test_nobody_redeems_who_cannot_afford_the_cheapest_reward() -> None:
    entries = ledger([an_order(1, "1.00")], redeems=1.0)

    assert all(entry.delta > 0 for entry in entries)


def test_a_balance_is_never_overdrawn() -> None:
    """The property the whole ledger turns on, at the level of one customer."""
    entries = ledger(
        [an_order(index, "20.00") for index in range(1, 40)],
        seed_points=250,
        redeems=1.0,
    )

    assert all(balance >= 0 for balance in balances(entries))


def test_an_eager_customer_still_only_redeems_what_they_can_afford() -> None:
    """``splurge_share`` chooses among affordable rewards; it never reaches past."""
    terms = fixture_terms()

    for share in (0.0, 1.0):
        entries = ledger(
            [an_order(index, "20.00") for index in range(1, 30)],
            redeems=1.0,
            splurge_share=share,
        )
        assert all(balance >= 0 for balance in balances(entries))
        assert {entry.reward_name for entry in entries if entry.delta < 0} <= {
            reward.name for reward in terms.rewards
        }


def test_a_splurging_customer_takes_the_best_reward_they_can_afford() -> None:
    """Which is what makes the Rewards Exchange's expensive end reachable."""
    terms = fixture_terms()

    entries = ledger(
        [an_order(index, "60.00") for index in range(1, 20)],
        redeems=1.0,
        splurge_share=1.0,
    )

    assert terms.costliest.name in {
        entry.reward_name for entry in entries if entry.delta < 0
    }


def test_an_archetype_that_never_redeems_accumulates() -> None:
    """The Lapsed Regular, in miniature: eighteen months of earning, no spending."""
    entries = ledger([an_order(index, "20.00") for index in range(1, 30)], redeems=0.0)

    assert all(entry.delta > 0 for entry in entries)
    assert all(entry.reward_name is None for entry in entries)
    assert sum(entry.delta for entry in entries) > fixture_terms().costliest.point_cost


# --------------------------------------------------------------------------
# The published expiry window
# --------------------------------------------------------------------------


def test_a_balance_expires_after_the_published_inactivity_window() -> None:
    """ "Points expire after 365 days of account inactivity" — the terms."""
    terms = fixture_terms()
    orders = [an_order(1, "20.00", days=1), an_order(2, "20.00", days=400)]

    entries = ledger(orders)

    assert terms.inactivity_expiry_days == 365
    expired = [entry for entry in entries if entry.reason == "POINTS_EXPIRED"]
    assert [entry.delta for entry in expired] == [-200]
    assert expired[0].created_at == orders[0].placed_at + timedelta(days=365)
    assert expired[0].order_id is None
    assert balances(entries) == [200, 0, 200]


def test_a_customer_who_keeps_ordering_never_expires() -> None:
    """ "Your points won't expire as long as you keep your account active"."""
    orders = [an_order(index, "20.00", days=index * 300) for index in range(1, 5)]

    entries = ledger(orders)

    assert all(entry.reason == "ORDER" for entry in entries)


def test_the_expiry_window_is_read_and_not_hardcoded() -> None:
    """A shorter published window has to expire a balance the long one keeps."""
    terms = dataclasses.replace(fixture_terms(), inactivity_expiry_days=30)
    orders = [an_order(1, "20.00", days=1), an_order(2, "20.00", days=90)]

    entries = ledger(orders, terms=terms)

    assert [entry.reason for entry in entries] == ["ORDER", "POINTS_EXPIRED", "ORDER"]


def test_an_opening_balance_starts_the_inactivity_clock() -> None:
    """An account exists from the day it was opened, balance and all."""
    entries = ledger([an_order(1, "20.00", days=500)], seed_points=250)

    assert [entry.reason for entry in entries] == [
        "SIGNUP_BONUS",
        "POINTS_EXPIRED",
        "ORDER",
    ]
    assert entries[1].created_at == START + timedelta(days=365)


# --------------------------------------------------------------------------
# The shape of the ledger itself
# --------------------------------------------------------------------------


def test_an_opening_balance_is_an_entry_and_not_a_column() -> None:
    """A balance that is not in the ledger is a balance nothing can reconcile."""
    entries = ledger([], seed_points=250)

    assert len(entries) == 1
    assert entries[0].delta == 250
    assert entries[0].reason == shipped_config().loyalty.seed_reason
    assert entries[0].order_id is None
    assert entries[0].reward_name is None
    assert entries[0].created_at == START


def test_an_archetype_with_no_opening_balance_gets_no_entry() -> None:
    assert ledger([], seed_points=0) == ()


def test_the_ledger_is_in_time_order() -> None:
    """The running balance is only meaningful if the entries are."""
    orders = [an_order(index, "30.00") for index in range(1, 12)]

    entries = ledger(orders, seed_points=100, redeems=1.0)

    assert [entry.created_at for entry in entries] == sorted(
        entry.created_at for entry in entries
    )


def test_entries_are_numbered_from_the_shared_counter() -> None:
    """Identifiers are unique across the whole population, not per customer."""
    config, terms = shipped_config(), fixture_terms()
    numbers = count(41)
    made = [
        ledger_for(
            substream(1, stream),
            demo_id,
            10,
            START,
            [],
            terms,
            config.loyalty,
            config.orders,
            0.0,
            numbers,
        )
        for stream, demo_id in (("a", "demo-0001"), ("b", "demo-0002"))
    ]

    assert made[0][0].entry_id == "loy-0000041"
    assert made[1][0].entry_id == "loy-0000042"


@pytest.mark.parametrize("seed_points", [0, 250])
def test_the_reasons_are_the_configured_vocabulary(seed_points: int) -> None:
    """``reason`` says what kind of movement it is; ``reward_name`` says what."""
    loyalty = shipped_config().loyalty
    vocabulary = {
        loyalty.seed_reason,
        loyalty.earn_reason,
        loyalty.redeem_reason,
        loyalty.expiry_reason,
    }

    entries = ledger(
        [an_order(index, "40.00") for index in range(1, 20)],
        seed_points=seed_points,
        redeems=1.0,
    )

    assert entries
    assert {entry.reason for entry in entries} <= vocabulary
    for entry in entries:
        assert (entry.reward_name is not None) == (entry.reason == loyalty.redeem_reason)
