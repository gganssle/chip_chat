"""The catalogue, narrowed to the question "what can go in a basket?".

Issue #25's second acceptance criterion is that zero orders reference an item
or modifier absent from ``menu_catalog``. The cheapest way to keep a rule like
that is to make breaking it unreachable, so nothing downstream of this module
ever sees an item identifier that did not come out of it: the generator picks
:class:`Buildable` objects it was handed, not strings it composed.

Two things the catalogue publishes are honoured here rather than assumed away.
**A slot's published quantities.** ``modifiers.min_quantity`` and
``max_quantity`` say when a choice is one-of and when it is any-of, and the
generator obeys them rather than deciding for itself that a burrito takes one
rice. **Two prices per item.** Chipotle publishes a counter price and a
delivery price, and an item may be available at one and not the other, so the
orderable menu is computed per :class:`~chip_chat.data_gen.records.Channel`
and an order composed for delivery cannot contain something not sold that way.

The one thing this module does that the catalogue does not is decide *whose*
prices a store quotes. Chipotle publishes a menu per restaurant and the
harvest prices as many restaurants as it was asked to — one, in practice —
while the population orders from thirty stores. :meth:`OrderableMenu.pricing`
returns the store's own restaurant when the catalogue priced it and the
catalogue's reference restaurant when it did not, and every order records
which it got. See ``docs/decisions/synthetic-population.md``.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from chip_chat.catalog import MenuCatalog, MenuItem, Modifier, Store
from chip_chat.data_gen.config import CatalogueConfig
from chip_chat.data_gen.errors import ThinCatalogError
from chip_chat.data_gen.records import Channel

FREE = Decimal("0")
"""What a modifier costs when the catalogue publishes no price for it.

