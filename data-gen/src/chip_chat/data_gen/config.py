"""Every number that shapes the population, in one file that is not this one.

Issue #25's fourth acceptance criterion is the reason this module exists:
"generation parameters live in a config file, not scattered constants, so the
population can be retuned without archaeology". The parameters are therefore
data — ``population.toml``, shipped inside the package — and this module is
only the reader that turns that file into validated records.

The validation is deliberately unforgiving. :class:`ConfigError` is raised for
shares that do not sum to one, for a cadence of zero days, for an hour
distribution with no mass in it. None of those are clamped, because a file
whose numbers are silently corrected is no longer the file that was tuned, and
the retuning loop this criterion asks for only closes if the number you wrote
is the number that ran.

The archetypes are here rather than in code for the same reason PRD section 09
calls the generator a product decision: adding "the customer who only ever
orders catering" is a change to a table of behaviour, not a change to the
walk that reads it.
"""

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from chip_chat.data_gen.errors import ConfigError

PACKAGED_CONFIG = "population.toml"
"""The tuned population that ships with the package. ``--config`` replaces it."""

WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
"""Day names, Monday first, spelled as :meth:`datetime.datetime.strftime` and
the catalogue's :class:`~chip_chat.catalog.StoreHours` both spell them."""

MINIMUM_CADENCE_DAYS = 1.0
"""The shortest mean gap between a customer's orders.

Not a modelling preference so much as a guard: a cadence of a tenth of a day
generates six thousand orders for one customer and a population that takes an
hour to write. A customer who orders more than once a day is a customer this
generator does not model, and saying so is better than discovering it.
"""

TOLERANCE = 1e-6
"""How far a distribution may miss summing to one before it is a mistake.

Wide enough for the rounding in a hand-written decimal, narrow enough that a
weight left out of a list is caught rather than absorbed.
"""


@dataclass(frozen=True, slots=True)
class Distribution:
    """A discrete distribution over integers — hours of the day, in practice.

    Stored as parallel sequences rather than a 24-long vector of mostly zeros,
    because a lunch distribution written as six hours and six weights is a
    thing a reader can check against their own experience of a lunch rush.

    Attributes:
        values: The values with mass on them, ascending.
        weights: Their probabilities, summing to one.
    """

    values: tuple[int, ...]
    weights: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class PersonaSpec:
    """One archetype: how a customer of this kind behaves over eighteen months.

    Every field is a behaviour the population would not otherwise exhibit, and
    PRD section 09's constraint — "behaviour that the synthetic population
    doesn't exhibit cannot be demonstrated" — is the argument for each one
    being tunable rather than assumed.

    Attributes:
        persona_id: Stable identifier; the key ``demo_visitors.persona_id``
            carries.
        label: What the demo calls this archetype out loud.
        narrative: One sentence a visitor is shown when assigned this persona.
        share: Fraction of the population. Shares sum to one across the file.
        seed_points: Loyalty points the archetype starts with.
        cadence_days: Mean days between orders. At least
            :data:`MINIMUM_CADENCE_DAYS`.
        cadence_spread: Log-normal spread on that gap. Zero is a metronome.
        active_from_share: Where in the window this customer's history starts,
            as a fraction of it. A customer who joined recently starts late.
        active_until_share: Where it stops. Below one is a lapse — the
            customer who stopped four months ago is ``1 - 4/18``.
        usual_order_fidelity: Probability that an order is the customer's
            usual, repeated exactly. This is the Tuesday regular.
        weekday_fidelity: Probability that an order is moved onto the
            customer's own day rather than falling where the cadence put it.
        preferred_weekdays: Indices into :data:`WEEKDAY_NAMES` that a customer
            of this archetype may keep to. Each customer draws *one* of them
            and keeps it for eighteen months, weighted by
            :attr:`TimingConfig.day_of_week_weights`; empty means the
            archetype has no fixed day and :attr:`weekday_fidelity` never
            applies. Per customer rather than per archetype on purpose — an
            archetype whose eighty members all order on Tuesday makes Tuesday
            a third of the whole population's traffic, which is a generator
            showing through rather than a regular.
        lunch_share: Probability an order happens at lunch rather than dinner.
        entrees_min: Fewest entrees in a basket.
        entrees_max: Most. Above one is a group order.
        toppings_min: Fewest optional toppings on an entree.
        toppings_max: Most.
        side_probability: Probability a basket carries a side per entree.
        drink_probability: Probability it carries a drink per entree.
        extra_probability: Probability an entree takes a published extra
            portion.
        store_loyalty: Probability an order happens at the customer's home
            store rather than another.
        delivery_share: Probability an order is priced at delivery prices.
    """

    persona_id: str
    label: str
    narrative: str
    share: float
    seed_points: int
    cadence_days: float
    cadence_spread: float
    active_from_share: float
    active_until_share: float
    usual_order_fidelity: float
    weekday_fidelity: float
    preferred_weekdays: tuple[int, ...]
    lunch_share: float
    entrees_min: int
    entrees_max: int
    toppings_min: int
    toppings_max: int
    side_probability: float
    drink_probability: float
    extra_probability: float
    store_loyalty: float
    delivery_share: float


