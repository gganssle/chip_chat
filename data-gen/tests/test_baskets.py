"""That a basket obeys the published structure rather than the generator's taste.

The catalogue says which slots are one-of and which are any-of, and how many of
a thing may go on an item. Those are Chipotle's answers, not this package's, and
a generator that quietly picked two rices would be inventing a menu as surely as
one that picked an item that does not exist.
"""

import dataclasses

from population_fixtures import fixture_catalog, shipped_config

from chip_chat.data_gen import Channel, OrderableMenu, compose, mint_palate
from chip_chat.data_gen.baskets import Line, repeatable, weights_for
from chip_chat.data_gen.rng import substream


def menu() -> OrderableMenu:
    """The orderable view of the fixture catalogue."""
    return OrderableMenu(fixture_catalog(), shipped_config().catalogue)


def taste():
    """One customer's palate over that menu."""
    config = shipped_config()
    return mint_palate(substream(1, "palate"), menu(), config.palate_concentration)


def baskets(spec, count: int = 120, channel: Channel = Channel.IN_STORE):
    """Compose many baskets for one archetype."""
    return [
        compose(substream(2, "order", index), spec, menu(), channel, taste())
        for index in range(count)
    ]


def test_every_required_slot_is_filled_exactly_once() -> None:
    """Rice and beans are published as one-of, and a bowl without rice is not one."""
    offered = menu()
    spec = shipped_config().persona("explorer")
    slots = {
        buildable.item.item_id: buildable
        for buildable in offered.entrees(Channel.IN_STORE)
    }

    for lines in baskets(spec):
        for line in lines:
            buildable = slots.get(line.item_id)
            if buildable is None:
                continue
            for slot in buildable.required:
                published = {row.modifier_id for row in slot.choices}
                assert len(published & set(line.modifiers)) == 1


def test_an_optional_slot_stays_inside_the_archetype_and_the_published_maximum() -> None:
    offered = menu()
    spec = dataclasses.replace(
        shipped_config().persona("explorer"), toppings_min=1, toppings_max=2
    )
    slots = {
        buildable.item.item_id: buildable
        for buildable in offered.entrees(Channel.IN_STORE)
    }

    seen = set()
    for lines in baskets(spec):
        for line in lines:
            buildable = slots.get(line.item_id)
            if buildable is None:
                continue
            for slot in buildable.optional:
                published = {row.modifier_id for row in slot.choices}
                chosen = published & set(line.modifiers)
                ceiling = min(2, len(published))
                if slot.published_max is not None:
                    ceiling = min(ceiling, slot.published_max)
                assert 1 <= len(chosen) <= ceiling
                seen.add(len(chosen))

    assert seen


def test_an_archetype_that_wants_no_toppings_gets_none() -> None:
    spec = dataclasses.replace(
        shipped_config().persona("explorer"),
        toppings_min=0,
        toppings_max=0,
        extra_probability=0.0,
    )
    offered = menu()
    optional = {
        row.modifier_id
        for buildable in offered.entrees(Channel.IN_STORE)
        for slot in buildable.optional
        for row in slot.choices
    }

    for lines in baskets(spec, count=40):
        for line in lines:
            assert set(line.modifiers) & optional == set()


def test_a_group_order_is_bigger_than_one_person_s_lunch() -> None:
    config = shipped_config()

    solo = baskets(config.persona("regular"), count=40)
    group = baskets(config.persona("office_manager"), count=40)

    def entrees(lines: list[Line]) -> int:
        return sum(line.qty for line in lines)

    assert min(entrees(lines) for lines in group) > max(
        entrees(lines) for lines in solo
    ) or sum(entrees(lines) for lines in group) > 3 * sum(
        entrees(lines) for lines in solo
    )


def test_identical_builds_are_merged_into_a_quantity() -> None:
    """Five identical bowls are one line on a receipt, not five."""
    spec = dataclasses.replace(
        shipped_config().persona("office_manager"),
        entrees_min=6,
        entrees_max=6,
        toppings_min=1,
        toppings_max=1,
        side_probability=0.0,
        drink_probability=0.0,
        extra_probability=0.0,
    )

    merged = [
        lines for lines in baskets(spec, count=60) if any(line.qty > 1 for line in lines)
    ]

    assert merged
    for lines in baskets(spec, count=60):
        builds = [(line.item_id, line.modifiers) for line in lines]
        assert len(builds) == len(set(builds))


def test_a_remembered_basket_is_only_repeated_where_it_is_still_sold() -> None:
    """ "The same bowl every Tuesday" must not be what invents an availability."""
    catalog = fixture_catalog()
    entree = menu().entrees(Channel.IN_STORE)[0].item.item_id
    counter_only = OrderableMenu(
        dataclasses.replace(
            catalog,
            item_prices=tuple(
                dataclasses.replace(row, eligible_for_delivery=False)
                if row.item_id == entree
                else row
                for row in catalog.item_prices
            ),
        ),
        shipped_config().catalogue,
    )
    usual = (Line(item_id=entree, qty=1, modifiers=()),)

    assert repeatable(usual, counter_only, Channel.IN_STORE)
    assert not repeatable(usual, counter_only, Channel.DELIVERY)


def test_an_item_the_palate_has_never_seen_is_not_unorderable() -> None:
    """A catalogue that grew a row must not grow a row nobody can order."""
    weights = weights_for({"CMG-1": 0.25, "CMG-2": 0.75}, ["CMG-1", "CMG-3"])

    assert weights[0] == 0.25
    assert weights[1] > 0


def test_a_basket_composed_for_delivery_only_holds_things_sold_that_way() -> None:
    catalog = fixture_catalog()
    entree = menu().entrees(Channel.IN_STORE)[0].item.item_id
    counter_only = OrderableMenu(
        dataclasses.replace(
            catalog,
            item_prices=tuple(
                dataclasses.replace(row, eligible_for_delivery=False)
                if row.item_id == entree
                else row
                for row in catalog.item_prices
            ),
        ),
        shipped_config().catalogue,
    )
    spec = shipped_config().persona("explorer")
    palate = mint_palate(
        substream(3, "palate"), counter_only, shipped_config().palate_concentration
    )

    for index in range(60):
        lines = compose(
            substream(3, "order", index), spec, counter_only, Channel.DELIVERY, palate
        )
        assert all(line.item_id != entree for line in lines)
