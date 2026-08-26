"""The generator: five hundred customers, thirty stores, eighteen months.

Everything else in this package is a part; this is the assembly, and it is
written so that the two properties issue #25 is actually about are visible in
one screen of code.

**Reproducibility.** No stream is shared. Every draw comes from
:func:`~chip_chat.data_gen.rng.substream`, addressed by the seed and by what
is being drawn for, so a customer's history is a pure function of the seed and
their identifier — not of how many customers were generated before them, and
not of the wall clock, which appears nowhere in this module.

**Real food only.** Every identifier written into ``order_items`` came off a
catalogue row handed over by :class:`~chip_chat.data_gen.catalogue.OrderableMenu`,
and every price came out of ``item_prices`` at a named restaurant. The
generator has no way to name a food, which is what makes "zero orders
reference an item absent from the catalogue" a property rather than a hope.

The population's *texture* — the thing PRD section 09 says bounds what the
assistant can demonstrate — comes from three places that compound. Archetypes
give a customer their rhythm; a palate gives them their taste; and a window
gives them a life that may have started five months ago or stopped four months
ago. Two Tuesday regulars are both Tuesday regulars and are not the same
person.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from random import Random
from zoneinfo import ZoneInfo

from chip_chat.catalog import MenuCatalog, Store
from chip_chat.data_gen.baskets import Line, Palate, compose, mint_palate, repeatable
from chip_chat.data_gen.catalogue import OrderableMenu
from chip_chat.data_gen.config import GeneratorConfig, PersonaSpec
from chip_chat.data_gen.fixtures import entree_ids, measure_customers, select_fixtures
from chip_chat.data_gen.loyalty import ledger_for
from chip_chat.data_gen.records import (
    DEMO_ID_FORMAT,
    ORDER_ID_FORMAT,
    Channel,
    DemoVisitor,
    LoyaltyEntry,
    Order,
    OrderItem,
    Persona,
    SyntheticPopulation,
)
from chip_chat.data_gen.rng import substream, weighted_choice
from chip_chat.data_gen.timeline import placed_at, visit_days


@dataclass(frozen=True, slots=True)
class _Draft:
    """One order after it has been decided and before it has been numbered.

    Orders are numbered after a customer's whole history is sorted, because
    the cadence walk does not produce them in time order: an order may be
    moved as much as three days onto the weekday its customer keeps to, which
    can carry it past the one after it. Numbering the drafts in the order they
    were drawn would leave ``ord-0000042`` older than ``ord-0000041``, and the
    loyalty ledger — which walks a customer's orders adding points up — would
    be spending a balance before it was earned.
    """

    when: datetime
    sequence: int
    store_id: int
    channel: Channel
    status: str
    lines: tuple[Line, ...]


CENTS = Decimal("0.01")
"""What a money column is rounded to. Dollars have two decimal places and a
total that carries fourteen is a total computed by a program."""

DAYS_PER_MONTH = Decimal("30.4375")
"""Days in an average month over a four-year cycle. The window is stated in
months and walked in days, and 365.25 / 12 is the honest conversion."""


def generate_population(
    catalog: MenuCatalog, config: GeneratorConfig
) -> SyntheticPopulation:
    """Generate the whole synthetic population against one catalogue.

    Args:
        catalog: The real, harvested menu. Everything orderable comes from it.
        config: The tuned parameters, from ``population.toml`` or a file the
            caller named.

    Returns:
        The population, with the catalogue's ``content_version`` recorded on
        it so a downstream mart can be traced back to both of its inputs.

    Raises:
        ThinCatalogError: If the catalogue has no orderable entree or no
            store. Refusing is the only honest response; inventing either is
            the failure this whole package is arranged around.
    """
    menu = OrderableMenu(catalog, config.catalogue)
    stores = menu.stores(catalog.stores, config.stores)
    popularity = _popularity(config, stores)
    starts_at, ends_at = _window(config)

    personas = _personas(config, stores, popularity)
    assignment = _assignment(config)

    visitors: list[DemoVisitor] = []
    orders: list[Order] = []
    items: list[OrderItem] = []
    ledger: list[LoyaltyEntry] = []
    order_numbers = count(1)
    entry_numbers = count(1)
    taken: set[str] = set()

    for index, persona_id in enumerate(assignment, start=1):
        demo_id = DEMO_ID_FORMAT.format(index=index)
        spec = config.persona(persona_id)
        home = weighted_choice(
            substream(config.seed, "home", demo_id),
            stores,
            [popularity[store.store_id] for store in stores],
        )
        opened, closed = _lifetime(config, spec, demo_id, starts_at, ends_at)
        mine, my_items = _history(
            config,
            menu,
            stores,
            popularity,
            spec,
            demo_id,
            home,
            opened,
            closed,
            order_numbers,
        )
        orders.extend(mine)
        items.extend(my_items)
        created_at = opened.astimezone(UTC)
        ledger.extend(
            ledger_for(
                substream(config.seed, "loyalty", demo_id),
                demo_id,
                spec.seed_points,
                created_at,
                mine,
                config.loyalty,
                config.orders,
                entry_numbers,
            )
        )
        visitors.append(_visitor(config, demo_id, spec, stores, mine, created_at, taken))

    # Issue #26's fixtures are chosen last because they cannot be chosen
    # earlier: which customer best demonstrates being a Regular is a fact
    # about eighteen months of their history, and the history does not exist
    # until it has been generated.
    facts = measure_customers(
        visitors, orders, items, ledger, entree_ids(menu), ends_at.astimezone(UTC)
    )

    return SyntheticPopulation(
        seed=config.seed,
        catalog_content_version=menu.content_version,
        window_starts_at=starts_at.astimezone(UTC),
        window_ends_at=ends_at.astimezone(UTC),
        personas=personas,
        persona_fixtures=select_fixtures(facts, config, catalog, stores),
        demo_visitors=tuple(visitors),
        orders=tuple(orders),
        order_items=tuple(items),
        loyalty_ledger=tuple(ledger),
    )


def _window(config: GeneratorConfig) -> tuple[datetime, datetime]:
    """Return the first and last instant of the generated history."""
    span = timedelta(days=float(DAYS_PER_MONTH * config.months))
    return config.ends_at - span, config.ends_at


def _popularity(config: GeneratorConfig, stores: Sequence[Store]) -> dict[int, float]:
    """Return each store's share of traffic.

    Traffic is not spread evenly over thirty restaurants in the real world,
    and a population where it is has no busy store to talk about. The stores
    are shuffled by seed and then weighted by rank, so which store is busiest
    is a property of the seed rather than of the identifiers the locator
    happened to assign.
    """
    rng = substream(config.seed, "popularity")
    ranked = list(stores)
    rng.shuffle(ranked)
    return {
        store.store_id: 1.0
        / (rank + config.store_popularity_offset) ** config.store_popularity_exponent
        for rank, store in enumerate(ranked)
    }


def _personas(
    config: GeneratorConfig, stores: Sequence[Store], popularity: dict[int, float]
) -> tuple[Persona, ...]:
    """Mint the archetype rows, each set at a store drawn from the roster."""
    return tuple(
        Persona(
            persona_id=spec.persona_id,
            label=spec.label,
            home_store=weighted_choice(
                substream(config.seed, "persona", spec.persona_id),
                stores,
                [popularity[store.store_id] for store in stores],
            ).store_id,
            seed_points=spec.seed_points,
            narrative=spec.narrative,
        )
        for spec in config.personas
    )


def _assignment(config: GeneratorConfig) -> tuple[str, ...]:
    """Return which archetype each customer is, in ``demo_id`` order.

    Apportioned by largest remainder rather than sampled, so the shares in the
    config are the counts in the population — eighteen per cent of five
    hundred is ninety customers, every run, and not ninety-one on a seed that
    rolled well. Shuffled afterwards so that ``demo-0001`` through
    ``demo-0080`` are not all the same archetype, which would make every
    naive "first few customers" query a query about one persona.
    """
    counts = _apportion(config.customers, [spec.share for spec in config.personas])
    assigned = [
        spec.persona_id
        for spec, many in zip(config.personas, counts, strict=True)
        for _ in range(many)
    ]
    substream(config.seed, "assignment").shuffle(assigned)
    return tuple(assigned)


def _apportion(total: int, shares: Sequence[float]) -> list[int]:
    """Split ``total`` by ``shares``, giving the remainders to the largest.

    Args:
        total: How many to split.
        shares: Fractions summing to one.

    Returns:
        Counts summing to ``total``, one per share.
    """
    exact = [share * total for share in shares]
    counts = [int(value) for value in exact]
    remainder = total - sum(counts)
    order = sorted(
        range(len(shares)), key=lambda index: (-(exact[index] - counts[index]), index)
    )
    for index in order[:remainder]:
        counts[index] += 1
    return counts


def _lifetime(
    config: GeneratorConfig,
    spec: PersonaSpec,
    demo_id: str,
    starts_at: datetime,
    ends_at: datetime,
) -> tuple[datetime, datetime]:
    """Return when this customer's history starts and stops.

    The archetype's shares put them roughly where they belong in the window
    and the jitter moves them off each other, because a cohort that all lapsed
    on the same afternoon is a generator showing through.
    """
    rng = substream(config.seed, "lifetime", demo_id)
    span = ends_at - starts_at
    slide = timedelta(days=config.window_jitter_days)
    opened = starts_at + span * spec.active_from_share
    closed = starts_at + span * spec.active_until_share
    if spec.active_from_share > 0.0:
        opened += slide * rng.uniform(-1.0, 1.0)
    if spec.active_until_share < 1.0:
        closed += slide * rng.uniform(-1.0, 1.0)
    opened = max(opened, starts_at)
    closed = min(closed, ends_at)
    if closed <= opened:
        closed = min(opened + slide, ends_at)
    return opened, closed


def _history(
    config: GeneratorConfig,
    menu: OrderableMenu,
    stores: Sequence[Store],
    popularity: dict[int, float],
    spec: PersonaSpec,
    demo_id: str,
    home: Store,
    opened: datetime,
    closed: datetime,
    numbers: Iterator[int],
) -> tuple[tuple[Order, ...], tuple[OrderItem, ...]]:
    """Generate one customer's orders and their lines, oldest first."""
    taste = mint_palate(
        substream(config.seed, "palate", demo_id), menu, config.palate_concentration
    )
    usual = compose(
        substream(config.seed, "usual", demo_id),
        spec,
        menu,
        Channel.IN_STORE,
        taste,
    )
    zone = config.timing.zone(home.region)
    days = visit_days(
        substream(config.seed, "cadence", demo_id),
        spec,
        config.timing,
        _midnight(opened, zone),
        _midnight(closed, zone),
        _weekday(config, spec, demo_id),
    )

    drafted: list[_Draft] = []
    for sequence, day in enumerate(days, start=1):
        rng = substream(config.seed, "order", demo_id, sequence)
        store = _store(rng, spec, stores, popularity, home)
        channel = (
            Channel.DELIVERY if rng.random() < spec.delivery_share else Channel.IN_STORE
        )
        lines = _lines(rng, spec, menu, channel, taste, usual)
        if not lines:
            continue
        drafted.append(
            _Draft(
                when=placed_at(rng, spec, config.timing, day, store),
                sequence=sequence,
                store_id=store.store_id,
                channel=channel,
                status=weighted_choice(
                    rng, config.orders.statuses, config.orders.status_weights
                ),
                lines=lines,
            )
        )

    orders: list[Order] = []
    items: list[OrderItem] = []
    for draft in sorted(drafted, key=lambda row: (row.when, row.sequence)):
        order_id = ORDER_ID_FORMAT.format(index=next(numbers))
        priced = menu.pricing(draft.store_id)
        priced_lines = tuple(
            _priced(menu, order_id, number, line, priced, draft.channel)
            for number, line in enumerate(draft.lines, start=1)
        )
        orders.append(
            Order(
                order_id=order_id,
                demo_id=demo_id,
                store_id=draft.store_id,
                placed_at=draft.when,
                status=draft.status,
                total=sum(
                    (line.line_total for line in priced_lines), start=Decimal("0")
                ).quantize(CENTS),
                channel=draft.channel,
                priced_restaurant_id=priced,
            )
        )
        items.extend(priced_lines)
    return tuple(orders), tuple(items)


