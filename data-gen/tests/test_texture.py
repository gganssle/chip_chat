"""That the population has texture, which is the thing the ticket is really about.

Issue #25 calls this "the highest-leverage phase and the one everyone rushes",
and the failure mode it names is not a crash — it is five hundred customers who
all behave the same, which produces a chatbot with nothing interesting to say
and no amount of downstream engineering fixes it. PRD section 09 puts it as a
constraint: behaviour the population does not exhibit cannot be demonstrated.

So these tests assert that the specific behaviours the ticket names are *in*
the data: "the Tuesday regular who orders the identical bowl, the customer who
lapsed four months ago, the office manager placing group orders". Each one is
checked as a property of the archetype relative to the others, not as a
threshold pulled out of the air — the regular repeats their usual more than the
explorer does, the lapsed customer's history stops before everyone else's — so
that retuning ``population.toml`` moves the numbers without breaking the test
that the archetypes are still distinguishable.

Variety of *food* is not asserted here, and issue #28 is where it went. This
catalogue has two entrees in it, so a threshold on how many were ordered would
be a test of the fixture — ``texture.py`` asks the question in the form that
survives a small catalogue instead, as coverage of what the catalogue makes
orderable, and ``test_texture_suite.py`` is its suite. What is here is variety
of *behaviour*, asserted archetype by archetype, which is the half that was
always assertable against a nine-item menu.
"""

import statistics
from collections import Counter, defaultdict

from population_fixtures import (
    fixture_catalog,
    fixture_population,
    personas_by_id,
    shipped_config,
)

from chip_chat.data_gen import Channel, OrderableMenu, mint_palate
from chip_chat.data_gen.rng import substream

Basket = tuple[tuple[str, tuple[str, ...], int], ...]
"""One order flattened into comparable lines: item, modifiers, quantity."""


def baskets_by_customer() -> dict[str, list[Basket]]:
    """Return each customer's orders as comparable baskets."""
    lines: defaultdict[str, list[tuple[str, tuple[str, ...], int]]] = defaultdict(list)
    for item in fixture_population().order_items:
        lines[item.order_id].append((item.item_id, item.modifiers, item.qty))
    grouped: defaultdict[str, list[Basket]] = defaultdict(list)
    for order in fixture_population().orders:
        grouped[order.demo_id].append(tuple(sorted(lines[order.order_id])))
    return grouped


def repeat_rate(persona_id: str) -> float:
    """Return the median share of a customer's orders that are their commonest."""
    personas = personas_by_id()
    rates = [
        Counter(baskets).most_common(1)[0][1] / len(baskets)
        for demo_id, baskets in baskets_by_customer().items()
        if personas[demo_id] == persona_id
        if len(baskets) >= 5
    ]
    return statistics.median(rates)


def test_the_population_is_the_size_the_config_asks_for() -> None:
    """Five hundred customers, thirty stores, eighteen months."""
    config, population = shipped_config(), fixture_population()

    assert len(population.demo_visitors) == config.customers == 500
    assert len({order.store_id for order in population.orders}) == config.stores == 30
    span = population.window_ends_at - population.window_starts_at
    assert 540 <= span.days <= 555


def test_every_archetype_reaches_the_population() -> None:
    """An archetype nobody is assigned is behaviour that cannot be demonstrated."""
    config, population = shipped_config(), fixture_population()
    counts = Counter(row.persona_id for row in population.demo_visitors)

    assert set(counts) == {spec.persona_id for spec in config.personas}
    for spec in config.personas:
        assert counts[spec.persona_id] == round(spec.share * config.customers)


def test_the_regular_orders_the_identical_bowl_and_the_explorer_does_not() -> None:
    """The first two textures the ticket names, as a comparison between them."""
    assert repeat_rate("regular") > 0.8
    assert repeat_rate("explorer") < 0.3
    assert repeat_rate("regular") > repeat_rate("occasional")


