"""Consolidating three harvests into the one table everything resolves against.

The menu of issue #19 knows what exists and what it costs. The nutrition data
of issue #20 knows what is in it and what it does to you. The policy corpus of
issue #21 knows where you can buy it. None of them is the catalogue; the
catalogue is the join, and this module is the join.

Nothing here fetches, and nothing here invents. Every column is a published
value carried across with its provenance, or a value derived from published
columns by a rule written down in ``docs/decisions/catalog-shape.md``. Where
the three harvests disagree about which items exist, the menu wins — it is the
document that says what a restaurant will actually sell — and an item the
nutrition data has never heard of gets ``NOT_PUBLISHED`` rather than a guess.

Two things this module deliberately does not do.

**It does not sum calories.** A Steak Burrito's published figure is its steak;
a total is that plus each chosen modifier's figure, and any of those may be
``None`` because nobody published it. Summing here would need a rule for what
``None`` contributes, and every available rule is a lie: zero understates,
skipping the row understates, and refusing to answer is a decision for the
confirmation card of issue #62 rather than for the catalogue.

**It does not convert an allergen to a boolean.** See
:class:`~chip_chat.catalog.records.AllergenDisclosure`.
"""

from collections.abc import Mapping, Sequence

from chip_chat.catalog.errors import MissingSourceError
from chip_chat.catalog.records import (
    Allergen,
    AllergenDisclosure,
    Caveat,
    ItemAllergen,
    ItemPrice,
    MenuCatalog,
    MenuItem,
    Modifier,
    Store,
    StoreHours,
)
from chip_chat.catalog.vocabulary import build_vocabulary, slot_of
from chip_chat.harvest.sources.chipotle import (
    AllergenStatus,
    DietaryTag,
    ItemNutrient,
    MenuDataset,
    ModifierGroup,
    NutritionDataset,
    PolicyDataset,
    PortionOption,
    StoreProfile,
    TagKind,
)
from chip_chat.harvest.sources.chipotle import (
    Caveat as HarvestCaveat,
)
from chip_chat.harvest.sources.chipotle import (
    ItemAllergen as HarvestItemAllergen,
)
from chip_chat.harvest.sources.chipotle import (
    ItemPrice as HarvestItemPrice,
)
from chip_chat.harvest.sources.chipotle import (
    MenuItem as HarvestMenuItem,
)
from chip_chat.harvest.sources.chipotle import (
    Store as HarvestStore,
)
from chip_chat.harvest.sources.chipotle import (
    StoreHours as HarvestStoreHours,
)
from chip_chat.harvest.sources.chipotle.locator import DAYS

CALORIE_NUTRIENT_KEY = "tcal"
"""The published key for the total-calorie figure.

Checked against the harvested nutrient vocabulary on every build. If Chipotle
renames it, :func:`build_catalog` raises rather than producing a catalogue
whose ``calories`` column is null for every row — an empty calorie column is
the kind of failure that gets noticed a demo later, and a build that stops is
the kind that gets noticed now.
"""