Not a guess at what it might cost. The catalogue prices what Chipotle prices,
and a modifier with no ``item_prices`` row is one the published menu does not
charge separately for; inventing an upcharge would be inventing money.
"""


@dataclass(frozen=True, slots=True)
class SlotChoices:
    """The published choices for one slot on one item.

    Attributes:
        slot: The slot's name, as ``modifiers.slot`` spells it.
        choices: The modifiers, sorted by ``modifier_id``.
        published_max: The largest published ``max_quantity`` across those
            modifiers, or ``None`` if none of them publishes one. The
            generator never picks more than this.
    """

    slot: str
    choices: tuple[Modifier, ...]
    published_max: int | None


@dataclass(frozen=True, slots=True)
class Buildable:
    """One orderable entree and everything published as buildable onto it.

    Attributes:
        item: The entree.
        required: Slots the generator always fills — the catalogue publishes
            them as one-of and a bowl without rice is a bowl no one ordered.
        optional: Slots it fills a variable number of.
        extras: Published paid extra portions, e.g. ``Extra Chicken``.
    """

    item: MenuItem
    required: tuple[SlotChoices, ...]
    optional: tuple[SlotChoices, ...]
    extras: tuple[Modifier, ...]


class OrderableMenu:
    """What a basket may contain, per channel, and what each thing costs.

    Args:
        catalog: The catalogue. Nothing is orderable that is not in it.
        shape: Which published categories and modifier types mean what.

    Raises:
        ThinCatalogError: If the catalogue contains no orderable entree at
            all. Refusing is the only honest response — the alternative is a
            population of empty orders, or an invented item.
    """

    def __init__(self, catalog: MenuCatalog, shape: CatalogueConfig) -> None:
        self.reference_restaurant_id = catalog.reference_restaurant_id
        self.content_version = catalog.content_version()
        self._shape = shape
        self._prices = {
            (row.restaurant_id, row.item_id): row for row in catalog.item_prices
        }
        self._priced_restaurants = frozenset(catalog.restaurant_ids)
        self._modifiers: dict[str, list[Modifier]] = {}
        self._by_modifier_id: dict[str, Modifier] = {}
        for modifier in sorted(catalog.modifiers, key=lambda row: row.modifier_id):
            self._modifiers.setdefault(modifier.item_id, []).append(modifier)
            self._by_modifier_id[modifier.modifier_id] = modifier

        items = sorted(catalog.menu_items, key=lambda row: row.item_id)
        self._entrees = {
            channel: tuple(
                self._buildable(item)
                for item in items
                if item.category == shape.entree_category
                if self.sellable(item.item_id, channel)
            )
            for channel in Channel
        }
        self._sides = {
            channel: self._standalone(items, shape.side_categories, channel)
            for channel in Channel
        }
        self._drinks = {
            channel: self._standalone(items, shape.drink_categories, channel)
            for channel in Channel
        }
        if not any(self._entrees.values()):
            raise ThinCatalogError(
                f"catalogue {self.content_version[:12]} publishes no available "
                f"item in category {shape.entree_category!r} priced at "
                f"restaurant {self.reference_restaurant_id}; there is nothing "
                "to compose an order from and nothing may be invented"
            )

    def entrees(self, channel: Channel) -> tuple[Buildable, ...]:
        """Return every entree orderable on ``channel``, sorted by item id."""
        return self._entrees[channel]

    def sides(self, channel: Channel) -> tuple[MenuItem, ...]:
        """Return every side orderable on ``channel``, sorted by item id."""
        return self._sides[channel]

    def drinks(self, channel: Channel) -> tuple[MenuItem, ...]:
        """Return every drink orderable on ``channel``, sorted by item id."""
        return self._drinks[channel]

    def stores(self, catalog_stores: Sequence[Store], wanted: int) -> tuple[Store, ...]:
        """Return the store roster the population orders from.

        Args:
            catalog_stores: Every store the locator harvest carried.
            wanted: How many the config asks for.

        Returns:
            The first ``wanted`` stores by ``store_id``, with the catalogue's
            reference restaurant always among them. Taken in identifier order
            rather than sampled, so re-harvesting the locator adds stores to
            the end of the roster instead of reshuffling everybody's history.

        Raises:
            ThinCatalogError: If the catalogue carries no stores.
        """
        ordered = sorted(catalog_stores, key=lambda row: row.store_id)
        if not ordered:
            raise ThinCatalogError(
                "the catalogue carries no stores; an order has to happen "
                "somewhere and a store cannot be invented"
            )
        roster = ordered[:wanted]
        if all(store.store_id != self.reference_restaurant_id for store in roster):
            reference = [
                store
                for store in ordered
                if store.store_id == self.reference_restaurant_id
            ]
            roster = (reference + roster)[:wanted]
        return tuple(sorted(roster, key=lambda row: row.store_id))

    def modifier(self, modifier_id: str) -> Modifier:
        """Return one modifier by identifier.

        Args:
            modifier_id: A ``modifiers.modifier_id``, which the catalogue keys
                as ``(item_id, modifier_item_id)``.

        Returns:
            The modifier.

        Raises:
            KeyError: If the catalogue does not publish it. Reached only by a
                caller that constructed an identifier rather than reading one,
                which is the bug this package is arranged to make impossible.
        """
        return self._by_modifier_id[modifier_id]

    def pricing(self, store_id: int) -> int:
        """Return which restaurant's published prices a store quotes.

        Args:
            store_id: Where the order happened.

        Returns:
            ``store_id`` when the catalogue priced that restaurant, and the
            catalogue's reference restaurant when it did not.
        """
        if store_id in self._priced_restaurants:
            return store_id
        return self.reference_restaurant_id

    def price(self, restaurant_id: int, item_id: str, channel: Channel) -> Decimal:
        """Return one published price.

        Args:
            restaurant_id: Whose menu to quote.
            item_id: What to quote.
            channel: Which of the two published prices.

        Returns:
            The price, or :data:`FREE` when the catalogue publishes no price
            row for the item — which is the case for most modifiers, and is
            what "included" means on a published menu.
        """
        row = self._prices.get((restaurant_id, item_id))
        if row is None:
            return FREE
        if channel is Channel.DELIVERY:
            return row.unit_delivery_price
        return row.unit_price

    def _standalone(
        self, items: Iterable[MenuItem], categories: Sequence[str], channel: Channel
    ) -> tuple[MenuItem, ...]:
        """Return the items of ``categories`` that are sellable on ``channel``."""
        return tuple(
            item
            for item in items
            if item.category in categories
            if item.category not in self._shape.excluded_categories
            if self.sellable(item.item_id, channel)
        )

    def sellable(self, item_id: str, channel: Channel) -> bool:
        """Return whether the reference restaurant sells this item on ``channel``.

        Availability is read from the reference restaurant because that is the
        menu whose structure the catalogue was built from. A store priced by
        fallback sells what the reference restaurant sells; saying otherwise
        would be inventing a stock level.
        """
        row = self._prices.get((self.reference_restaurant_id, item_id))
        if row is None or not row.is_available:
            return False
        if channel is Channel.DELIVERY:
            return row.eligible_for_delivery
        return True

    def _buildable(self, item: MenuItem) -> Buildable:
        """Group one entree's published modifiers into the slots it fills."""
        published = self._modifiers.get(item.item_id, [])
        return Buildable(
            item=item,
            required=self._slots(published, self._shape.required_slots),
            optional=self._slots(published, self._shape.optional_slots),
            extras=tuple(
                modifier
                for modifier in published
                if modifier.modifier_type == self._shape.extra_portion_modifier_type
            ),
        )

    def _slots(
        self, published: Sequence[Modifier], wanted: Sequence[str]
    ) -> tuple[SlotChoices, ...]:
        """Return one :class:`SlotChoices` per wanted slot the item publishes."""
        grouped = []
        for slot in wanted:
            choices = tuple(
                modifier
                for modifier in published
                if modifier.slot is not None
                if str(modifier.slot) == slot
            )
            if not choices:
                continue
            maxima = [
                modifier.max_quantity
                for modifier in choices
                if modifier.max_quantity is not None
            ]
            grouped.append(
                SlotChoices(
                    slot=slot,
                    choices=choices,
                    published_max=max(maxima) if maxima else None,
                )
            )
        return tuple(grouped)