@dataclass(frozen=True, slots=True)
class TimingConfig:
    """When orders land: the shape of a week, a day and a year.

    Attributes:
        day_of_week_weights: Seven weights, Monday first, summing to one.
            A cadence that lands on a Sunday is redistributed by these.
        month_weights: Twelve multipliers, January first, around one. This is
            the seasonal drift: a month above one shortens the gap between
            orders, a month below one lengthens it.
        weekday_lunch: Hour distribution for a weekday lunch.
        weekday_dinner: Hour distribution for a weekday dinner.
        weekend_lunch: Hour distribution for a weekend lunch.
        weekend_dinner: Hour distribution for a weekend dinner.
        weekday_shift_days: How far an order may be moved to land on a
            better-weighted day.
        default_opens: Opening time assumed for a store that publishes none.
        default_closes: Closing time for the same.
        last_order_minutes: Minutes before closing after which an order is
            pulled back inside the published hours.
        store_timezones: ``(region, IANA zone)`` pairs, sorted. The locator
            publishes opening hours with no zone on them; they are local
            times, and both "lunch" and "inside opening hours" are claims
            about local time, so the region has to name one.
        default_timezone: The zone for a store whose region is unpublished or
            absent from the table.
    """

    day_of_week_weights: tuple[float, ...]
    month_weights: tuple[float, ...]
    weekday_lunch: Distribution
    weekday_dinner: Distribution
    weekend_lunch: Distribution
    weekend_dinner: Distribution
    weekday_shift_days: int
    default_opens: str
    default_closes: str
    last_order_minutes: int
    store_timezones: tuple[tuple[str, str], ...]
    default_timezone: str

    def zone(self, region: str | None) -> ZoneInfo:
        """Return the timezone a store in ``region`` keeps its hours in.

        Args:
            region: A published ``stores.region``, or ``None``.

        Returns:
            The zone named for that region, or :attr:`default_timezone`.
        """
        for name, key in self.store_timezones:
            if name == region:
                return ZoneInfo(key)
        return ZoneInfo(self.default_timezone)


