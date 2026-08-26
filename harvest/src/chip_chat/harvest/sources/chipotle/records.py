"""The parsed shape of Chipotle's published menu.

Seven flat tables rather than one nested document, because everything
downstream of here — the Snowflake catalogue in RFC-001 section 04, the
constrained vocabulary the vision matcher resolves against, the action surface
in issue #23 — wants rows it can join, not a tree it has to walk.

Two conventions run through all of them.

**Every row carries ``source_url`` and ``harvested_at``.** Not the table, the
row. By the time a price reaches a confirmation card there is nowhere left to
recover where it came from, and RFC-001 section 08 requires the answer to cite
its source.

**Prices are per restaurant, and separate from identity.** Chipotle's prices
genuinely differ store to store — a Steak Burrito was $11.15 at one restaurant
and $13.15 at another on the same afternoon — so a catalogue with one
``base_price`` column would be quietly wrong. Identity and structure live in
:class:`MenuItem`; money lives in :class:`ItemPrice`, keyed by restaurant, and
so does availability. See ``docs/decisions/menu-pricing.md`` for the decision and what
it costs.

Money is :class:`~decimal.Decimal`, parsed from the JSON token's own text and
serialised back as a string. A price is not a measurement and must not pick up
binary-float noise on the way through a dataset whose whole job is to
reproduce byte for byte.
"""

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from typing import Any

from chip_chat.harvest.blobs import BlobStore

DEFAULT_PARSED_PREFIX = "parsed/chipotle/menu"
"""Where the parsed tables land. Beside ``raw/``, never inside it."""