def build_catalog(
    menu: MenuDataset,
    nutrition: NutritionDataset,
    policy: PolicyDataset,
) -> MenuCatalog:
    """Consolidate the three harvested datasets into the catalogue.

    Args:
        menu: The parsed menu of issue #19 — identity, structure and prices.
        nutrition: The parsed nutrition and allergen data of issue #20.
        policy: The parsed policy corpus of issue #21, for its stores.

    Returns:
        The catalogue, every table sorted so two builds over one harvest
        produce identical bytes and therefore an identical version.

    Raises:
        MissingSourceError: If the published nutrient vocabulary has no
            total-calorie figure in it, if the menu has no items, or if the
            nutrition dataset does not cover every item the menu sells. All
            three mean the harvest this is built from is not the harvest this
            expects, and a catalogue built anyway would be silently empty in a
            column somebody is about to quote.
    """
    _require_calorie_nutrient(nutrition)
    if not menu.menu_items:
        raise MissingSourceError(
            "the menu dataset has no items; there is no catalogue to build"
        )

    calories = _calories_by_item(nutrition.item_nutrition)
    allergens = _allergens_by_item(nutrition.item_allergens)
    composed = {modifier.item_id for modifier in menu.modifiers}

    menu_items = tuple(
        _menu_item(item, calories, allergens, composed) for item in menu.menu_items
    )
    known = {item.item_id for item in menu_items}
    modifiers = _modifiers(menu, calories)
    catalog_allergens = _allergen_vocabulary(nutrition.dietary_tags)
    _require_allergen_coverage(menu_items, catalog_allergens, nutrition.item_allergens)

    return MenuCatalog(
        reference_restaurant_id=menu.reference_restaurant_id,
        restaurant_ids=menu.restaurant_ids,
        menu_items=menu_items,
        item_prices=tuple(
            _price(row) for row in menu.item_prices if row.item_id in known
        ),
        modifiers=modifiers,
        stores=_stores(policy),
        item_allergens=tuple(
            _item_allergen(row)
            for row in nutrition.item_allergens
            if row.item_id in known
        ),
        allergens=catalog_allergens,
        caveats=tuple(_caveat(row) for row in nutrition.caveats),
        vocabulary=build_vocabulary(menu_items, modifiers),
    )


def _require_calorie_nutrient(nutrition: NutritionDataset) -> None:
    """Fail the build if the published nutrient vocabulary lost its calories."""
    if any(row.nutrient_key == CALORIE_NUTRIENT_KEY for row in nutrition.nutrients):
        return
    published = ", ".join(sorted(row.nutrient_key for row in nutrition.nutrients))
    raise MissingSourceError(
        f"the published nutrient vocabulary has no {CALORIE_NUTRIENT_KEY!r} in "
        f"it, so no row would carry a calorie figure; it published {published}"
    )


def _require_allergen_coverage(
    menu_items: Sequence[MenuItem],
    allergens: Sequence[Allergen],
    rows: Sequence[HarvestItemAllergen],
) -> None:
    """Fail the build if any item lacks a statement about any published allergen.

    The nutrition harvest guarantees a row for every item it knows about
    crossed with every published allergen code, so a gap here means the two
    harvests were run over different restaurants and the catalogue is a join
    that quietly missed. It would fail in the safe direction — an item with no
    rows reads as ``NOT_PUBLISHED`` — but ``item_allergens`` would be short,
    and the difference between "we asked and nothing is published" and "we
    never asked" is the whole reason that table has a row per pair.
    """
    stated = {(row.item_id, row.allergen_code) for row in rows}
    codes = [row.allergen_code for row in allergens]
    for item in menu_items:
        missing = [code for code in codes if (item.item_id, code) not in stated]
        if missing:
            raise MissingSourceError(
                f"the nutrition dataset says nothing at all about {item.item_id} "
                f"({item.name}) and {', '.join(missing)}; the menu and nutrition "
                f"harvests do not describe the same restaurants"
            )


class _Figure:
    """One published figure and the document it was published in.

    A merged row cites what it merged, so the calorie figure carries its own
    ``source_url`` rather than borrowing the menu's.
    """

    __slots__ = ("harvested_at", "source_url", "value")

    def __init__(self, row: ItemNutrient) -> None:
        self.value = row.value
        self.source_url = row.source_url
        self.harvested_at = row.harvested_at


def _calories_by_item(rows: Sequence[ItemNutrient]) -> Mapping[str, _Figure]:
    """Index the published total-calorie figure by item.

    A row whose ``value`` is ``None`` is kept rather than dropped: "nobody
    published a figure for this" and "this item is not in the nutrition data
    at all" are different answers, and the second one has no ``source_url`` to
    cite while the first one does.
    """
    return {
        row.item_id: _Figure(row)
        for row in rows
        if row.nutrient_key == CALORIE_NUTRIENT_KEY
    }