@dataclass(frozen=True, slots=True)
class CatalogueConfig:
    """Which catalogue rows this generator considers orderable, and how.

    The catalogue describes everything Chipotle publishes, including rows that
    are not things a customer puts in a basket — a modifier's own row, a bag of
    cutlery. Naming the categories here rather than in code means a catalogue
    whose published category names change is a config edit.

    Attributes:
        entree_category: ``menu_items.category`` marking a composed entree.
        side_categories: Categories orderable alongside an entree as a side.
        drink_categories: Categories orderable alongside one as a drink.
        excluded_categories: Categories never ordered — cutlery and napkins
            are published, orderable in principle, and noise in a basket.
        extra_portion_modifier_type: ``modifiers.modifier_type`` marking a
            paid extra portion rather than an ordinary slot choice.
        required_slots: Slots where the catalogue publishes exactly one choice
            being required, and the generator therefore always picks one.
        optional_slots: Slots the generator picks a variable number from.
    """

    entree_category: str
    side_categories: tuple[str, ...]
    drink_categories: tuple[str, ...]
    excluded_categories: tuple[str, ...]
    extra_portion_modifier_type: str
    required_slots: tuple[str, ...]
    optional_slots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrderConfig:
    """What happens to an order once it is composed.

    Attributes:
        statuses: The statuses an order may end in.
        status_weights: Their probabilities, summing to one.
        settled_statuses: The statuses that earn loyalty points. A refunded
            order is real history and earns nothing.
    """

    statuses: tuple[str, ...]
    status_weights: tuple[float, ...]
    settled_statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LoyaltyConfig:
    """The rewards arithmetic, provisionally.

    Issue #27 reconciles these against Chipotle's published rewards terms,
    which the policy harvest already carries. Until it does they are stated
    here as parameters rather than asserted anywhere as facts, and
    ``loyalty_ledger`` says which order every entry came from so that the
    reconciliation is a join rather than a regeneration.

    Attributes:
        points_per_dollar: Points earned per dollar of a settled order.
        redemption_threshold: Points a redemption costs.
        seed_reason: ``loyalty_ledger.reason`` for a persona's opening balance.
        earn_reason: The reason for points earned on an order.
        redeem_reason: The reason for a redemption.
        redemption_probability: Probability a customer redeems once they can.
    """

    points_per_dollar: int
    redemption_threshold: int
    seed_reason: str
    earn_reason: str
    redeem_reason: str
    redemption_probability: float


@dataclass(frozen=True, slots=True)
class NameConfig:
    """The pools display names and stated preferences are drawn from.

    There is no PII in this system by construction (RFC-001 section 02), so
    these are invented words combined by seed rather than sampled from
    anything. They are config because the population is a product surface: a
    demo whose five hundred customers are all called Alex reads as a demo.

    Attributes:
        given: Given names.
        family: Family names.
        stated_preferences: Free-text preferences a visitor may have stated.
        stated_preference_share: Fraction of customers who stated one.
        home_store_override_share: Fraction who overrode their home store.
    """

    given: tuple[str, ...]
    family: tuple[str, ...]
    stated_preferences: tuple[str, ...]
    stated_preference_share: float
    home_store_override_share: float


@dataclass(frozen=True, slots=True)
class GeneratorConfig:
    """The whole tuned population, read from one file.

    Attributes:
        seed: The seed. Same seed, same population, byte for byte.
        customers: How many synthetic customers to mint.
        stores: How many of the catalogue's stores the population orders from.
        months: How many months of history to generate.
        ends_at: The instant the history ends. Fixed rather than "now",
            because a generator whose output depends on the wall clock is not
            reproducible whatever its seed says.
        store_popularity_exponent: How unevenly traffic spreads over stores.
            Zero is uniform; higher concentrates it on the busiest.
        store_popularity_offset: Softens that curve's head so the busiest
            store is busy rather than the only one.
        window_jitter_days: How far a customer's first and last order may
            slide from where their archetype's shares put them. Without it
            every lapsed customer lapses on the same afternoon, which reads as
            a generator rather than as a population.
        palate_concentration: How sharply one customer prefers some catalogue
            items over others. This is a Dirichlet concentration: below one
            gives most customers a few favourites, at one they have none, and
            a population with no favourites has nothing for the
            personalization lane to find.
        personas: The archetypes, in file order.
        timing: When orders land.
        catalogue: Which catalogue rows are orderable.
        orders: What happens to an order.
        loyalty: The rewards arithmetic.
        names: The display-name pools.
    """

    seed: int
    customers: int
    stores: int
    months: int
    ends_at: datetime
    store_popularity_exponent: float
    store_popularity_offset: float
    window_jitter_days: int
    palate_concentration: float
    personas: tuple[PersonaSpec, ...]
    timing: TimingConfig
    catalogue: CatalogueConfig
    orders: OrderConfig
    loyalty: LoyaltyConfig
    names: NameConfig

    def persona(self, persona_id: str) -> PersonaSpec:
        """Return one archetype by identifier.

        Args:
            persona_id: The identifier.

        Returns:
            The archetype.

        Raises:
            KeyError: If no archetype has that identifier.
        """
        for spec in self.personas:
            if spec.persona_id == persona_id:
                return spec
        raise KeyError(f"no persona {persona_id!r}")