def test_the_regular_keeps_to_one_day_of_the_week() -> None:
    """And a different day per customer, or Tuesday swallows the whole population."""
    personas = personas_by_id()
    stores = {row.store_id: row for row in fixture_catalog().stores}
    timing = shipped_config().timing
    days: dict[str, Counter[int]] = defaultdict(Counter)
    for order in fixture_population().orders:
        local = order.placed_at.astimezone(timing.zone(stores[order.store_id].region))
        days[order.demo_id][local.weekday()] += 1

    regulars = [
        counted for demo_id, counted in days.items() if personas[demo_id] == "regular"
    ]
    concentration = [
        counted.most_common(1)[0][1] / counted.total() for counted in regulars
    ]
    chosen = Counter(counted.most_common(1)[0][0] for counted in regulars)

    assert statistics.median(concentration) > 0.7
    assert len(chosen) == 7


def test_the_lapsed_customer_stopped_and_the_newcomer_started_late() -> None:
    """Two textures that are properties of the window rather than of a basket."""
    personas = personas_by_id()
    population = fixture_population()
    window = population.window_ends_at - population.window_starts_at
    last: dict[str, float] = {}
    first: dict[str, float] = {}
    for order in population.orders:
        share = (order.placed_at - population.window_starts_at) / window
        last[order.demo_id] = max(last.get(order.demo_id, 0.0), share)
        first[order.demo_id] = min(first.get(order.demo_id, 1.0), share)

    lapsed = [last[demo] for demo in last if personas[demo] == "lapsed"]
    steady = [last[demo] for demo in last if personas[demo] == "regular"]
    newcomers = [first[demo] for demo in first if personas[demo] == "newcomer"]
    established = [first[demo] for demo in first if personas[demo] == "regular"]

    assert statistics.median(lapsed) < 0.85
    assert statistics.median(steady) > 0.95
    assert statistics.median(newcomers) > 0.6
    assert statistics.median(established) < 0.1


def test_the_office_manager_places_group_orders_on_delivery_at_lunch() -> None:
    """The third texture the ticket names, in all three of its parts."""
    personas = personas_by_id()
    stores = {row.store_id: row for row in fixture_catalog().stores}
    timing = shipped_config().timing
    sizes: dict[str, list[int]] = defaultdict(list)
    delivery: Counter[str] = Counter()
    lunch: Counter[str] = Counter()
    placed: Counter[str] = Counter()
    quantities: defaultdict[str, int] = defaultdict(int)
    for item in fixture_population().order_items:
        quantities[item.order_id] += item.qty
    for order in fixture_population().orders:
        persona = personas[order.demo_id]
        sizes[persona].append(quantities[order.order_id])
        placed[persona] += 1
        if order.channel is Channel.DELIVERY:
            delivery[persona] += 1
        local = order.placed_at.astimezone(timing.zone(stores[order.store_id].region))
        if local.hour < 16:
            lunch[persona] += 1

    assert statistics.mean(sizes["office_manager"]) > 5
    assert statistics.mean(sizes["office_manager"]) > 2 * statistics.mean(
        sizes["regular"]
    )
    assert delivery["office_manager"] / placed["office_manager"] > 0.5
    assert lunch["office_manager"] / placed["office_manager"] > 0.85


def test_orders_cluster_at_lunch_and_dinner_and_never_overnight() -> None:
    """The published opening hours are honoured, and the peaks are where meals are."""
    stores = {row.store_id: row for row in fixture_catalog().stores}
    timing = shipped_config().timing
    hours: Counter[int] = Counter()
    for order in fixture_population().orders:
        local = order.placed_at.astimezone(timing.zone(stores[order.store_id].region))
        hours[local.hour] += 1

    assert min(hours) >= 6
    assert max(hours) <= 23
    total = hours.total()
    assert sum(hours[hour] for hour in (11, 12, 13)) / total > 0.4
    assert sum(hours[hour] for hour in (17, 18, 19)) / total > 0.2


