"""Choosing stores from the published sitemap, and reading a store's page.

Two properties matter here and both are issue #21's. The choice of *which*
stores has to be the same on every run, or nothing downstream reproduces; and
the restaurant the menu harvest priced has to be among them, or a harvested
price has no address to belong to.
"""

import chipotle_fixtures as site
import pytest

from chip_chat.harvest.sources.chipotle import (
    REFERENCE_STORE_URL,
    ChipotleSourceError,
    parse_opening_hours,
    parse_store_page,
    select_store_urls,
    store_page_urls,
)
from chip_chat.harvest.sources.chipotle.locator import LOCATOR_URL

SITEMAP = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{LOCATOR_URL}</loc></url>
  <url><loc>{LOCATOR_URL}404.html</loc></url>
  <url><loc>{LOCATOR_URL}wa</loc></url>
  <url><loc>{LOCATOR_URL}wa/seattle</loc></url>
  <url><loc>{LOCATOR_URL}wa/seattle/1600-nw-market-st</loc></url>
  <url><loc>{LOCATOR_URL}wa/seattle/1600-nw-market-st/order-delivery</loc></url>
  <url><loc>{LOCATOR_URL}ca/lakewood/5310-lakewood-blvd</loc></url>
  <url><loc>{LOCATOR_URL}ca/alameda/2314-s-shore-ctr</loc></url>
</urlset>
"""


def store_page(**overrides: object) -> str:
    """Return one generated locator page, with fields overridden."""
    store = dict(site.REFERENCE_STORE)
    store.update(overrides)
    return site.locator_page(store)


def test_only_the_store_pages_come_out_of_a_sitemap() -> None:
    urls = store_page_urls([SITEMAP])

    assert urls == (
        f"{LOCATOR_URL}ca/alameda/2314-s-shore-ctr",
        f"{LOCATOR_URL}ca/lakewood/5310-lakewood-blvd",
        f"{LOCATOR_URL}wa/seattle/1600-nw-market-st",
    )


def test_the_reference_restaurant_is_always_chosen_first() -> None:
    chosen = select_store_urls(store_page_urls([SITEMAP]), 2)

    assert chosen[0] == REFERENCE_STORE_URL


def test_the_selection_takes_one_state_at_a_time() -> None:
    """Three pages, two states: the second pick is the state the first was not."""
    chosen = select_store_urls(store_page_urls([SITEMAP]), 3)

    assert chosen == (
        f"{LOCATOR_URL}ca/lakewood/5310-lakewood-blvd",
        f"{LOCATOR_URL}ca/alameda/2314-s-shore-ctr",
        f"{LOCATOR_URL}wa/seattle/1600-nw-market-st",
    )


def test_the_same_sitemap_always_chooses_the_same_stores() -> None:
    assert select_store_urls(store_page_urls([SITEMAP]), 3) == select_store_urls(
        store_page_urls([SITEMAP]), 3
    )


def test_a_sitemap_without_the_reference_restaurant_raises() -> None:
    sitemap = SITEMAP.replace(f"{LOCATOR_URL}ca/lakewood/5310-lakewood-blvd", "")

    with pytest.raises(ChipotleSourceError, match="reference restaurant"):
        select_store_urls(store_page_urls([sitemap]), 2)


def test_asking_for_more_stores_than_the_sitemap_lists_raises() -> None:
    with pytest.raises(ChipotleSourceError, match="fewer than the 9 asked for"):
        select_store_urls(store_page_urls([SITEMAP]), 9)


def test_a_store_page_yields_the_restaurant_number_its_order_links_carry() -> None:
    page = parse_store_page(store_page(), REFERENCE_STORE_URL)

    assert page.store_id == 679
    assert page.street_address == "5310 Lakewood Blvd"
    assert page.city == "Lakewood"
    assert page.region == "CA"
    assert page.postal_code == "90712"
    assert page.telephone == "+15627908786"
    assert (page.latitude, page.longitude) == (33.854146, -118.1419424)


def test_a_store_page_yields_a_day_at_a_time() -> None:
    page = parse_store_page(store_page(opening_hours=site.SPLIT_WEEK), "any")

    assert [(hours.day, hours.opens, hours.closes) for hours in page.hours] == [
        ("Monday", "10:45", "21:00"),
        ("Tuesday", "10:45", "21:00"),
        ("Wednesday", "10:45", "21:00"),
        ("Thursday", "10:45", "21:00"),
        ("Friday", "10:45", "21:00"),
        ("Saturday", "10:45", "20:00"),
        ("Sunday", "10:45", "20:00"),
    ]


def test_a_published_closure_is_a_day_with_no_times() -> None:
    page = parse_store_page(store_page(opening_hours=site.CLOSED_SUNDAY), "any")

    sunday = next(hours for hours in page.hours if hours.day == "Sunday")
    assert (sunday.opens, sunday.closes) == (None, None)


def test_a_day_nobody_published_is_absent_rather_than_closed() -> None:
    """The distinction ``store_hours`` exists to keep."""
    page = parse_store_page(store_page(opening_hours=site.NO_SUNDAY), "any")

    assert [hours.day for hours in page.hours] == [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
    ]


def test_opening_hours_this_source_cannot_read_raise() -> None:
    with pytest.raises(ChipotleSourceError, match="cannot read"):
        parse_opening_hours(["whenever we feel like it"], "any")


def test_an_unknown_day_code_raises() -> None:
    with pytest.raises(ChipotleSourceError, match="does not know"):
        parse_opening_hours(["Zz 10:45-23:00"], "any")


def test_a_page_with_no_restaurant_number_raises() -> None:
    html = store_page().replace("?restaurant=679", "")

    with pytest.raises(ChipotleSourceError, match="no restaurant number"):
        parse_store_page(html, "any")


def test_a_page_naming_two_restaurants_raises() -> None:
    html = store_page().replace(
        "burrito-bowl?restaurant=679", "burrito-bowl?restaurant=680"
    )

    with pytest.raises(ChipotleSourceError, match="more than one restaurant number"):
        parse_store_page(html, "any")


def test_a_page_with_no_restaurant_node_raises() -> None:
    html = store_page().replace('"@type":"Restaurant"', '"@type":"Thing"')

    with pytest.raises(ChipotleSourceError, match=r"no schema\.org Restaurant node"):
        parse_store_page(html, "any")