class _AllergenMarks:
    """What one item's allergen rows add up to, without adding up to a boolean."""

    __slots__ = ("codes", "disclosure", "harvested_at", "source_url")

    def __init__(self, rows: Sequence[HarvestItemAllergen]) -> None:
        self.codes = tuple(
            sorted(
                row.allergen_code for row in rows if row.status is AllergenStatus.CONTAINS
            )
        )
        published = any(row.status is not AllergenStatus.NOT_PUBLISHED for row in rows)
        self.disclosure = (
            AllergenDisclosure.PUBLISHED
            if published
            else AllergenDisclosure.NOT_PUBLISHED
        )
        self.source_url = rows[0].source_url
        self.harvested_at = rows[0].harvested_at


def _allergens_by_item(
    rows: Sequence[HarvestItemAllergen],
) -> Mapping[str, _AllergenMarks]:
    """Group the three-valued allergen rows by the item they describe."""
    grouped: dict[str, list[HarvestItemAllergen]] = {}
    for row in rows:
        grouped.setdefault(row.item_id, []).append(row)
    return {item_id: _AllergenMarks(group) for item_id, group in grouped.items()}


def _menu_item(
    item: HarvestMenuItem,
    calories: Mapping[str, _Figure],
    allergens: Mapping[str, _AllergenMarks],
    composed: frozenset[str] | set[str],
) -> MenuItem:
    """Merge one item's identity, calories and allergen marks into one row."""
    figure = calories.get(item.item_id)
    marks = allergens.get(item.item_id)
    return MenuItem(
        item_id=item.item_id,
        name=item.name,
        category=item.category,
        item_type=item.item_type,
        primary_filling=item.primary_filling,
        description=item.description,
        calories=figure.value if figure is not None else None,
        is_composed=item.item_id in composed,
        allergens=marks.codes if marks is not None else (),
        allergen_disclosure=(
            marks.disclosure if marks is not None else AllergenDisclosure.NOT_PUBLISHED
        ),
        source_url=item.source_url,
        harvested_at=item.harvested_at,
        nutrition_source_url=figure.source_url if figure is not None else None,
        nutrition_harvested_at=figure.harvested_at if figure is not None else None,
        allergen_source_url=marks.source_url if marks is not None else None,
        allergen_harvested_at=marks.harvested_at if marks is not None else None,
    )


def _modifiers(
    menu: MenuDataset, calories: Mapping[str, _Figure]
) -> tuple[Modifier, ...]:
    """Build the modifier table, slots and portion words attached.

    A modifier's calories are the calories of the thing added, which is a menu
    item in its own right, so the figure is looked up under
    ``modifier_item_id`` and not under the item being modified. Reading it
    under the latter would give every topping on a Steak Burrito the burrito's
    own 150 calories.
    """
    bounds = _group_bounds(menu.modifier_groups)
    portions = _portions(menu.portion_options)
    rows: list[Modifier] = []
    for modifier in menu.modifiers:
        placement = slot_of(modifier_type=modifier.modifier_type, name=modifier.name)
        figure = calories.get(modifier.modifier_item_id)
        low, high = bounds.get((modifier.item_id, modifier.group_name), (None, None))
        rows.append(
            Modifier(
                modifier_id=modifier.modifier_id,
                item_id=modifier.item_id,
                modifier_item_id=modifier.modifier_item_id,
                name=modifier.name,
                slot=placement[0] if placement is not None else None,
                derivation=placement[1] if placement is not None else None,
                group_name=modifier.group_name or None,
                modifier_type=modifier.modifier_type,
                min_quantity=low,
                max_quantity=high,
                is_default=modifier.is_default,
                delta_calories=figure.value if figure is not None else None,
                portion_options=portions.get(
                    (modifier.item_id, modifier.modifier_item_id), ()
                ),
                source_url=modifier.source_url,
                harvested_at=modifier.harvested_at,
                nutrition_source_url=figure.source_url if figure is not None else None,
                nutrition_harvested_at=(
                    figure.harvested_at if figure is not None else None
                ),
            )
        )
    return tuple(rows)


def _group_bounds(
    groups: Sequence[ModifierGroup],
) -> Mapping[tuple[str, str | None], tuple[int | None, int | None]]:
    """Index how many choices each slot on each item accepts."""
    return {
        (group.item_id, group.group_name): (group.min_quantity, group.max_quantity)
        for group in groups
    }


