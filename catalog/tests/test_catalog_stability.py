"""That an order composed against one harvest is not orphaned by the next.

Issue #24 asks for ``item_id`` stability across two harvests, demonstrated by
a test rather than asserted in a docstring. The demonstration has to be a
*second harvest* and not a second build of the first, because a build that is
merely deterministic proves nothing about what happens when the site changes.

So these tests harvest the fixture site twice, moving underneath the second
harvest the things that really move — the clock, the prices, which restaurants
were asked — and then check that identifiers minted by the first harvest still
resolve in the second.
"""

from datetime import UTC, datetime
from decimal import Decimal

from catalog_fixtures import catalog as build
from catalog_fixtures import chipotle, fixture_catalog

from chip_chat.harvest.testing import fake_response

A_WEEK_LATER = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)
"""When the second harvest happens. The re-harvest is weekly (RFC-001 s08)."""


def repriced(item_id: str, price: str) -> chipotle.FakeTransport:
    """The fixture site with one item repriced at the reference restaurant.

    A re-harvest that finds a new price is the ordinary case, and it is the
    one that would break identifiers derived from anything but the published
    identifier.
    """
    import json

    payload = json.loads(chipotle.read(f"onlinemenu-{chipotle.REFERENCE}.json"))
    for section in ("entrees", "sides", "drinks", "nonFoodItems"):
        for entry in payload.get(section, ()):
            if entry.get("itemId") == item_id:
                entry["unitPrice"] = float(price)
    url = chipotle.menu_url(chipotle.REFERENCE)
    return chipotle.site(extra={url: fake_response(url, json.dumps(payload).encode())})


def test_item_ids_survive_a_second_harvest() -> None:
    """Two harvests, a moved clock and a changed price; the same identifiers."""
    first = build()
    second = build(transport=repriced("CMG-2", "14.25"), start=A_WEEK_LATER)

    assert [row.item_id for row in first.menu_items] == [
        row.item_id for row in second.menu_items
    ]
    assert [row.modifier_id for row in first.modifiers] == [
        row.modifier_id for row in second.modifiers
    ]
    assert [row.store_id for row in first.stores] == [
        row.store_id for row in second.stores
    ]


def test_an_order_composed_against_the_first_harvest_still_resolves() -> None:
    """The property the stability criterion is actually protecting.

    Issue #25 composes synthetic orders from catalogue rows. If a re-harvest
    changed the identifiers, every order ever generated would point at nothing
    — and would do it silently, because an order is just a list of strings.
    """
    first = build()
    order = [
        (row.item_id, None) for row in first.menu_items if row.item_id == "CMG-1002"
    ] + [("CMG-2", "CMG-2:CMG-5051")]

    second = build(transport=repriced("CMG-2", "14.25"), start=A_WEEK_LATER)
    modifiers = {row.modifier_id for row in second.modifiers}
    for item_id, modifier_id in order:
        assert second.item(item_id) is not None, item_id
        if modifier_id is not None:
            assert modifier_id in modifiers


def test_a_changed_price_changes_both_versions() -> None:
    """A version that did not move when a price did would be worthless."""
    first = build()
    second = build(transport=repriced("CMG-2", "14.25"), start=A_WEEK_LATER)

    assert first.version() != second.version()
    assert first.content_version() != second.content_version()

    quoted = {
        row.item_id: row.unit_price
        for row in second.item_prices
        if row.restaurant_id == 679
    }
    assert quoted["CMG-2"] == Decimal("14.25")


def test_two_builds_of_one_harvest_are_byte_identical() -> None:
    """Reproducibility, which the versions are only meaningful on top of."""
    menu_first = build()
    menu_second = build()

    assert menu_first.version() == menu_second.version()
    assert menu_first.manifest() == menu_second.manifest()


def test_the_content_version_ignores_when_it_was_read() -> None:
    """The version issue #25 records against a batch of generated orders.

    ``catalog_version`` moves when the harvest is repeated because it is a
    different harvest. ``content_version`` moves only when what is orderable
    changes, which is the question "are these orders still valid" is really
    asking.
    """
    catalog = fixture_catalog()
    later = build(start=A_WEEK_LATER)

    assert later.version() != catalog.version()
    assert later.content_version() == catalog.content_version()
    assert "content_version" in catalog.manifest()
    assert catalog.manifest()["content_version"] == catalog.content_version()


def test_the_manifest_counts_every_table() -> None:
    """Issue #24's row counts, carried beside the digests that make them checkable."""
    catalog = fixture_catalog()
    described = catalog.manifest()["tables"]
    for name, rows in catalog.tables():
        assert described[name]["rows"] == len(rows)
        assert len(described[name]["sha256"]) == 64