def test_a_weekend_does_not_look_like_a_weekday() -> None:
    """ "Weekend differences", asserted as the shape of the day rather than its size."""
    stores = {row.store_id: row for row in fixture_catalog().stores}
    timing = shipped_config().timing
    weekday: list[int] = []
    weekend: list[int] = []
    for order in fixture_population().orders:
        local = order.placed_at.astimezone(timing.zone(stores[order.store_id].region))
        (weekend if local.weekday() >= 5 else weekday).append(local.hour)

    assert statistics.median(weekend) != statistics.median(weekday) or (
        statistics.mean(weekend) > statistics.mean(weekday)
    )
    assert statistics.mean(weekend) > statistics.mean(weekday)


def test_the_year_drifts() -> None:
    """Seasonal drift: December is not January is not May."""
    population = fixture_population()
    months = Counter(order.placed_at.strftime("%Y-%m") for order in population.orders)

    assert len(months) >= shipped_config().months
    by_month: Counter[int] = Counter()
    for order in population.orders:
        by_month[order.placed_at.month] += 1
    busiest = max(by_month.values())
    quietest = min(by_month.values())
    assert busiest > quietest * 1.15


def test_traffic_is_not_spread_evenly_over_the_stores() -> None:
    """A population with no busy store has no busy store to talk about."""
    counted = Counter(order.store_id for order in fixture_population().orders)
    shares = sorted(counted.values(), reverse=True)

    assert len(shares) == shipped_config().stores
    assert shares[0] > 3 * shares[-1]


def test_customers_of_one_archetype_are_not_the_same_customer() -> None:
    """The palate is what makes eighty regulars eighty people rather than one.

    Asserted on the palate itself rather than on the food, because this
    catalogue publishes two entrees, one rice and one bean: the number of
    distinguishable baskets in it is single digits whatever the palates say,
    and a threshold tuned to that number would be a test of the fixture. That
    the *food* is varied is issue #28's question, and it needs a real harvest
    to ask.
    """
    config = shipped_config()
    menu = OrderableMenu(fixture_catalog(), config.catalogue)
    tastes = [
        mint_palate(
            substream(config.seed, "palate", row.demo_id),
            menu,
            config.palate_concentration,
        )
        for row in fixture_population().demo_visitors
    ]
    favourites = Counter(max(taste, key=lambda key: taste[key]) for taste in tastes)

    assert len({tuple(sorted(taste.items())) for taste in tastes}) == len(tastes)
    assert len(favourites) > 1
    uniform = 1.0 / len(tastes[0])
    peaked = [max(taste.values()) > 2 * uniform for taste in tastes]
    assert sum(peaked) / len(peaked) > 0.5


def test_the_regulars_do_not_all_have_the_same_usual() -> None:
    """As much variety as two entrees and four modifiers can carry."""
    personas = personas_by_id()
    usual = {
        demo_id: Counter(baskets).most_common(1)[0][0]
        for demo_id, baskets in baskets_by_customer().items()
        if personas[demo_id] == "regular"
        if len(baskets) >= 5
    }

    assert len(usual) > 50
    assert len(set(usual.values())) > 1


def test_the_ledger_moves_in_both_directions() -> None:
    """Earning and spending both happen, and nobody spends what they never had."""
    population = fixture_population()
    balances: dict[str, int] = defaultdict(int)
    reasons = Counter(row.reason for row in population.loyalty_ledger)
    for entry in sorted(population.loyalty_ledger, key=lambda row: row.created_at):
        balances[entry.demo_id] += entry.delta
        assert balances[entry.demo_id] >= 0

    loyalty = shipped_config().loyalty
    assert reasons[loyalty.earn_reason] > 0
    assert reasons[loyalty.redeem_reason] > 0
    assert reasons[loyalty.seed_reason] > 0


def test_a_few_orders_did_not_go_through() -> None:
    """Cancelled and refunded orders are real history the demo has to handle."""
    statuses = Counter(order.status for order in fixture_population().orders)

    assert set(statuses) == set(shipped_config().orders.statuses)
    assert statuses["COMPLETED"] / statuses.total() > 0.9
