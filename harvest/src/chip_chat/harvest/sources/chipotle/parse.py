"""Turning the landed bytes into the ten tables.

Nothing here fetches. It is handed :class:`~...menu.MenuDocuments` and reads
them, which is what makes fixing a parser bug cost a re-run instead of another
pass over someone else's servers, and what makes "re-running the parser
reproduces the dataset exactly" a claim with nothing in it to drift.

Two decisions worth knowing before reading the code.

**A modifier is a menu item too.** Black beans have an identifier, a name, a
published price and, sometimes, a paragraph about where they are grown, so
they get a row in ``menu_items`` alongside the Steak Burrito, with a null
category to say they are not orderable on their own. The ``modifiers`` table
is then purely a relation — which item may go on which, in which slot — and
every price in the catalogue joins to exactly one item.

**Structure comes from the first restaurant, prices from all of them.** A
restaurant's menu response is both a catalogue and a price list. Harvest three
stores and you get three catalogues that are the same catalogue, so the first
restaurant asked defines identity and structure and the rest contribute only
their prices and their availability. Where a later restaurant publishes an
item the first does not, the item is taken from the first restaurant that has
it; ordering the identifiers is therefore a decision, not a detail.

**Descriptions are joined exactly or not at all.** Chipotle publishes prose
about ingredients and prose about meals; it publishes none about a Steak
Burrito. The ingredient corpus links an ingredient to every item that
*contains* it, which is a containment relation and not a description — black
pepper lists the Steak Burrito. So an item takes an ingredient's description
only when the ingredient's own title is that item's name *and* the ingredient
lists the item, and otherwise its description stays null. A harvester that
guessed here would be putting words in a restaurant's mouth about food, which
is the one place a demo cannot afford to be creative.

**Two invariants are checked rather than assumed**, because both are true of
the data today and neither is promised by anyone. A restaurant whose menu is
entirely free is a dead store, not a bargain, and is refused. And a modifier
priced two different ways within one restaurant would break the flattening
that lets one price table cover items and modifiers alike, so it raises
instead of silently keeping whichever row came last.
"""

import json
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any

from chip_chat.harvest.cache import CachedDocument
from chip_chat.harvest.sources.chipotle.errors import ChipotleSourceError
from chip_chat.harvest.sources.chipotle.menu import MenuDocuments
from chip_chat.harvest.sources.chipotle.records import (
    Ingredient,
    ItemIngredients,
    ItemPrice,
    Meal,
    MealContent,
    MealPrice,
    MenuDataset,
    MenuItem,
    Modifier,
    ModifierGroup,
    PortionOption,
)

ITEM_SECTIONS = ("entrees", "sides", "drinks", "nonFoodItems")
"""The four lists a restaurant's menu response is divided into."""


