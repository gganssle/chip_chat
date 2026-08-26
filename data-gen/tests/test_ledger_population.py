"""Issue #27's invariants, asserted over the whole five hundred.

The unit tests in ``test_loyalty.py`` check one customer against hand-built
orders. These check the generated population, because the acceptance criterion
is about the population: "Ledger generated for all 500 customers with the above
invariants asserted by tests."

The three consistency requirements are the three sections below.

* The ledger sum per ``demo_id`` is the balance, and it is never negative at
  any point in history.
* Every accrual traces to a real, settled, qualifying order and is worth what
  the published rate says that order was worth.
* Every redemption traces to a real published reward and costs its published
  price.

Two published limits — three qualifying purchases a day, and expiry after 365
days of inactivity — are honoured by the ledger and never reached by this
population. That is asserted rather than assumed: a retune that produced a
four-order day or a fourteen-month gap would otherwise change what the ledger
means without changing a line of code.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC
from itertools import pairwise

from population_fixtures import fixture_population, fixture_terms

from chip_chat.data_gen import LoyaltyEntry, SyntheticPopulation


def ledgers(population: SyntheticPopulation) -> dict[str, list[LoyaltyEntry]]:
    """Return each customer's entries, in the order they were written."""
    grouped: dict[str, list[LoyaltyEntry]] = defaultdict(list)
    for entry in population.loyalty_ledger:
        grouped[entry.demo_id].append(entry)
    return grouped


def balance_of(entries: Sequence[LoyaltyEntry]) -> int:
    """Return the balance a ledger derives to. There is no balance column."""
    return sum(entry.delta for entry in entries)


# --------------------------------------------------------------------------
# The ledger balances
# --------------------------------------------------------------------------


def test_every_customer_has_a_ledger() -> None:
    """All five hundred, not the ones who happened to order."""
    population = fixture_population()

    assert ledgers(population).keys() == {
        visitor.demo_id for visitor in population.demo_visitors
    }


def test_a_balance_is_the_sum_of_its_entries_and_nothing_else() -> None:
    """What ``get_points_balance`` returns is a ``SUM(delta)`` and no more.

    There is no balance column in this package to disagree with the ledger, so
    what this asserts is the property that keeps it that way: every balance is
    derivable, and the derivation is addition.
    """
    population = fixture_population()

    for demo_id, entries in ledgers(population).items():
        derived = balance_of(entries)
        assert derived == sum(
            entry.delta for entry in population.loyalty_ledger if entry.demo_id == demo_id
        )
        assert derived >= 0


def test_no_balance_is_ever_negative_at_any_point_in_history() -> None:
    """Not just at the end. Nobody spends points they had not yet earned."""
    for entries in ledgers(fixture_population()).values():
        running = 0
        for entry in entries:
            running += entry.delta
            assert running >= 0, entry


def test_every_ledger_is_in_time_order() -> None:
    """A running balance over entries that are out of order means nothing."""
    for entries in ledgers(fixture_population()).values():
        stamps = [entry.created_at for entry in entries]
        assert stamps == sorted(stamps)


def test_entry_identifiers_are_unique_across_the_population() -> None:
    population = fixture_population()

    identifiers = {entry.entry_id for entry in population.loyalty_ledger}

    assert len(identifiers) == len(population.loyalty_ledger)


# --------------------------------------------------------------------------
# Every accrual traces to a real order
# --------------------------------------------------------------------------


def test_every_accrual_traces_to_that_customers_own_settled_order() -> None:
    """A ``demo_id`` earning on another customer's order is a leak, not a bug."""
    population = fixture_population()
    orders = {order.order_id: order for order in population.orders}

    for entry in population.loyalty_ledger:
        if entry.reason != "ORDER":
            continue
        order = orders[entry.order_id or ""]
        assert order.demo_id == entry.demo_id
        assert order.status == "COMPLETED"
        assert entry.created_at == order.placed_at


def test_every_accrual_is_worth_what_the_published_rate_says() -> None:
    """Ten points per dollar of the order's own total, floored to whole points."""
    population, terms = fixture_population(), fixture_terms()
    orders = {order.order_id: order for order in population.orders}

    for entry in population.loyalty_ledger:
        if entry.reason != "ORDER":
            continue
        total = orders[entry.order_id or ""].total
        assert entry.delta == int(total * terms.points_per_dollar)


def test_every_settled_order_earns_exactly_once() -> None:
    """The published limits are the only reason one would not, and neither bites.

    Stated as an equality rather than as an inequality on purpose: an order
    that earned twice and an order that earned not at all both pass "at most
    one entry per order", and both are wrong.
    """
    population = fixture_population()
    settled = [order for order in population.orders if order.status == "COMPLETED"]
    earned = [
        entry.order_id or ""
        for entry in population.loyalty_ledger
        if entry.reason == "ORDER"
    ]

    assert sorted(earned) == sorted(order.order_id for order in settled)


def test_a_cancelled_or_refunded_order_moves_no_points() -> None:
    """It stays in ``orders`` because it happened. It earns because it settled."""
    population = fixture_population()
    unsettled = {
        order.order_id for order in population.orders if order.status != "COMPLETED"
    }

    assert unsettled
    assert not [
        entry
        for entry in population.loyalty_ledger
        if entry.reason == "ORDER" and entry.order_id in unsettled
    ]


def test_an_opening_balance_names_no_order() -> None:
    """It is a grant at enrollment, and there is no order for it to point at."""
    for entry in fixture_population().loyalty_ledger:
        if entry.reason == "SIGNUP_BONUS":
            assert entry.order_id is None
            assert entry.reward_name is None


