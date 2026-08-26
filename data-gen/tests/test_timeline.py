"""That a timestamp lands where a meal would, in the store's own time.

The catalogue publishes opening hours as local times with no zone attached, so
every claim in issue #25's "realistic texture" — the lunch peak, the weekend
difference — is a claim about local time. These tests are the unit-level half
of that; ``test_texture.py`` asserts the same shapes on the whole population.

The folding behaviour is the one worth reading. An order drawn at nine in the
morning has to end up inside the serving window, and clamping it to the
opening minute would put every early order in the population on the stroke of
10:45. Folding spreads them, and the test below is what says so.
"""

import dataclasses
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from population_fixtures import fixture_catalog, shipped_config

from chip_chat.catalog import StoreHours
from chip_chat.data_gen.rng import substream
from chip_chat.data_gen.timeline import (
    inside_opening_hours,
    placed_at,
    time_of_day,
    visit_days,
)

PACIFIC = ZoneInfo("America/Los_Angeles")


def a_store():
    """Return the fixture catalogue's reference store."""
    catalog = fixture_catalog()
    return next(
        store
        for store in catalog.stores
        if store.store_id == catalog.reference_restaurant_id
    )


def test_an_order_before_opening_is_folded_into_the_day() -> None:
    """Not clamped to the opening minute, which would be a visible spike."""
    store = a_store()
    timing = shipped_config().timing
    early = datetime(2026, 3, 4, 6, 20, tzinfo=PACIFIC)

    folded = inside_opening_hours(early, store, timing)

    assert folded.date() == early.date()
    assert folded.hour >= 10
    assert folded != early.replace(hour=10, minute=45)


def test_an_order_after_closing_is_folded_back_before_last_orders() -> None:
    store = a_store()
    timing = shipped_config().timing
    late = datetime(2026, 3, 4, 23, 50, tzinfo=PACIFIC)

    folded = inside_opening_hours(late, store, timing)

    assert folded < late.replace(hour=23, minute=0)


def test_an_order_already_inside_the_window_is_left_alone() -> None:
    store = a_store()
    timing = shipped_config().timing
    lunch = datetime(2026, 3, 4, 12, 34, tzinfo=PACIFIC)

    assert inside_opening_hours(lunch, store, timing) == lunch


def test_a_store_that_publishes_no_hours_gets_the_stated_assumption() -> None:
    """Stated rather than silently accepted; an order at 4am is worse."""
    timing = shipped_config().timing
    silent = dataclasses.replace(
        a_store(),
        hours=tuple(
            StoreHours(
                day_of_week=row.day_of_week, opens=None, closes=None, is_published=False
            )
            for row in a_store().hours
        ),
    )
    dawn = datetime(2026, 3, 4, 4, 0, tzinfo=PACIFIC)

    folded = inside_opening_hours(dawn, silent, timing)

    assert f"{folded.hour:02d}:{folded.minute:02d}" >= timing.default_opens
    assert folded.hour < int(timing.default_closes.split(":")[0])


def test_the_hour_comes_from_the_meal_and_the_kind_of_day() -> None:
    """Lunch on a weekday and dinner at a weekend are different distributions."""
    config = shipped_config()
    always_lunch = dataclasses.replace(config.personas[0], lunch_share=1.0)
    never_lunch = dataclasses.replace(config.personas[0], lunch_share=0.0)
    weekday = datetime(2026, 3, 4, tzinfo=PACIFIC)

    lunches = {
        time_of_day(
            substream(1, "hour", index), always_lunch, config.timing, weekday
        ).hour
        for index in range(200)
    }
    dinners = {
        time_of_day(substream(1, "hour", index), never_lunch, config.timing, weekday).hour
        for index in range(200)
    }

    assert lunches <= set(config.timing.weekday_lunch.values)
    assert dinners <= set(config.timing.weekday_dinner.values)
    assert lunches & dinners == set()


def test_a_customer_who_keeps_to_a_day_keeps_to_it() -> None:
    """And one who does not follows the week's own weights."""
    config = shipped_config()
    spec = dataclasses.replace(config.personas[0], weekday_fidelity=1.0)
    start = datetime(2025, 1, 6, tzinfo=PACIFIC)
    end = datetime(2026, 1, 6, tzinfo=PACIFIC)

    fixed = list(visit_days(substream(2, "walk"), spec, config.timing, start, end, 2))
    free = list(visit_days(substream(2, "walk"), spec, config.timing, start, end, None))

    assert fixed
    assert sum(day.weekday() == 2 for day in fixed) / len(fixed) > 0.9
    assert len({day.weekday() for day in free}) == 7


def test_the_walk_stays_inside_the_window() -> None:
    """Eighteen months of history means eighteen months of history."""
    config = shipped_config()
    start = datetime(2025, 1, 6, tzinfo=PACIFIC)
    end = datetime(2025, 4, 6, tzinfo=PACIFIC)

    days = list(
        visit_days(
            substream(3, "walk"), config.personas[0], config.timing, start, end, None
        )
    )

    assert days
    assert all(start <= day < end for day in days)


def test_an_order_at_another_store_keeps_the_day_and_takes_its_hours() -> None:
    """ "I got lunch on the Thursday, but in Denver"."""
    config = shipped_config()
    catalog = fixture_catalog()
    elsewhere = next(store for store in catalog.stores if store.region == "CO")
    day = datetime(2026, 3, 5, tzinfo=PACIFIC)

    when = placed_at(
        substream(4, "order"), config.personas[0], config.timing, day, elsewhere
    )

    local = when.astimezone(config.timing.zone(elsewhere.region))
    assert when.tzinfo is UTC
    assert local.date() == day.date()
    assert 6 <= local.hour <= 23