def _portions(
    options: Sequence[PortionOption],
) -> Mapping[tuple[str, str], tuple[str, ...]]:
    """Index the portion words each modifier accepts, in published order."""
    grouped: dict[tuple[str, str], list[str]] = {}
    for option in options:
        grouped.setdefault((option.item_id, option.modifier_item_id), []).append(
            option.name
        )
    return {key: tuple(names) for key, names in grouped.items()}


def _price(row: HarvestItemPrice) -> ItemPrice:
    """Carry one price row across unchanged."""
    return ItemPrice(
        restaurant_id=row.restaurant_id,
        item_id=row.item_id,
        unit_price=row.unit_price,
        unit_delivery_price=row.unit_delivery_price,
        is_available=row.is_available,
        eligible_for_delivery=row.eligible_for_delivery,
        source_url=row.source_url,
        harvested_at=row.harvested_at,
    )


def _item_allergen(row: HarvestItemAllergen) -> ItemAllergen:
    """Carry one allergen statement across with its status untouched."""
    return ItemAllergen(
        item_id=row.item_id,
        allergen_code=row.allergen_code,
        status=row.status,
        source_url=row.source_url,
        harvested_at=row.harvested_at,
    )


def _allergen_vocabulary(tags: Sequence[DietaryTag]) -> tuple[Allergen, ...]:
    """Return the published allergen codes and whatever labels came with them.

    A code Chipotle publishes without a label keeps a null name. The nutrition
    harvest found two codes in that state and refused to guess at either; this
    refuses in the same direction, because a label invented here would read
    exactly like a published one by the time it reached a visitor.
    """
    return tuple(
        Allergen(
            allergen_code=tag.tag_code,
            name=tag.tag_name,
            badge_text=tag.badge_text,
            source_url=tag.source_url,
            harvested_at=tag.harvested_at,
        )
        for tag in sorted(tags, key=lambda tag: tag.tag_code)
        if tag.kind is TagKind.ALLERGEN
    )


def _caveat(row: HarvestCaveat) -> Caveat:
    """Carry one published caveat across verbatim."""
    return Caveat(
        position=row.position,
        heading=row.heading,
        text=row.text,
        source_url=row.source_url,
        harvested_at=row.harvested_at,
    )


def _stores(policy: PolicyDataset) -> tuple[Store, ...]:
    """Join each store's address, its name and its week into one row."""
    profiles = {profile.store_id: profile for profile in policy.store_profiles}
    hours = _hours_by_store(policy.store_hours)
    return tuple(
        _store(store, profiles.get(store.store_id), hours.get(store.store_id, ()))
        for store in sorted(policy.stores, key=lambda store: store.store_id)
    )


def _hours_by_store(
    rows: Sequence[HarvestStoreHours],
) -> Mapping[int, tuple[StoreHours, ...]]:
    """Group opening times by store, in the published order of the week."""
    grouped: dict[int, list[HarvestStoreHours]] = {}
    for row in rows:
        grouped.setdefault(row.store_id, []).append(row)
    return {
        store_id: tuple(
            StoreHours(
                day_of_week=row.day_of_week,
                opens=row.opens,
                closes=row.closes,
                is_published=row.is_published,
            )
            for row in sorted(days, key=lambda row: DAYS.index(row.day_of_week))
        )
        for store_id, days in grouped.items()
    }


def _store(
    store: HarvestStore,
    profile: StoreProfile | None,
    hours: tuple[StoreHours, ...],
) -> Store:
    """Merge one store's two documents into one row, citing both."""
    return Store(
        store_id=store.store_id,
        name=profile.name if profile is not None else None,
        street_address=store.street_address,
        city=store.city,
        region=store.region,
        postal_code=store.postal_code,
        hours=hours,
        page_url=store.page_url,
        source_url=store.source_url,
        harvested_at=store.harvested_at,
        profile_source_url=profile.source_url if profile is not None else None,
        profile_harvested_at=profile.harvested_at if profile is not None else None,
    )


__all__ = ["CALORIE_NUTRIENT_KEY", "build_catalog"]