TABLES = (
    "menu_items",
    "item_prices",
    "modifier_groups",
    "modifiers",
    "portion_options",
    "meals",
    "meal_contents",
    "meal_prices",
    "ingredients",
    "item_ingredients",
)
"""Table names, in the order the manifest lists them."""


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One item on the menu, without its price.

    Attributes:
        item_id: Chipotle's own identifier, e.g. ``CMG-2``.
        name: The name as published, e.g. ``Steak Burrito``.
        category: ``Entree``, ``Side``, ``Drink`` or ``Non Food Items`` for an
            item that can be ordered on its own, and ``None`` for one that
            only ever appears inside another — the black beans in a burrito.
            Both are menu items with an identifier, a name and a price, so
            both are rows here; ``category IS NOT NULL`` is the test for
            whether a thing can be ordered by itself.
        item_type: The finer published type, e.g. ``Burrito``, ``Jarritos``,
            ``Beans``, ``ExtraPortion``.
        primary_filling: The protein an entree is built around, where the menu
            names one.
        description: What Chipotle publishes about the item, where it
            publishes anything. Most entrees have none — the description of a
            Steak Burrito is its structure, not prose.
        max_quantity: How many of it one order may contain.
        max_contents: Ceiling on modifiers, ``-1`` where the menu sets none.
        max_customizations: Ceiling on portion changes, ``-1`` for none.
        max_on_the_side_customizations: Ceiling on "on the side" requests.
        max_extras: Ceiling on extra portions.
        max_halfs: Ceiling on half portions.
        max_extras_plus_halfs: Ceiling on the two together.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    item_id: str
    name: str
    category: str | None
    item_type: str
    primary_filling: str | None
    description: str | None
    max_quantity: int | None
    max_contents: int | None
    max_customizations: int | None
    max_on_the_side_customizations: int | None
    max_extras: int | None
    max_halfs: int | None
    max_extras_plus_halfs: int | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class ItemPrice:
    """What one restaurant charged for one item, and whether it had it.

    Attributes:
        restaurant_id: Chipotle's numeric restaurant identifier.
        item_id: The item priced. Top-level items and modifiers share this
            namespace without colliding, so a modifier's price is a row here
            too.
        unit_price: The in-store price.
        unit_delivery_price: The delivery price, which is higher and is a
            different number rather than a surcharge.
        is_available: Whether this restaurant had the item at harvest time.
        eligible_for_delivery: Whether it can be delivered at all.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    restaurant_id: int
    item_id: str
    unit_price: Decimal
    unit_delivery_price: Decimal
    is_available: bool
    eligible_for_delivery: bool
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class ModifierGroup:
    """A slot on an item, and how many things may go in it.

    ``RiceContentGroup`` on a burrito takes exactly one rice;
    ``ToppingsContentGroup`` takes between one and five. This is the part of
    the taxonomy that says what a *valid* order looks like, which is why it is
    a table and not a sentence.

    Attributes:
        item_id: The item the slot belongs to.
        group_name: The slot's published name.
        min_quantity: Fewest choices the slot accepts, or ``None`` where the
            menu references the group without declaring its bounds.
        max_quantity: Most choices it accepts, or ``None``.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    item_id: str
    group_name: str
    min_quantity: int | None
    max_quantity: int | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class Modifier:
    """One thing that may be added to one item.

    Attributes:
        item_id: The item being modified.
        modifier_item_id: The identifier of the thing added, which is an item
            identifier in its own right and is how the price is joined.
        name: The name as published, e.g. ``Black Beans``.
        modifier_type: The published type, e.g. ``Beans``, ``Toppings``,
            ``ExtraPortion``.
        group_name: The slot it goes in, or ``None`` for modifiers the menu
            leaves ungrouped — extra and half portions, mostly.
        is_default: Whether it is included unless removed.
        counts_toward_content_max: How much of the item's content allowance it
            consumes.
        counts_toward_customization_max: How much of the customization
            allowance it consumes.
        pricing_reference_item_id: The item whose price this one is derived
            from, where the menu names one.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    item_id: str
    modifier_item_id: str
    name: str
    modifier_type: str
    group_name: str | None
    is_default: bool
    counts_toward_content_max: float
    counts_toward_customization_max: float
    pricing_reference_item_id: str | None
    source_url: str
    harvested_at: datetime

    @property
    def modifier_id(self) -> str:
        """A stable identifier for this modifier of this item.

        The same ingredient is a different modifier on a different item — it
        sits in a different slot and counts against a different allowance — so
        the identity is the pair, not the ingredient alone.
        """
        return f"{self.item_id}:{self.modifier_item_id}"


@dataclass(frozen=True, slots=True)
class PortionOption:
    """How much of a modifier may be asked for.

    The published vocabulary is exactly four words — ``Light``, ``Extra``,
    ``Side``, ``Half`` — and which of them a given modifier accepts varies.
    This table is where the vision matcher's constrained vocabulary comes
    from: "extra cheese" and "salsa on the side" have to resolve to a row
    here or be refused.

    Attributes:
        item_id: The item being modified.
        modifier_item_id: The modifier the option applies to.
        option_id: Chipotle's numeric identifier for the option.
        name: The option's published name.
        counts_toward_customization_max: How much of the customization
            allowance choosing it consumes. A half portion counts as ``0.5``.
        counts_toward_on_the_side_max: How much of the "on the side"
            allowance it consumes.
        counts_toward_content_max: How much of the content allowance it
            consumes, which is negative for a half portion.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    item_id: str
    modifier_item_id: str
    option_id: int
    name: str
    counts_toward_customization_max: float
    counts_toward_on_the_side_max: float
    counts_toward_content_max: float
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class Ingredient:
    """An ingredient Chipotle publishes a page about.

    Attributes:
        key: The short published key, e.g. ``avoc``.
        title: The ingredient's name, e.g. ``Avocado``.
        description: What Chipotle says about it. This is the source of a menu
            item's description where the item is an ingredient.
        fun_fact: The marketing aside published beside the description, kept
            apart from it so nothing downstream mistakes one for the other.
        used_in_menu_item_ids: The orderable items that contain this
            ingredient. Containment, not identity: black pepper lists the
            Steak Burrito. Reading it as "black pepper *is* the Steak Burrito"
            is the mistake this field name exists to prevent.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    key: str
    title: str
    description: str | None
    fun_fact: str | None
    used_in_menu_item_ids: tuple[str, ...]
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class ItemIngredients:
    """What Chipotle says is in one item, and which group it belongs to.

    The four published groups — proteins, rice and beans, toppings, sides —
    are the taxonomy in Chipotle's own words rather than ours, which is the
    only version worth carrying.

    Attributes:
        group_title: The published group, e.g. ``proteins``.
        item_id: The item.
        ingredient_keys: Keys into :class:`Ingredient`, in published order.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    group_title: str
    item_id: str
    ingredient_keys: tuple[str, ...]
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class Meal:
    """A preconfigured order Chipotle names, describes and sells as one thing.

    The high-protein bowls, the family Build-Your-Own packs, the bowls named
    after athletes. They matter here for two reasons: they are where Chipotle
    publishes actual prose about food, and they are a list of complete orders
    that a customer really can place — which is exactly what issue #23 needs
    the action surface to be drawn from rather than invented.

    A meal is not a menu item. Its ``entree_item_id`` is an ordinary item, and
    several meals share one: six different meals are built on the Chicken
    Bowl. The description belongs to the meal, never to the item.

    Attributes:
        meal_id: Chipotle's identifier for the meal.
        name: The meal's published name.
        meal_type: ``BuildYourOwn``, ``HighProtein`` or ``Influencer``.
        description: What the meal contains, in Chipotle's words.
        calories: The published calorie figure or range, verbatim, because a
            range is what is published and narrowing it here would be an
            invention. Nutrition proper is issue #20.
        dietary_tags: Published tags, e.g. ``Serves 4-6 people``.
        entree_item_id: The item the meal is built on.
        sort_order: Where the meal appears in the published order.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    meal_id: str
    name: str
    meal_type: str
    description: str | None
    calories: str | None
    dietary_tags: tuple[str, ...]
    entree_item_id: str | None
    sort_order: int | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class MealContent:
    """One line of what a meal is made of.

    Attributes:
        meal_id: The meal.
        position: Where the line falls in the published list, which is the
            only thing distinguishing two otherwise identical lines.
        item_id: The item in it.
        name: That item's name as the meal publishes it.
        quantity: How many.
        customization_id: The portion option applied, or ``None``.
        customization_name: That option's name, e.g. ``Extra``.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    meal_id: str
    position: int
    item_id: str
    name: str
    quantity: int | None
    customization_id: int | None
    customization_name: str | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class MealPrice:
    """What one restaurant charged for one meal.

    Attributes:
        restaurant_id: Chipotle's numeric restaurant identifier.
        meal_id: The meal priced.
        meal_price: The in-store price.
        meal_delivery_price: The delivery price.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    restaurant_id: int
    meal_id: str
    meal_price: Decimal
    meal_delivery_price: Decimal
    source_url: str
    harvested_at: datetime


def _json_ready(value: Any) -> Any:
    """Return ``value`` in a form :func:`json.dumps` can write deterministically."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    return value