def parse_menu(documents: MenuDocuments) -> MenuDataset:
    """Parse the harvested documents into the seven tables.

    Args:
        documents: What :func:`~...menu.harvest_menu` or
            :func:`~...menu.load_menu` returned.

    Returns:
        The dataset, every table sorted so two runs produce identical bytes.

    Raises:
        ChipotleSourceError: If a restaurant published no menu, published a
            menu with no prices in it, or priced one modifier two ways.
    """
    if not documents.menus:
        raise ChipotleSourceError("no restaurant menus were harvested")

    ingredients, item_ingredients, descriptions = _parse_ingredients(
        documents.ingredients
    )

    items: dict[str, MenuItem] = {}
    groups: dict[tuple[str, str], ModifierGroup] = {}
    modifiers: dict[tuple[str, str], Modifier] = {}
    options: dict[tuple[str, str, int], PortionOption] = {}
    meals: dict[str, Meal] = {}
    meal_contents: dict[tuple[str, int], MealContent] = {}
    meal_prices: list[MealPrice] = []
    prices: list[ItemPrice] = []
    restaurant_ids: list[int] = []

    for document in documents.menus:
        menu = _decode(document)
        restaurant_id = _restaurant_id(menu, document.source_url)
        restaurant_ids.append(restaurant_id)
        priced: dict[str, ItemPrice] = {}
        published = list(_items_in(menu))

        for raw in published:
            item = _menu_item(raw, descriptions, document)
            items.setdefault(item.item_id, item)
            _record_price(priced, raw, restaurant_id, document)

        for raw in published:
            for group in _modifier_groups(raw, document):
                groups.setdefault((group.item_id, group.group_name), group)
            for modifier in _modifiers(raw, document):
                modifiers.setdefault(
                    (modifier.item_id, modifier.modifier_item_id), modifier
                )
            for option in _portion_options(raw, document):
                options.setdefault(
                    (option.item_id, option.modifier_item_id, option.option_id),
                    option,
                )
            for content in _contents_of(raw):
                component = _menu_item(content, descriptions, document)
                items.setdefault(component.item_id, component)
                _record_price(priced, content, restaurant_id, document)

        _require_prices(priced, restaurant_id, document.source_url)
        prices.extend(priced.values())

    for restaurant_id, document in zip(restaurant_ids, documents.meals, strict=True):
        for meal, contents, price in _parse_meals(document, restaurant_id):
            meals.setdefault(meal.meal_id, meal)
            for line in contents:
                meal_contents.setdefault((line.meal_id, line.position), line)
            meal_prices.append(price)

    return MenuDataset(
        restaurant_ids=tuple(restaurant_ids),
        reference_restaurant_id=restaurant_ids[0],
        menu_items=tuple(sorted(items.values(), key=lambda row: row.item_id)),
        item_prices=tuple(
            sorted(prices, key=lambda row: (row.restaurant_id, row.item_id))
        ),
        modifier_groups=tuple(sorted(groups.values(), key=_group_key)),
        modifiers=tuple(sorted(modifiers.values(), key=_modifier_key)),
        portion_options=tuple(sorted(options.values(), key=_option_key)),
        meals=tuple(sorted(meals.values(), key=lambda row: row.meal_id)),
        meal_contents=tuple(
            sorted(meal_contents.values(), key=lambda row: (row.meal_id, row.position))
        ),
        meal_prices=tuple(
            sorted(meal_prices, key=lambda row: (row.restaurant_id, row.meal_id))
        ),
        ingredients=ingredients,
        item_ingredients=item_ingredients,
    )


def _group_key(row: ModifierGroup) -> tuple[str, str]:
    return (row.item_id, row.group_name)


def _modifier_key(row: Modifier) -> tuple[str, str]:
    return (row.item_id, row.modifier_item_id)


def _option_key(row: PortionOption) -> tuple[str, str, int]:
    return (row.item_id, row.modifier_item_id, row.option_id)


