"""When an order happens, and why it is never three in the morning.

Issue #25 asks for "order timestamps with realistic texture: weekday lunch
peaks, weekend differences, seasonal drift". All three are claims about *local*
time, and the catalogue's opening hours are published in local time with no
zone attached, so this module works in the store's zone throughout and hands
back UTC at the end. ``timing.store_timezones`` is where the zone comes from,
and its approximations are named there rather than here.

Four things shape a timestamp, in this order:

**Cadence.** A customer's gap to their next order is their archetype's mean,
stretched by a log-normal jitter and divided by the month's weight — which is
the seasonal drift, applied as a change to how often people order rather than
as a multiplier bolted onto a count afterwards.

**The week.** The day the cadence landed on is moved, within a few days, onto
a better-weighted weekday. For a customer who keeps to one day this is the
Tuesday regular being a Tuesday regular; for one who does not, it is Friday
being busier than Sunday.

**The meal.** Lunch or dinner by the archetype's ``lunch_share``, then an hour
from the matching distribution — a different one at weekends, which is where
"weekend differences" actually lives.

**The store.** The result is folded into that store's published opening hours
for that day. Folded rather than clamped: clamping would pile every early
order onto the minute the doors open, and a spike at 10:45 is a tell.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from random import Random

from chip_chat.catalog import Store
from chip_chat.data_gen.config import (
    WEEKDAY_NAMES,
    Distribution,
    PersonaSpec,
    TimingConfig,
)
from chip_chat.data_gen.rng import jitter, weighted_choice, weighted_index

MINUTES_PER_HOUR = 60


def visit_days(
    rng: Random,
    spec: PersonaSpec,
    timing: TimingConfig,
    starts_at: datetime,
    ends_at: datetime,
    preferred_weekday: int | None,
) -> Iterator[datetime]:
    """Yield the local dates this customer orders on, in order.

    Args:
        rng: This customer's cadence stream.
        spec: Their archetype.
        timing: The shape of a week and a year.
        starts_at: The first instant they could order.
        ends_at: The last.
        preferred_weekday: The day *this customer* keeps to, or ``None``.

    Yields:
        Midnight on each day they order, in the same zone as the arguments.
    """
    when = starts_at
    while True:
        gap = spec.cadence_days * jitter(rng, spec.cadence_spread)
        gap /= timing.month_weights[when.month - 1]
        when = when + timedelta(days=gap)
        if when >= ends_at:
            return
        moved = _onto_a_weekday(rng, spec, timing, when, preferred_weekday)
        if starts_at <= moved < ends_at:
            yield moved


def _onto_a_weekday(
    rng: Random,
    spec: PersonaSpec,
    timing: TimingConfig,
    when: datetime,
    preferred_weekday: int | None,
) -> datetime:
    """Move a date onto a better-weighted weekday, within the allowed shift."""
    if preferred_weekday is not None and rng.random() < spec.weekday_fidelity:
        target = preferred_weekday
    else:
        target = weighted_index(rng, timing.day_of_week_weights)
    offset = (target - when.weekday()) % len(WEEKDAY_NAMES)
    if offset > len(WEEKDAY_NAMES) // 2:
        offset -= len(WEEKDAY_NAMES)
    if abs(offset) > timing.weekday_shift_days:
        return when
    return when + timedelta(days=offset)


def time_of_day(
    rng: Random, spec: PersonaSpec, timing: TimingConfig, day: datetime
) -> datetime:
    """Return ``day`` with a meal-shaped hour and minute on it.

    Args:
        rng: This customer's timing stream.
        spec: Their archetype.
        timing: The hour distributions.
        day: The local day the order falls on.

    Returns:
        The same day, at an hour drawn from the lunch or dinner distribution
        for a weekday or a weekend as appropriate.
    """
    weekend = day.weekday() >= len(WEEKDAY_NAMES) - 2
    lunch = rng.random() < spec.lunch_share
    hours = _distribution(timing, weekend=weekend, lunch=lunch)
    hour = weighted_choice(rng, hours.values, hours.weights)
    minute = rng.randrange(MINUTES_PER_HOUR)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _distribution(timing: TimingConfig, weekend: bool, lunch: bool) -> Distribution:
    """Return the hour distribution for one kind of meal on one kind of day."""
    if weekend:
        return timing.weekend_lunch if lunch else timing.weekend_dinner
    return timing.weekday_lunch if lunch else timing.weekday_dinner


def inside_opening_hours(when: datetime, store: Store, timing: TimingConfig) -> datetime:
    """Fold a local timestamp into the store's published hours for that day.

    Args:
        when: The local timestamp, in the store's zone.
        store: The store, whose ``hours`` are consulted for that weekday.
        timing: Where the assumed hours and the last-order margin come from.

    Returns:
        A timestamp inside the store's serving window. Folded modulo the
        window rather than clamped to its edges, so an order drawn at nine in
        the morning lands somewhere in the day rather than on the stroke of
        opening along with every other early order.
    """
    opens, closes = _published(store, when, timing)
    last_order = closes - timedelta(minutes=timing.last_order_minutes)
    if last_order <= opens:
        return opens
    window = last_order - opens
    return opens + (when - opens) % window


def _published(
    store: Store, when: datetime, timing: TimingConfig
) -> tuple[datetime, datetime]:
    """Return the store's opening and closing instants on ``when``'s day.

    A store that publishes nothing for that day, or publishes it as closed,
    gets the configured assumed hours — stated rather than silently accepted,
    because an order at four in the morning in a store whose hours are merely
    missing is worse than an order inside an assumption a reader can see.
    """
    name = WEEKDAY_NAMES[when.weekday()]
    opens_text, closes_text = timing.default_opens, timing.default_closes
    for hours in store.hours:
        if hours.day_of_week != name:
            continue
        if hours.is_published and hours.opens is not None and hours.closes is not None:
            opens_text, closes_text = hours.opens, hours.closes
        break
    opens = _at(when, opens_text)
    closes = _at(when, closes_text)
    if closes <= opens:
        closes += timedelta(days=1)
    return opens, closes


def _at(day: datetime, clock: str) -> datetime:
    """Return ``day`` at the ``HH:MM`` given, validated by the config reader."""
    hour, _, minute = clock.partition(":")
    return day.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)


def placed_at(
    rng: Random,
    spec: PersonaSpec,
    timing: TimingConfig,
    day: datetime,
    store: Store,
) -> datetime:
    """Return one order's instant in UTC, having chosen it in the store's zone.

    The cadence walks in the customer's *home* store's zone, because a
    customer lives somewhere; an order placed at another store keeps the
    calendar day and takes that store's zone and opening hours. Which is what
    "I got lunch on the Thursday, but in Denver" means.

    Args:
        rng: This customer's timing stream.
        spec: Their archetype.
        timing: The hour distributions and opening-hour assumptions.
        day: The local day the cadence landed on, in the home store's zone.
        store: Where this order happens.

    Returns:
        The instant, in UTC.
    """
    here = datetime(day.year, day.month, day.day, tzinfo=timing.zone(store.region))
    local = time_of_day(rng, spec, timing, here)
    return inside_opening_hours(local, store, timing).astimezone(UTC)