def load_config(path: Path | None = None) -> GeneratorConfig:
    """Read and validate the generation parameters.

    Args:
        path: A TOML file, or ``None`` for the one shipped in the package.

    Returns:
        The validated parameters.

    Raises:
        ConfigError: If the file is not readable TOML, if a required key is
            missing, or if any number in it does not describe a population
            that can exist.
    """
    text = _read(path)
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        where = path if path is not None else PACKAGED_CONFIG
        raise ConfigError(f"{where} is not readable TOML: {error}") from error
    return _config(raw)


def _read(path: Path | None) -> str:
    """Return the config text, from a path or from the packaged resource."""
    if path is None:
        return (
            resources.files("chip_chat.data_gen")
            .joinpath(PACKAGED_CONFIG)
            .read_text(encoding="utf-8")
        )
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read {path}: {error}") from error


def _config(raw: Mapping[str, Any]) -> GeneratorConfig:
    """Build the validated record out of the parsed TOML."""
    population = _table(raw, "population")
    personas = tuple(_persona(entry, index) for index, entry in enumerate(_personas(raw)))
    _sums_to_one("population.personas share", [spec.share for spec in personas])
    _unique("population.personas persona_id", [spec.persona_id for spec in personas])

    customers = _count("population.customers", population, minimum=1)
    if customers < len(personas):
        raise ConfigError(
            f"population.customers is {customers} but there are {len(personas)} "
            "personas; every archetype must reach at least one customer or the "
            "behaviour it exists to demonstrate is not in the population"
        )

    return GeneratorConfig(
        seed=_count("population.seed", population, minimum=0),
        customers=customers,
        stores=_count("population.stores", population, minimum=1),
        months=_count("population.months", population, minimum=1),
        ends_at=_instant("population.ends_at", population),
        store_popularity_exponent=_number(
            "population.store_popularity_exponent", population, minimum=0.0
        ),
        store_popularity_offset=_number(
            "population.store_popularity_offset", population, minimum=TOLERANCE
        ),
        window_jitter_days=_count("population.window_jitter_days", population, minimum=0),
        palate_concentration=_number(
            "population.palate_concentration", population, minimum=TOLERANCE
        ),
        personas=personas,
        timing=_timing(_table(raw, "timing")),
        catalogue=_catalogue(_table(raw, "catalogue")),
        orders=_orders(_table(raw, "orders")),
        loyalty=_loyalty(_table(raw, "loyalty")),
        names=_names(_table(raw, "names")),
    )