# --------------------------------------------------------------------------
# Every redemption traces to a real published reward
# --------------------------------------------------------------------------


def test_the_redeemable_rewards_are_exactly_the_harvested_catalogue() -> None:
    """Issue #27's third acceptance criterion.

    Both directions. Nothing was redeemed that Chipotle does not publish, and
    nothing Chipotle publishes went unreachable in five hundred customers —
    the second half being what would catch a Rewards Exchange whose expensive
    end no generated balance can afford.
    """
    population, terms = fixture_population(), fixture_terms()

    redeemed = {
        entry.reward_name
        for entry in population.loyalty_ledger
        if entry.reason == "REWARD_REDEEMED"
    }

    assert redeemed == {reward.name for reward in terms.rewards}


def test_every_redemption_costs_its_published_price() -> None:
    population, terms = fixture_population(), fixture_terms()
    published = {reward.name: reward.point_cost for reward in terms.rewards}

    spent = [
        entry for entry in population.loyalty_ledger if entry.reason == "REWARD_REDEEMED"
    ]

    assert spent
    for entry in spent:
        assert entry.delta == -published[entry.reward_name or ""]


def test_every_redemption_happened_at_one_of_that_customers_orders() -> None:
    """Points are spent at a register, which is an order this customer placed."""
    population = fixture_population()
    orders = {order.order_id: order for order in population.orders}

    for entry in population.loyalty_ledger:
        if entry.reason != "REWARD_REDEEMED":
            continue
        assert orders[entry.order_id or ""].demo_id == entry.demo_id


def test_only_redemptions_name_a_reward() -> None:
    """``reward_name`` says what was spent, and is null where nothing was."""
    for entry in fixture_population().loyalty_ledger:
        assert (entry.reward_name is not None) == (entry.reason == "REWARD_REDEEMED")


# --------------------------------------------------------------------------
# The published limits, honoured and never reached
# --------------------------------------------------------------------------


def test_no_customer_exceeds_the_published_daily_purchase_limit() -> None:
    """ "Limited to three qualifying purchases per day."

    The ledger caps earning at the published number; this asserts the tuned
    population never gets near it, so that "every settled order earns exactly
    once" above is a statement about this population and not a coincidence
    that the cap happens not to contradict.
    """
    population, terms = fixture_population(), fixture_terms()
    per_day: dict[tuple[str, object], int] = defaultdict(int)

    for order in population.orders:
        if order.status == "COMPLETED":
            per_day[(order.demo_id, order.placed_at.astimezone(UTC).date())] += 1

    assert max(per_day.values()) <= terms.daily_qualifying_purchases


def test_nobody_is_inactive_long_enough_for_the_published_expiry() -> None:
    """ "Points expire after 365 days of account inactivity."

    Eighteen months is not long enough for anybody in this population to lapse
    past the published window, so no balance expires — which is why the Lapsed
    Regular still has theirs. A retune that lengthened the window or the lapse
    would break this test rather than quietly zero sixty customers.
    """
    population, terms = fixture_population(), fixture_terms()
    history: dict[str, list] = defaultdict(list)

    for order in sorted(population.orders, key=lambda row: row.placed_at):
        if order.status == "COMPLETED":
            history[order.demo_id].append(order.placed_at)

    longest = max(
        (later - earlier).days
        for stamps in history.values()
        for earlier, later in pairwise(stamps)
    )

    assert longest < terms.inactivity_expiry_days
    assert not [
        entry for entry in population.loyalty_ledger if entry.reason == "POINTS_EXPIRED"
    ]


# --------------------------------------------------------------------------
# What the demo can actually show
# --------------------------------------------------------------------------


def test_a_persona_can_afford_a_meaningful_redemption() -> None:
    """Issue #27's second acceptance criterion.

    "At least one persona has a balance sufficient for a meaningful
    ``redeem_points`` demo" — meaning the most expensive thing the published
    Rewards Exchange sells, not the cheapest thing on it.
    """
    population, terms = fixture_population(), fixture_terms()
    personas = {
        visitor.demo_id: visitor.persona_id for visitor in population.demo_visitors
    }

    rich = {
        personas[demo_id]
        for demo_id, entries in ledgers(population).items()
        if balance_of(entries) >= terms.costliest.point_cost
    }

    assert rich


def test_the_lapsed_regular_carries_an_unredeemed_balance_worth_surfacing() -> None:
    """The fourth item of issue #27's scope, and the reason redemption is per
    archetype.

    Almost every lapsed customer, not one of them: a persona a visitor is
    assigned at random is only a demonstration if the story holds whichever of
    them they get.
    """
    population, terms = fixture_population(), fixture_terms()
    personas = {
        visitor.demo_id: visitor.persona_id for visitor in population.demo_visitors
    }
    lapsed = [
        entries
        for demo_id, entries in ledgers(population).items()
        if personas[demo_id] == "lapsed"
    ]

    assert lapsed
    worth_surfacing = [
        entries for entries in lapsed if balance_of(entries) >= terms.costliest.point_cost
    ]

    assert len(worth_surfacing) >= 0.9 * len(lapsed)


def test_some_customers_have_spent_points_and_can_be_asked_about_it() -> None:
    """ "Redemption history, so some personas have already spent points."

    Asserted across archetypes rather than in total, because a redemption
    history concentrated in one persona is a history most visitors never see.
    """
    population = fixture_population()
    personas = {
        visitor.demo_id: visitor.persona_id for visitor in population.demo_visitors
    }
    spent = {
        personas[entry.demo_id]
        for entry in population.loyalty_ledger
        if entry.reason == "REWARD_REDEEMED"
    }

    assert len(spent) >= 5