def _decode(document: CachedDocument) -> Any:
    """Parse a cached body as JSON, keeping every number's own text.

    ``parse_float=Decimal`` is the whole reason this is not
    :meth:`~chip_chat.harvest.cache.CachedDocument.json`. A price that goes
    through a binary float on its way into the dataset is a price that can
    come back out with a different last digit, and this dataset's contract is
    that it does not change between runs.
    """
    try:
        return json.loads(document.content, parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise ChipotleSourceError(
            f"{document.source_url} is not JSON: {error}"
        ) from error


def _restaurant_id(menu: Any, source_url: str) -> int:
    """Return the restaurant the menu says it is for."""
    if not isinstance(menu, Mapping) or "restaurantId" not in menu:
        raise ChipotleSourceError(f"{source_url} carries no restaurantId")
    try:
        return int(menu["restaurantId"])
    except (TypeError, ValueError) as error:
        raise ChipotleSourceError(
            f"{source_url} has a restaurantId that is not a number: "
            f"{menu['restaurantId']!r}"
        ) from error


def _items_in(menu: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Yield every top-level item in a restaurant's menu, section by section."""
    for section in ITEM_SECTIONS:
        for raw in menu.get(section) or ():
            if isinstance(raw, Mapping):
                yield raw


def _menu_item(
    raw: Mapping[str, Any],
    descriptions: Mapping[tuple[str, str], str],
    document: CachedDocument,
) -> MenuItem:
    """Build one item row from a menu entry."""
    item_id = str(raw["itemId"])
    name = str(raw.get("itemName", ""))
    return MenuItem(
        item_id=item_id,
        name=name,
        category=_text_or_none(raw.get("itemCategory")),
        item_type=str(raw.get("itemType", "")),
        primary_filling=_text_or_none(raw.get("primaryFillingName")),
        description=descriptions.get((name.strip().lower(), item_id)),
        max_quantity=_int_or_none(raw.get("maxQuantity")),
        max_contents=_int_or_none(raw.get("maxContents")),
        max_customizations=_int_or_none(raw.get("maxCustomizations")),
        max_on_the_side_customizations=_int_or_none(
            raw.get("maxOnTheSideCustomizations")
        ),
        max_extras=_int_or_none(raw.get("maxExtras")),
        max_halfs=_int_or_none(raw.get("maxHalfs")),
        max_extras_plus_halfs=_int_or_none(raw.get("maxExtrasPlusHalfs")),
        source_url=document.source_url,
        harvested_at=document.harvested_at,
    )


def _modifier_groups(
    raw: Mapping[str, Any], document: CachedDocument
) -> Iterator[ModifierGroup]:
    """Yield the slots on one item.

    A menu declares bounds for some groups and merely puts modifiers in
    others, so the row set is the union of both and ``min_quantity`` is
    ``None`` where nothing was declared.
    """
    item_id = str(raw["itemId"])
    declared = {
        str(group["contentGroupName"]): group
        for group in raw.get("contentGroups") or ()
        if isinstance(group, Mapping) and group.get("contentGroupName")
    }
    referenced = {
        str(content["contentGroupName"])
        for content in raw.get("contents") or ()
        if isinstance(content, Mapping) and content.get("contentGroupName")
    }
    for name in sorted(declared.keys() | referenced):
        bounds = declared.get(name, {})
        yield ModifierGroup(
            item_id=item_id,
            group_name=name,
            min_quantity=_int_or_none(bounds.get("minQuantity")),
            max_quantity=_int_or_none(bounds.get("maxQuantity")),
            source_url=document.source_url,
            harvested_at=document.harvested_at,
        )


def _modifiers(raw: Mapping[str, Any], document: CachedDocument) -> Iterator[Modifier]:
    """Yield what may be added to one item."""
    item_id = str(raw["itemId"])
    for content in _contents_of(raw):
        yield Modifier(
            item_id=item_id,
            modifier_item_id=str(content["itemId"]),
            name=str(content.get("itemName", "")),
            modifier_type=str(content.get("itemType", "")),
            group_name=_text_or_none(content.get("contentGroupName")),
            is_default=bool(content.get("defaultContent", False)),
            counts_toward_content_max=_number(content.get("countTowardsContentMax")),
            counts_toward_customization_max=_number(
                content.get("countTowardsCustomizationMax")
            ),
            pricing_reference_item_id=_text_or_none(
                content.get("pricingReferenceItemId")
            ),
            source_url=document.source_url,
            harvested_at=document.harvested_at,
        )


def _portion_options(
    raw: Mapping[str, Any], document: CachedDocument
) -> Iterator[PortionOption]:
    """Yield how much of each modifier may be asked for."""
    item_id = str(raw["itemId"])
    for content in _contents_of(raw):
        modifier_item_id = str(content["itemId"])
        for option in content.get("customizations") or ():
            if not isinstance(option, Mapping) or option.get("id") is None:
                continue
            yield PortionOption(
                item_id=item_id,
                modifier_item_id=modifier_item_id,
                option_id=int(option["id"]),
                name=str(option.get("name", "")),
                counts_toward_customization_max=_number(
                    option.get("countTowardsCustomizationMax")
                ),
                counts_toward_on_the_side_max=_number(
                    option.get("countTowardsOnTheSideCustomizationMax")
                ),
                counts_toward_content_max=_number(option.get("countTowardsContentMax")),
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )


def _contents_of(raw: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    """Yield an item's modifier entries, skipping anything malformed."""
    for content in raw.get("contents") or ():
        if isinstance(content, Mapping) and content.get("itemId"):
            yield content


def _record_price(
    priced: dict[str, ItemPrice],
    raw: Mapping[str, Any],
    restaurant_id: int,
    document: CachedDocument,
) -> None:
    """Record one price, refusing to overwrite a different one.

    Items and modifiers share an identifier namespace and, within a single
    restaurant, a single price each — a modifier costs the same whichever
    entree it goes on. That is what lets one table price both. It is checked
    here rather than trusted, because the day it stops being true this should
    stop the harvest rather than pick a winner.
    """
    item_id = str(raw["itemId"])
    price = ItemPrice(
        restaurant_id=restaurant_id,
        item_id=item_id,
        unit_price=_money(raw.get("unitPrice"), item_id, document.source_url),
        unit_delivery_price=_money(
            raw.get("unitDeliveryPrice"), item_id, document.source_url
        ),
        is_available=bool(raw.get("isItemAvailable", False)),
        eligible_for_delivery=bool(raw.get("eligibleForDelivery", False)),
        source_url=document.source_url,
        harvested_at=document.harvested_at,
    )
    existing = priced.get(item_id)
    if existing is None:
        priced[item_id] = price
        return
    if (existing.unit_price, existing.unit_delivery_price) != (
        price.unit_price,
        price.unit_delivery_price,
    ):
        raise ChipotleSourceError(
            f"{document.source_url}: restaurant {restaurant_id} prices "
            f"{item_id} both at {existing.unit_price} and at {price.unit_price}"
        )


def _require_prices(
    priced: Mapping[str, ItemPrice], restaurant_id: int, source_url: str
) -> None:
    """Refuse a restaurant whose entire menu is free.

    Chipotle answers for identifiers that are not serving customers, and what
    it answers is a complete menu with every price set to zero. Left alone
    that becomes a catalogue that confidently quotes $0.00.
    """
    if not priced:
        raise ChipotleSourceError(
            f"{source_url}: restaurant {restaurant_id} published no items"
        )
    if all(price.unit_price == 0 for price in priced.values()):
        raise ChipotleSourceError(
            f"{source_url}: restaurant {restaurant_id} priced every one of its "
            f"{len(priced)} items at zero, which means it is not open for "
            f"orders rather than that its food is free"
        )


def _parse_ingredients(
    document: CachedDocument,
) -> tuple[
    tuple[Ingredient, ...],
    tuple[ItemIngredients, ...],
    Mapping[tuple[str, str], str],
]:
    """Parse the ingredient metadata into rows, plus the exact descriptions.

    The third return value is keyed by ``(lowercased item name, item id)``,
    which is the exact join described in this module's docstring: an
    ingredient describes an item only when it both names it and lists it.
    Anything looser attaches the paragraph about black pepper to a burrito.
    """
    payload = _decode(document)
    if not isinstance(payload, Mapping):
        raise ChipotleSourceError(f"{document.source_url} is not an ingredient document")

    ingredients: list[Ingredient] = []
    descriptions: dict[tuple[str, str], str] = {}
    for raw in payload.get("ingredients") or ():
        if not isinstance(raw, Mapping) or not raw.get("key"):
            continue
        used_in = tuple(str(item_id) for item_id in raw.get("menuItems") or ())
        title = str(raw.get("title", ""))
        description = _text_or_none(raw.get("subDescription"))
        ingredients.append(
            Ingredient(
                key=str(raw["key"]),
                title=title,
                description=description,
                fun_fact=_text_or_none(raw.get("factsDescription")),
                used_in_menu_item_ids=used_in,
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
        )
        if description is not None:
            for item_id in used_in:
                descriptions.setdefault((title.strip().lower(), item_id), description)

    item_ingredients: list[ItemIngredients] = []
    for group in payload.get("ingredientGroups") or ():
        if not isinstance(group, Mapping):
            continue
        title = str(group.get("title", ""))
        for member in group.get("items") or ():
            if not isinstance(member, Mapping) or not member.get("id"):
                continue
            item_ingredients.append(
                ItemIngredients(
                    group_title=title,
                    item_id=str(member["id"]),
                    ingredient_keys=tuple(
                        str(key) for key in member.get("ingredients") or ()
                    ),
                    source_url=document.source_url,
                    harvested_at=document.harvested_at,
                )
            )

    return (
        tuple(sorted(ingredients, key=lambda row: row.key)),
        tuple(sorted(item_ingredients, key=lambda row: (row.group_title, row.item_id))),
        descriptions,
    )


def _parse_meals(
    document: CachedDocument, restaurant_id: int
) -> Iterator[tuple[Meal, tuple[MealContent, ...], MealPrice]]:
    """Yield each preconfigured meal, its lines, and what it cost here.

    Args:
        document: The cached response from one restaurant's meals endpoint.
        restaurant_id: The restaurant that answered.

    Yields:
        A meal, its contents in published order, and its price.

    Raises:
        ChipotleSourceError: If the response is not a list of meals.
    """
    payload = _decode(document)
    if not isinstance(payload, list):
        raise ChipotleSourceError(f"{document.source_url} is not a list of meals")
    for raw in payload:
        if not isinstance(raw, Mapping) or not raw.get("mealId"):
            continue
        meal_id = str(raw["mealId"])
        entree = raw.get("entree")
        entree = entree if isinstance(entree, Mapping) else {}
        meal = Meal(
            meal_id=meal_id,
            name=str(raw.get("mealName", "")),
            meal_type=str(raw.get("mealType", "")),
            description=_text_or_none(raw.get("description")),
            calories=_text_or_none(raw.get("calories")),
            dietary_tags=tuple(str(tag) for tag in raw.get("dietaryTags") or ()),
            entree_item_id=_text_or_none(entree.get("itemId")),
            sort_order=_int_or_none(raw.get("sortOrder")),
            source_url=document.source_url,
            harvested_at=document.harvested_at,
        )
        price = MealPrice(
            restaurant_id=restaurant_id,
            meal_id=meal_id,
            meal_price=_money(raw.get("mealPrice"), meal_id, document.source_url),
            meal_delivery_price=_money(
                raw.get("mealDeliveryPrice"), meal_id, document.source_url
            ),
            source_url=document.source_url,
            harvested_at=document.harvested_at,
        )
        yield meal, tuple(_meal_contents(meal_id, entree, document)), price


def _meal_contents(
    meal_id: str, entree: Mapping[str, Any], document: CachedDocument
) -> Iterator[MealContent]:
    """Yield a meal's lines, numbered by where they appear.

    The entree itself is line zero: a meal is its entree plus what goes in it,
    and dropping the entree would leave a bowl of toppings.
    """
    lines: list[Mapping[str, Any]] = []
    if entree.get("itemId"):
        lines.append(entree)
    lines.extend(
        content
        for content in entree.get("contents") or ()
        if isinstance(content, Mapping) and content.get("itemId")
    )
    for position, line in enumerate(lines):
        customization_id = _int_or_none(line.get("customizationId"))
        yield MealContent(
            meal_id=meal_id,
            position=position,
            item_id=str(line["itemId"]),
            name=str(line.get("itemName", "")),
            quantity=_int_or_none(line.get("quantity")),
            customization_id=customization_id or None,
            customization_name=_text_or_none(line.get("customizationName")),
            source_url=document.source_url,
            harvested_at=document.harvested_at,
        )


def _money(value: Any, item_id: str, source_url: str) -> Decimal:
    """Return a published price as an exact decimal."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise ChipotleSourceError(
        f"{source_url}: {item_id} has a price that is not a number: {value!r}"
    )


def _number(value: Any) -> float:
    """Return an allowance weight as a float, defaulting to zero."""
    if isinstance(value, bool) or value is None:
        return 0.0
    if isinstance(value, int | float | Decimal):
        return float(value)
    return 0.0


def _int_or_none(value: Any) -> int | None:
    """Return ``value`` as an integer, or ``None`` if the menu omitted it."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        return int(value)
    return None


def _text_or_none(value: Any) -> str | None:
    """Return a trimmed string, or ``None`` where the menu published nothing."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