def _weekday(config: GeneratorConfig, spec: PersonaSpec, demo_id: str) -> int | None:
    """Return the one day of the week this customer keeps to, if any.

    Drawn once and kept for eighteen months, which is what makes a regular a
    regular. Drawn per customer rather than per archetype so that an archetype
    of eighty regulars is eighty people with their own days, and not eighty
    people with the same one.
    """
    if not spec.preferred_weekdays:
        return None
    return weighted_choice(
        substream(config.seed, "weekday", demo_id),
        spec.preferred_weekdays,
        [config.timing.day_of_week_weights[day] for day in spec.preferred_weekdays],
    )


def _midnight(when: datetime, zone: ZoneInfo) -> datetime:
    """Return midnight on ``when``'s local day, in the store's zone."""
    return when.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)


def _store(
    rng: Random,
    spec: PersonaSpec,
    stores: Sequence[Store],
    popularity: dict[int, float],
    home: Store,
) -> Store:
    """Choose where this order happens: usually home, sometimes not."""
    if rng.random() < spec.store_loyalty or len(stores) == 1:
        return home
    elsewhere = [store for store in stores if store.store_id != home.store_id]
    return weighted_choice(
        rng, elsewhere, [popularity[store.store_id] for store in elsewhere]
    )


def _lines(
    rng: Random,
    spec: PersonaSpec,
    menu: OrderableMenu,
    channel: Channel,
    taste: Palate,
    usual: Sequence[Line],
) -> tuple[Line, ...]:
    """Return this order's basket: the customer's usual, or a fresh one.

    The usual is what makes a regular a regular. It is only reused when every
    line in it is still sellable on this order's channel — Chipotle publishes
    availability per channel, and "the same bowl every Tuesday" must not be
    the thing that invents some.
    """
    keeping = usual and rng.random() < spec.usual_order_fidelity
    if keeping and repeatable(usual, menu, channel):
        return tuple(usual)
    return compose(rng, spec, menu, channel, taste)


