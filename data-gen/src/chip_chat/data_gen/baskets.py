"""Composing one basket out of real catalogue rows, and nothing else.

This is where issue #25's second acceptance criterion is kept: zero orders
reference an item or modifier absent from ``menu_catalog``. The mechanism is
that no identifier in this module is ever constructed — every ``item_id`` and
every ``modifier_id`` is read off a row handed over by
:class:`~chip_chat.data_gen.catalogue.OrderableMenu`, which read it off the
catalogue. There is no code path that could produce a name Chipotle does not
publish, which is a stronger claim than a test that no run happened to.

The other thing that happens here is the difference between one customer and
another. Two customers with the same archetype draw from the same behaviour,
so without something else they would order the same distribution of food and
the personalization lane would have nothing to find. The something else is a
:func:`palate`: a Dirichlet draw over every orderable row, minted once per
customer, which makes one customer the guacamole one and another the person
who never orders a drink. Their *usual* falls out of the same draw, which is
why the Tuesday regular's usual is theirs rather than their archetype's.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from random import Random

from chip_chat.catalog import MenuItem
from chip_chat.data_gen.catalogue import Buildable, OrderableMenu, SlotChoices
from chip_chat.data_gen.config import PersonaSpec
from chip_chat.data_gen.records import Channel
from chip_chat.data_gen.rng import palate, weighted_choice, weighted_sample

Palate = Mapping[str, float]
"""One customer's weight on every orderable identifier, item and modifier alike."""


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a basket, before it has been priced.

    Attributes:
        item_id: A ``menu_items.item_id``, off a catalogue row.
        qty: How many of this exact build.
        modifiers: ``modifiers.modifier_id`` values, sorted.
    """

    item_id: str
    qty: int
    modifiers: tuple[str, ...]


def mint_palate(rng: Random, menu: OrderableMenu, concentration: float) -> Palate:
    """Return one customer's preferences over everything orderable.

    Args:
        rng: This customer's palate stream.
        menu: What is orderable. Both channels are covered, so a customer's
            taste does not change when they order delivery.
        concentration: The Dirichlet concentration from the config.

    Returns:
        Identifier to weight. Weights are comparable only within a group;
        :func:`weights_for` slices them.
    """
    keys = sorted(_identifiers(menu))
    drawn = palate(rng, len(keys), concentration)
    return dict(zip(keys, drawn, strict=True))


def _identifiers(menu: OrderableMenu) -> set[str]:
    """Return every identifier a basket could contain, across both channels."""
    keys: set[str] = set()
    for channel in Channel:
        for buildable in menu.entrees(channel):
            keys.add(buildable.item.item_id)
            for slot in (*buildable.required, *buildable.optional):
                keys.update(modifier.modifier_id for modifier in slot.choices)
            keys.update(modifier.modifier_id for modifier in buildable.extras)
        keys.update(item.item_id for item in menu.sides(channel))
        keys.update(item.item_id for item in menu.drinks(channel))
    return keys


def weights_for(taste: Palate, keys: Sequence[str]) -> list[float]:
    """Return this customer's weights for ``keys``, in order.

    Args:
        taste: The customer's palate.
        keys: Identifiers to weigh.

    Returns:
        One weight per key. An identifier the palate has never seen weighs the
        same as an average one rather than nothing, so a catalogue that grew a
        row does not become a row nobody can order.
    """
    default = 1.0 / len(taste) if taste else 1.0
    return [taste.get(key, default) for key in keys]


def compose(
    rng: Random,
    spec: PersonaSpec,
    menu: OrderableMenu,
    channel: Channel,
    taste: Palate,
) -> tuple[Line, ...]:
    """Compose one basket: entrees, their builds, and whatever came alongside.

    Args:
        rng: This order's stream.
        spec: The customer's archetype.
        menu: What is orderable.
        channel: Which published price list this order is on.
        taste: The customer's palate.

    Returns:
        The lines, entrees first, then sides, then drinks. Identical builds
        are merged into one line with a quantity, which is what five identical
        bowls on an office order look like on a receipt.
    """
    entrees = menu.entrees(channel)
    if not entrees:
        return ()
    wanted = rng.randint(spec.entrees_min, spec.entrees_max)
    lines: list[Line] = []
    for _ in range(wanted):
        chosen = weighted_choice(
            rng, entrees, weights_for(taste, [row.item.item_id for row in entrees])
        )
        lines.append(_entree(rng, spec, chosen, taste))
    lines.extend(
        _alongside(rng, menu.sides(channel), spec.side_probability, wanted, taste)
    )
    lines.extend(
        _alongside(rng, menu.drinks(channel), spec.drink_probability, wanted, taste)
    )
    return _merged(lines)


def repeatable(lines: Sequence[Line], menu: OrderableMenu, channel: Channel) -> bool:
    """Return whether a remembered basket can still be ordered on ``channel``.

    A customer's usual is composed once, at the counter. Chipotle publishes
    availability per channel, so before that usual is reordered for delivery
    every line in it has to still be sellable that way — otherwise the "same
    bowl every Tuesday" property would quietly start inventing availability.

    Args:
        lines: The remembered basket.
        menu: What is orderable.
        channel: The channel this order is on.

    Returns:
        Whether every line survives.
    """
    return all(menu.sellable(line.item_id, channel) for line in lines)


def _entree(rng: Random, spec: PersonaSpec, buildable: Buildable, taste: Palate) -> Line:
    """Build one entree: its required slots, some optional ones, maybe an extra."""
    chosen: list[str] = []
    for slot in buildable.required:
        chosen.append(_one(rng, slot, taste))
    for slot in buildable.optional:
        chosen.extend(_some(rng, spec, slot, taste))
    if buildable.extras and rng.random() < spec.extra_probability:
        extras = buildable.extras
        chosen.append(
            weighted_choice(
                rng, extras, weights_for(taste, [row.modifier_id for row in extras])
            ).modifier_id
        )
    return Line(item_id=buildable.item.item_id, qty=1, modifiers=tuple(sorted(chosen)))


def _one(rng: Random, slot: SlotChoices, taste: Palate) -> str:
    """Pick the single choice a one-of slot takes."""
    return weighted_choice(
        rng,
        slot.choices,
        weights_for(taste, [row.modifier_id for row in slot.choices]),
    ).modifier_id


def _some(rng: Random, spec: PersonaSpec, slot: SlotChoices, taste: Palate) -> list[str]:
    """Pick however many an any-of slot takes, inside what is published."""
    ceiling = min(spec.toppings_max, len(slot.choices))
    if slot.published_max is not None:
        ceiling = min(ceiling, slot.published_max)
    floor = min(spec.toppings_min, ceiling)
    count = rng.randint(floor, ceiling)
    drawn = weighted_sample(
        rng,
        slot.choices,
        weights_for(taste, [row.modifier_id for row in slot.choices]),
        count,
    )
    return [row.modifier_id for row in drawn]


def _alongside(
    rng: Random,
    items: Sequence[MenuItem],
    probability: float,
    chances: int,
    taste: Palate,
) -> list[Line]:
    """Add a side or a drink, once per entree, at the archetype's rate."""
    if not items:
        return []
    keys = [item.item_id for item in items]
    lines: list[Line] = []
    for _ in range(chances):
        if rng.random() >= probability:
            continue
        lines.append(
            Line(
                item_id=weighted_choice(rng, keys, weights_for(taste, keys)),
                qty=1,
                modifiers=(),
            )
        )
    return lines


def _merged(lines: Sequence[Line]) -> tuple[Line, ...]:
    """Collapse identical builds into one line carrying a quantity."""
    merged: list[Line] = []
    for line in lines:
        for index, seen in enumerate(merged):
            if seen.item_id == line.item_id and seen.modifiers == line.modifiers:
                merged[index] = Line(
                    item_id=seen.item_id,
                    qty=seen.qty + line.qty,
                    modifiers=seen.modifiers,
                )
                break
        else:
            merged.append(line)
    return tuple(merged)