def _row(record: Any) -> dict[str, Any]:
    """Return one dataclass instance as a JSON-ready mapping."""
    return {
        field.name: _json_ready(getattr(record, field.name)) for field in fields(record)
    }


def to_jsonl(records: Iterable[Any]) -> bytes:
    """Serialise records as JSON Lines, one compact object per line.

    Keys are sorted and separators are tight, so two runs over the same cache
    produce identical bytes and a digest is a meaningful thing to compare.

    Args:
        records: The dataclass instances to write, in the order wanted.

    Returns:
        The encoded document, ending in a newline when it is not empty.
    """
    lines = [
        json.dumps(_row(record), sort_keys=True, separators=(",", ":"))
        for record in records
    ]
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class MenuDataset:
    """Everything issue #19 harvests, parsed and flattened.

    Attributes:
        restaurant_ids: The restaurants priced, in the order harvested.
        reference_restaurant_id: The restaurant whose catalogue defines the
            structure, and whose prices a demo quotes when it quotes one.
        menu_items: Item identity and structure.
        item_prices: Money and availability, per restaurant.
        modifier_groups: The slots on each item.
        modifiers: What may go in them.
        portion_options: How much of each modifier may be asked for.
        meals: The preconfigured orders Chipotle names and describes.
        meal_contents: What each of them is made of.
        meal_prices: What each restaurant charged for them.
        ingredients: The published ingredient pages.
        item_ingredients: The published ingredient taxonomy.
    """

    restaurant_ids: tuple[int, ...]
    reference_restaurant_id: int
    menu_items: tuple[MenuItem, ...]
    item_prices: tuple[ItemPrice, ...]
    modifier_groups: tuple[ModifierGroup, ...]
    modifiers: tuple[Modifier, ...]
    portion_options: tuple[PortionOption, ...]
    meals: tuple[Meal, ...]
    meal_contents: tuple[MealContent, ...]
    meal_prices: tuple[MealPrice, ...]
    ingredients: tuple[Ingredient, ...]
    item_ingredients: tuple[ItemIngredients, ...]

    def table(self, name: str) -> Sequence[Any]:
        """Return one table by name.

        Args:
            name: One of :data:`TABLES`.

        Returns:
            The rows.

        Raises:
            KeyError: If ``name`` is not a table.
        """
        if name not in TABLES:
            raise KeyError(f"no such table {name!r}; expected one of {TABLES}")
        rows: Sequence[Any] = getattr(self, name)
        return rows

    def tables(self) -> Iterator[tuple[str, Sequence[Any]]]:
        """Yield every table as a ``(name, rows)`` pair, in :data:`TABLES` order."""
        for name in TABLES:
            yield name, self.table(name)

    def manifest(self) -> dict[str, Any]:
        """Return a description of this dataset, digests included.

        The digests are the point: issue #19 asks that re-running the parser
        against the cache reproduce the dataset exactly, and comparing two
        manifests is how that claim is checked rather than asserted.

        Returns:
            A JSON-ready mapping.
        """
        return {
            "restaurant_ids": list(self.restaurant_ids),
            "reference_restaurant_id": self.reference_restaurant_id,
            "tables": {
                name: {
                    "rows": len(rows),
                    "sha256": hashlib.sha256(to_jsonl(rows)).hexdigest(),
                }
                for name, rows in self.tables()
            },
        }

    def write(
        self, blobs: BlobStore, prefix: str = DEFAULT_PARSED_PREFIX
    ) -> Mapping[str, str]:
        """Write every table, and the manifest, to the blob store.

        Args:
            blobs: Where to write. The same store the raw bytes landed in,
                under a different prefix.
            prefix: Key prefix for the parsed tables.

        Returns:
            Table name to the key it was written at, with the manifest under
            the key ``manifest``.
        """
        root = prefix.strip("/")
        written: dict[str, str] = {}
        for name, rows in self.tables():
            key = f"{root}/{name}.jsonl"
            blobs.write(key, to_jsonl(rows))
            written[name] = key
        manifest_key = f"{root}/manifest.json"
        blobs.write(
            manifest_key,
            json.dumps(self.manifest(), indent=2, sort_keys=True).encode("utf-8"),
        )
        written["manifest"] = manifest_key
        return written