def _priced(
    menu: OrderableMenu,
    order_id: str,
    number: int,
    line: Line,
    restaurant_id: int,
    channel: Channel,
) -> OrderItem:
    """Price one line out of ``item_prices``, modifiers included."""
    unit = menu.price(restaurant_id, line.item_id, channel)
    extras = sum(
        (
            menu.price(
                restaurant_id, menu.modifier(modifier_id).modifier_item_id, channel
            )
            for modifier_id in line.modifiers
        ),
        start=Decimal("0"),
    )
    return OrderItem(
        order_id=order_id,
        line_number=number,
        item_id=line.item_id,
        qty=line.qty,
        modifiers=line.modifiers,
        unit_price=unit.quantize(CENTS),
        line_total=(line.qty * (unit + extras)).quantize(CENTS),
    )


def _visitor(
    config: GeneratorConfig,
    demo_id: str,
    spec: PersonaSpec,
    stores: Sequence[Store],
    orders: Sequence[Order],
    created_at: datetime,
    taken: set[str],
) -> DemoVisitor:
    """Mint the customer row itself, once their history is known."""
    rng = substream(config.seed, "visitor", demo_id)
    names = config.names
    display = _display_name(rng, config, taken)
    override = None
    if rng.random() < names.home_store_override_share:
        override = stores[rng.randrange(len(stores))].store_id
    stated = None
    if names.stated_preferences and rng.random() < names.stated_preference_share:
        stated = names.stated_preferences[rng.randrange(len(names.stated_preferences))]
    return DemoVisitor(
        demo_id=demo_id,
        display_name=display,
        persona_id=spec.persona_id,
        thread_id=None,
        home_store_override=override,
        stated_preferences=stated,
        created_at=created_at,
        last_seen=max((order.placed_at for order in orders), default=created_at),
    )


def _display_name(rng: Random, config: GeneratorConfig, taken: set[str]) -> str:
    """Return an unused invented name.

    Names repeat in a real population and would repeat here too, which is
    fine right up until a demo shows two rows called Wren Larkin and a viewer
    wonders which one they are. The middle initial is the tie-break, so the
    pool does not have to be five hundred names long to hold five hundred
    customers.
    """
    given = config.names.given[rng.randrange(len(config.names.given))]
    family = config.names.family[rng.randrange(len(config.names.family))]
    candidate = f"{given} {family}"
    if candidate not in taken:
        taken.add(candidate)
        return candidate
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        suffixed = f"{given} {letter}. {family}"
        if suffixed not in taken:
            taken.add(suffixed)
            return suffixed
    taken.add(candidate)  # pragma: no cover - 26 collisions on one pair
    return candidate