def _personas(raw: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    """Return the persona tables, or raise if there are none."""
    entries = raw.get("personas")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("personas must be a non-empty array of tables")
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError("every entry in personas must be a table")
    return entries


def _persona(entry: Mapping[str, Any], index: int) -> PersonaSpec:
    """Build one archetype, naming the entry by index when a key is missing."""
    where = f"personas[{index}]"
    entrees_min = _count(f"{where}.entrees_min", entry, minimum=1)
    entrees_max = _count(f"{where}.entrees_max", entry, minimum=entrees_min)
    toppings_min = _count(f"{where}.toppings_min", entry, minimum=0)
    toppings_max = _count(f"{where}.toppings_max", entry, minimum=toppings_min)
    active_from = _fraction(f"{where}.active_from_share", entry)
    active_until = _fraction(f"{where}.active_until_share", entry)
    if active_until <= active_from:
        raise ConfigError(
            f"{where}.active_until_share ({active_until}) must be after "
            f"active_from_share ({active_from}); a customer whose window is "
            "empty contributes nothing but a row"
        )
    return PersonaSpec(
        persona_id=_text(f"{where}.persona_id", entry),
        label=_text(f"{where}.label", entry),
        narrative=_text(f"{where}.narrative", entry),
        share=_fraction(f"{where}.share", entry),
        seed_points=_count(f"{where}.seed_points", entry, minimum=0),
        cadence_days=_number(
            f"{where}.cadence_days", entry, minimum=MINIMUM_CADENCE_DAYS
        ),
        cadence_spread=_number(f"{where}.cadence_spread", entry, minimum=0.0),
        active_from_share=active_from,
        active_until_share=active_until,
        usual_order_fidelity=_fraction(f"{where}.usual_order_fidelity", entry),
        weekday_fidelity=_fraction(f"{where}.weekday_fidelity", entry),
        preferred_weekdays=_weekdays(f"{where}.preferred_weekdays", entry),
        lunch_share=_fraction(f"{where}.lunch_share", entry),
        entrees_min=entrees_min,
        entrees_max=entrees_max,
        toppings_min=toppings_min,
        toppings_max=toppings_max,
        side_probability=_fraction(f"{where}.side_probability", entry),
        drink_probability=_fraction(f"{where}.drink_probability", entry),
        extra_probability=_fraction(f"{where}.extra_probability", entry),
        store_loyalty=_fraction(f"{where}.store_loyalty", entry),
        delivery_share=_fraction(f"{where}.delivery_share", entry),
    )


def _timing(raw: Mapping[str, Any]) -> TimingConfig:
    """Build the timing parameters."""
    days = _weights("timing.day_of_week_weights", raw, length=len(WEEKDAY_NAMES))
    _sums_to_one("timing.day_of_week_weights", days)
    months = _weights("timing.month_weights", raw, length=12)
    zones, default_zone = _zones(_table(raw, "store_timezones"))
    return TimingConfig(
        day_of_week_weights=tuple(days),
        month_weights=tuple(months),
        weekday_lunch=_distribution("timing.weekday_lunch", raw),
        weekday_dinner=_distribution("timing.weekday_dinner", raw),
        weekend_lunch=_distribution("timing.weekend_lunch", raw),
        weekend_dinner=_distribution("timing.weekend_dinner", raw),
        weekday_shift_days=_count("timing.weekday_shift_days", raw, minimum=0),
        default_opens=_clock("timing.default_opens", raw),
        default_closes=_clock("timing.default_closes", raw),
        last_order_minutes=_count("timing.last_order_minutes", raw, minimum=0),
        store_timezones=zones,
        default_timezone=default_zone,
    )


def _distribution(where: str, raw: Mapping[str, Any]) -> Distribution:
    """Build one hour distribution from its parallel hours and weights."""
    table = _table(raw, where.split(".")[-1])
    hours = _integers(f"{where}.hours", table)
    weights = _weights(f"{where}.weights", table, length=len(hours))
    if sorted(hours) != list(hours):
        raise ConfigError(f"{where}.hours must ascend, got {list(hours)}")
    for hour in hours:
        if not 0 <= hour <= 23:
            raise ConfigError(f"{where}.hours has {hour}, which is not an hour")
    _sums_to_one(f"{where}.weights", weights)
    return Distribution(values=tuple(hours), weights=tuple(weights))


def _zones(raw: Mapping[str, Any]) -> tuple[tuple[tuple[str, str], ...], str]:
    """Return the region-to-zone table and the default, both validated.

    Every value is resolved through :class:`~zoneinfo.ZoneInfo` here rather
    than at the first order that needs it, so a typo in a state's zone is a
    config error and not a generator that ran for four hundred customers and
    then stopped.
    """
    default = _text("timing.store_timezones.default", raw)
    pairs = tuple(
        sorted(
            (region, key)
            for region, key in raw.items()
            if region != "default"
            if isinstance(key, str)
        )
    )
    unnamed = sorted(k for k, v in raw.items() if not isinstance(v, str))
    if unnamed:
        raise ConfigError(
            f"timing.store_timezones must map every region to a string, "
            f"and {unnamed} is not one"
        )
    for region, key in (*pairs, ("default", default)):
        try:
            ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ConfigError(
                f"timing.store_timezones.{region} is {key!r}, which is not a "
                f"timezone: {error}"
            ) from error
    return pairs, default


def _catalogue(raw: Mapping[str, Any]) -> CatalogueConfig:
    """Build the catalogue-shape parameters."""
    return CatalogueConfig(
        entree_category=_text("catalogue.entree_category", raw),
        side_categories=_texts("catalogue.side_categories", raw),
        drink_categories=_texts("catalogue.drink_categories", raw),
        excluded_categories=_texts("catalogue.excluded_categories", raw),
        extra_portion_modifier_type=_text("catalogue.extra_portion_modifier_type", raw),
        required_slots=_texts("catalogue.required_slots", raw),
        optional_slots=_texts("catalogue.optional_slots", raw),
    )


def _orders(raw: Mapping[str, Any]) -> OrderConfig:
    """Build the order-status parameters."""
    statuses = _texts("orders.statuses", raw)
    weights = _weights("orders.status_weights", raw, length=len(statuses))
    _sums_to_one("orders.status_weights", weights)
    _unique("orders.statuses", list(statuses))
    settled = _texts("orders.settled_statuses", raw)
    unknown = sorted(set(settled) - set(statuses))
    if unknown:
        raise ConfigError(
            f"orders.settled_statuses names {unknown}, which orders.statuses "
            "does not contain; points would be earned by a status no order "
            "can reach"
        )
    return OrderConfig(
        statuses=statuses,
        status_weights=tuple(weights),
        settled_statuses=settled,
    )


def _loyalty(raw: Mapping[str, Any]) -> LoyaltyConfig:
    """Build the rewards parameters."""
    return LoyaltyConfig(
        points_per_dollar=_count("loyalty.points_per_dollar", raw, minimum=1),
        redemption_threshold=_count("loyalty.redemption_threshold", raw, minimum=1),
        seed_reason=_text("loyalty.seed_reason", raw),
        earn_reason=_text("loyalty.earn_reason", raw),
        redeem_reason=_text("loyalty.redeem_reason", raw),
        redemption_probability=_fraction("loyalty.redemption_probability", raw),
    )


def _names(raw: Mapping[str, Any]) -> NameConfig:
    """Build the display-name parameters."""
    given = _texts("names.given", raw)
    family = _texts("names.family", raw)
    if not given or not family:
        raise ConfigError("names.given and names.family must both be non-empty")
    return NameConfig(
        given=given,
        family=family,
        stated_preferences=_texts("names.stated_preferences", raw),
        stated_preference_share=_fraction("names.stated_preference_share", raw),
        home_store_override_share=_fraction("names.home_store_override_share", raw),
    )


def _table(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """Return one sub-table, or raise naming it."""
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table")
    return value


def _text(where: str, raw: Mapping[str, Any]) -> str:
    """Return one string, or raise naming it."""
    value = raw.get(where.split(".")[-1])
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where} must be a non-empty string, got {value!r}")
    return value


def _texts(where: str, raw: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a tuple of strings, or raise naming it."""
    value = raw.get(where.split(".")[-1])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{where} must be an array of strings, got {value!r}")
    return tuple(value)


def _count(where: str, raw: Mapping[str, Any], minimum: int) -> int:
    """Return one integer at or above ``minimum``, or raise naming it."""
    value = raw.get(where.split(".")[-1])
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{where} must be an integer, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{where} must be at least {minimum}, got {value}")
    return value


def _number(where: str, raw: Mapping[str, Any], minimum: float) -> float:
    """Return one float at or above ``minimum``, or raise naming it."""
    value = raw.get(where.split(".")[-1])
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    if value < minimum:
        raise ConfigError(f"{where} must be at least {minimum}, got {value}")
    return float(value)


def _fraction(where: str, raw: Mapping[str, Any]) -> float:
    """Return one probability, or raise naming it."""
    value = _number(where, raw, minimum=0.0)
    if value > 1.0:
        raise ConfigError(f"{where} must be a probability, got {value}")
    return value


def _integers(where: str, raw: Mapping[str, Any]) -> tuple[int, ...]:
    """Return a non-empty tuple of integers, or raise naming it."""
    value = raw.get(where.split(".")[-1])
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} must be a non-empty array of integers")
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise ConfigError(f"{where} must hold integers, got {item!r}")
    return tuple(value)


def _weights(where: str, raw: Mapping[str, Any], length: int) -> tuple[float, ...]:
    """Return ``length`` non-negative weights, or raise naming it."""
    value = raw.get(where.split(".")[-1])
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be an array of numbers, got {value!r}")
    if len(value) != length:
        raise ConfigError(f"{where} must hold {length} numbers, got {len(value)}")
    weights = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise ConfigError(f"{where} must hold numbers, got {item!r}")
        if item < 0:
            raise ConfigError(f"{where} must hold non-negative numbers, got {item}")
        weights.append(float(item))
    if sum(weights) <= 0:
        raise ConfigError(f"{where} has no mass in it; nothing could be chosen")
    return tuple(weights)


def _weekdays(where: str, raw: Mapping[str, Any]) -> tuple[int, ...]:
    """Return the weekday indices an archetype keeps to, possibly none."""
    value = raw.get(where.split(".")[-1])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(
            f"{where} must be an array of day names or absent, got {value!r}"
        )
    days = []
    for name in value:
        if not isinstance(name, str) or name not in WEEKDAY_NAMES:
            raise ConfigError(
                f"{where} must hold names from {list(WEEKDAY_NAMES)}, got {name!r}"
            )
        index = WEEKDAY_NAMES.index(name)
        if index in days:
            raise ConfigError(f"{where} repeats {name!r}")
        days.append(index)
    return tuple(days)


def _clock(where: str, raw: Mapping[str, Any]) -> str:
    """Return an ``HH:MM`` string, or raise naming it."""
    value = _text(where, raw)
    hour, _, minute = value.partition(":")
    if not (hour.isdigit() and minute.isdigit() and len(minute) == 2):
        raise ConfigError(f"{where} must be HH:MM, got {value!r}")
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise ConfigError(f"{where} must be a time of day, got {value!r}")
    return value


def _instant(where: str, raw: Mapping[str, Any]) -> datetime:
    """Return one timezone-aware instant, or raise naming it."""
    value = _text(where, raw)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f"{where} must be an ISO 8601 instant: {error}") from error
    if parsed.tzinfo is None:
        raise ConfigError(
            f"{where} must carry a timezone; a naive instant means a different "
            "population on a different machine"
        )
    return parsed.astimezone(UTC)


def _sums_to_one(where: str, weights: Sequence[float]) -> None:
    """Raise unless the weights sum to one within :data:`TOLERANCE`."""
    total = sum(weights)
    if abs(total - 1.0) > TOLERANCE:
        raise ConfigError(f"{where} must sum to 1.0, got {total}")


def _unique(where: str, values: Sequence[str]) -> None:
    """Raise if any value appears twice."""
    seen: list[str] = []
    for value in values:
        if value in seen:
            raise ConfigError(f"{where} repeats {value!r}")
        seen.append(value)
