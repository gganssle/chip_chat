"""Turning the landed bytes into the eight nutrition and allergen tables.

Nothing here fetches. Four decisions are worth knowing before reading it, and
all four are about the same thing: not letting an absence become an assertion.

**Every item gets a statement about every allergen.** The item list is the
union of what the restaurant's menu sells and what the metadata document
describes, and the allergen table is that list crossed with the published
allergen codes. Napkins get four rows saying ``NOT_PUBLISHED``. Rows that
would otherwise be missing are the ones that get read as "nothing to worry
about", so there are none.

**Which codes are allergens is read, not recognised.** The allergen and diet
endpoint sorts the codes it uses into an ``allergens`` list and a ``diets``
list; that is Chipotle's classification and it is the one used. A code that
shares a tag group with a classified one inherits the group's kind, so an
allergen added to the "I'm Avoiding" group tomorrow is covered tomorrow
without anyone editing a constant here. Nothing matches on a group's display
name or on the spelling of a code — ``dair`` is an allergen because the chart
publishes it as one, not because it looks like "dairy".

**The two documents are checked against each other rather than merged.** They
describe overlapping sets of items, and where they overlap they agree about
allergens exactly — today, on all twenty-six shared foods. That agreement is
asserted on every run: if it ever breaks, this raises instead of quietly
preferring one document, because a harvest that picks a winner between two
disagreeing allergen sources is a harvest that has started guessing about
safety data. They do *not* agree about diets, which is why the diet table
records the document alongside the answer and merges nothing.

**Two ambiguities stop the harvest.** Two chart lines that share an item
identifier but mark different allergens cannot both be that identifier's
answer, and a chart that publishes no allergen codes at all is a chart whose
shape has changed. Both raise.

The one thing here that is derived rather than read is the tag-kind
inheritance described above, and the cross-document agreement it relies on.
Everything else is a copy of a published value with its provenance attached.
"""

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from chip_chat.harvest.cache import CachedDocument
from chip_chat.harvest.sources.chipotle.caveats import parse_caveats
from chip_chat.harvest.sources.chipotle.errors import ChipotleSourceError
from chip_chat.harvest.sources.chipotle.nutrition import NutritionDocuments
from chip_chat.harvest.sources.chipotle.nutrition_records import (
    AllergenChartRow,
    AllergenStatus,
    Caveat,
    DietaryTag,
    DietStatus,
    ItemAllergen,
    ItemDiet,
    ItemGroupCalories,
    ItemNutrient,
    Nutrient,
    NutritionDataset,
    TagKind,
)
from chip_chat.harvest.sources.chipotle.parse import ITEM_SECTIONS


def parse_nutrition(documents: NutritionDocuments) -> NutritionDataset:
    """Parse the harvested documents into the eight tables.

    Args:
        documents: What :func:`~...nutrition.harvest_nutrition` or
            :func:`~...nutrition.load_nutrition` returned.

    Returns:
        The dataset, every table sorted so two runs produce identical bytes.

    Raises:
        ChipotleSourceError: If a document is not the shape this source
            expects, if the chart publishes no allergens, if two chart lines
            sharing an identifier mark different allergens, or if the two
            documents disagree about an item's allergens.
    """
    metadata = _decode(documents.nutrition)
    if not isinstance(metadata, Mapping):
        raise ChipotleSourceError(
            f"{documents.nutrition.source_url} is not a menu metadata document"
        )
    chart_payload = _decode(documents.allergen_chart)
    if not isinstance(chart_payload, Mapping):
        raise ChipotleSourceError(
            f"{documents.allergen_chart.source_url} is not an allergen document"
        )

    nutrients = _parse_nutrients(metadata, documents.nutrition)
    item_nutrition = _parse_item_nutrition(metadata, nutrients, documents.nutrition)
    group_calories = _parse_group_calories(metadata, documents.nutrition)
    chart = _parse_chart(chart_payload, documents.allergen_chart)

    chart_allergens, chart_diets = _chart_vocabulary(chart)
    if not chart_allergens:
        raise ChipotleSourceError(
            f"{documents.allergen_chart.source_url} classified none of its "
            f"codes as allergens; the document's shape has changed and this "
            f"dataset would ship with an empty allergen table"
        )
    kinds = _tag_kinds(metadata, chart_allergens, chart_diets, documents.allergen_chart)
    tags = _parse_tags(metadata, chart, kinds, documents)

    metadata_tags = _metadata_item_tags(metadata)
    chart_tags = _chart_item_tags(chart, documents.allergen_chart)
    items = _item_universe(documents, metadata_tags, chart_tags)

    allergen_codes = tuple(
        sorted(code for code, kind in kinds.items() if kind is TagKind.ALLERGEN)
    )
    diet_codes = tuple(
        sorted(code for code, kind in kinds.items() if kind is not TagKind.ALLERGEN)
    )
    _require_agreement(
        metadata_tags, chart_tags, allergen_codes, documents.allergen_chart.source_url
    )

    return NutritionDataset(
        nutrients=nutrients,
        item_nutrition=item_nutrition,
        item_group_calories=group_calories,
        dietary_tags=tags,
        item_allergens=_item_allergens(
            items, allergen_codes, metadata_tags, chart_tags, documents
        ),
        item_diets=_item_diets(items, diet_codes, metadata_tags, chart_tags, documents),
        allergen_chart=chart,
        caveats=_parse_caveat_rows(documents.allergen_page),
        allergen_codes=allergen_codes,
    )


def _decode(document: CachedDocument) -> Any:
    """Parse a cached body as JSON, keeping every number's own text.

    ``parse_float=Decimal`` for the same reason the menu parser does it: a
    published 0.5 gram of saturated fat must come back out as ``0.5``, not as
    whatever the nearest binary float rounds to.
    """
    try:
        return json.loads(document.content, parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise ChipotleSourceError(
            f"{document.source_url} is not JSON: {error}"
        ) from error


def _parse_nutrients(
    metadata: Mapping[str, Any], document: CachedDocument
) -> tuple[Nutrient, ...]:
    """Read the published nutrient vocabulary, labels and units included.

    A section either is a nutrient itself — ``Total Fat``, which has a key and
    a figure — or is only a heading over components, as ``Vitamins & Minerals``
    is. The second kind contributes its components and no row of its own,
    because there is no ``vitamins`` figure to hold.
    """
    rows: list[Nutrient] = []
    order = 0
    for section in metadata.get("nutritionDetailSections") or ():
        if not isinstance(section, Mapping):
            continue
        section_name = str(section.get("name", ""))
        key = _text_or_none(section.get("key"))
        if key is not None:
            rows.append(
                Nutrient(
                    nutrient_key=key,
                    name=section_name,
                    unit=_text_or_none(section.get("unit")),
                    section_key=None,
                    section_name=section_name,
                    sort_order=order,
                    source_url=document.source_url,
                    harvested_at=document.harvested_at,
                )
            )
            order += 1
        for component in section.get("components") or ():
            if not isinstance(component, Mapping):
                continue
            component_key = _text_or_none(component.get("key"))
            if component_key is None:
                continue
            rows.append(
                Nutrient(
                    nutrient_key=component_key,
                    name=str(component.get("name", "")),
                    unit=_text_or_none(component.get("unit")),
                    section_key=key,
                    section_name=section_name,
                    sort_order=order,
                    source_url=document.source_url,
                    harvested_at=document.harvested_at,
                )
            )
            order += 1
    if not rows:
        raise ChipotleSourceError(
            f"{document.source_url} published no nutrient definitions; the "
            f"figures on its items would have no labels or units"
        )
    return tuple(sorted(rows, key=lambda row: row.nutrient_key))


def _parse_item_nutrition(
    metadata: Mapping[str, Any],
    nutrients: tuple[Nutrient, ...],
    document: CachedDocument,
) -> tuple[ItemNutrient, ...]:
    """Read every published figure, one row per item per known nutrient.

    Dense over the vocabulary rather than over what each item happened to
    publish, so an item that omits a nutrient produces an explicit null
    instead of a hole a reader has to notice. A figure the item publishes
    under a key the vocabulary does not define is kept too, with a null unit —
    dropping it would be discarding a published fact because our labels are
    out of date.
    """
    units = {row.nutrient_key: row.unit for row in nutrients}
    rows: list[ItemNutrient] = []
    for item_id, raw in _metadata_items(metadata):
        published = raw.get("nutrition")
        published = published if isinstance(published, Mapping) else {}
        portion = raw.get("portion")
        portion = portion if isinstance(portion, Mapping) else {}
        portion_unit = _text_or_none(portion.get("unit"))
        portion_value = _decimal_or_none(portion.get("value"))
        for key in sorted(units.keys() | published.keys()):
            rows.append(
                ItemNutrient(
                    item_id=item_id,
                    nutrient_key=key,
                    value=_decimal_or_none(published.get(key)),
                    unit=units.get(key),
                    portion_unit=portion_unit,
                    portion_value=portion_value,
                    source_url=document.source_url,
                    harvested_at=document.harvested_at,
                )
            )
    return tuple(sorted(rows, key=lambda row: (row.item_id, row.nutrient_key)))


def _parse_group_calories(
    metadata: Mapping[str, Any], document: CachedDocument
) -> tuple[ItemGroupCalories, ...]:
    """Read the published calorie ranges for groups of interchangeable items."""
    rows: list[ItemGroupCalories] = []
    groups = metadata.get("itemGroups")
    if not isinstance(groups, Mapping):
        return ()
    for key, raw in groups.items():
        if not isinstance(raw, Mapping):
            continue
        calories = raw.get("calories")
        calories = calories if isinstance(calories, Mapping) else {}
        rows.append(
            ItemGroupCalories(
                group_key=str(key),
                display_name=_text_or_none(raw.get("displayName")),
                default_item_id=_text_or_none(raw.get("defaultItemId")),
                calories_min=_decimal_or_none(calories.get("min")),
                calories_max=_decimal_or_none(calories.get("max")),
                display_range_format=_bool_or_none(raw.get("displayRangeFormat")),
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
        )
    return tuple(sorted(rows, key=lambda row: row.group_key))


def _parse_chart(
    payload: Mapping[str, Any], document: CachedDocument
) -> tuple[AllergenChartRow, ...]:
    """Read the published allergen chart, one row per published line."""
    rows: list[AllergenChartRow] = []
    for position, raw in enumerate(payload.get("allergens") or ()):
        if not isinstance(raw, Mapping):
            continue
        rows.append(
            AllergenChartRow(
                sort_order=_int_or_none(raw.get("sortOrder")) or position,
                name=str(raw.get("name", "")),
                menu_item_id=_text_or_none(raw.get("menuItemId")),
                allergen_codes=_codes(raw.get("allergens")),
                diet_codes=_codes(raw.get("diets")),
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
        )
    if not rows:
        raise ChipotleSourceError(
            f"{document.source_url} published no allergen chart lines"
        )
    return tuple(sorted(rows, key=lambda row: (row.sort_order, row.name)))


def _chart_vocabulary(
    chart: Iterable[AllergenChartRow],
) -> tuple[frozenset[str], frozenset[str]]:
    """Return the codes the chart publishes as allergens, and as diets."""
    allergens: set[str] = set()
    diets: set[str] = set()
    for row in chart:
        allergens.update(row.allergen_codes)
        diets.update(row.diet_codes)
    return frozenset(allergens), frozenset(diets)


def _tag_kinds(
    metadata: Mapping[str, Any],
    chart_allergens: frozenset[str],
    chart_diets: frozenset[str],
    chart_document: CachedDocument,
) -> dict[str, TagKind | None]:
    """Classify every published tag code as an allergen, a diet, or neither.

    Two published facts do the work. The chart sorts the codes it uses into
    allergens and diets. The metadata sorts codes into tag groups. A group
    whose codes include a chart allergen is an allergen group, and every code
    in it is an allergen — which is how a fifth allergen added to that group
    is covered without anyone here noticing.

    Raises:
        ChipotleSourceError: If one code is published as both an allergen and
            a diet, or if a group mixes the two. Either would mean the
            classification this dataset rests on has stopped being a
            classification.
    """
    overlap = chart_allergens & chart_diets
    if overlap:
        raise ChipotleSourceError(
            f"{chart_document.source_url} publishes {sorted(overlap)} as both "
            f"an allergen and a diet; the classification this dataset rests "
            f"on no longer holds"
        )

    kinds: dict[str, TagKind | None] = {}
    for group_id, group in _tag_groups(metadata):
        codes = {
            code
            for code in (
                _text_or_none(tag.get("tagCode"))
                for tag in group.get("tags") or ()
                if isinstance(tag, Mapping)
            )
            if code is not None
        }
        is_allergen = bool(codes & chart_allergens)
        is_diet = bool(codes & chart_diets)
        if is_allergen and is_diet:
            raise ChipotleSourceError(
                f"tag group {group_id!r} mixes codes the chart publishes as "
                f"allergens with codes it publishes as diets; a group can no "
                f"longer stand in for either"
            )
        group_kind = (
            TagKind.ALLERGEN if is_allergen else TagKind.DIET if is_diet else None
        )
        for code in codes:
            kinds[code] = group_kind

    for code in sorted(chart_allergens):
        kinds.setdefault(code, TagKind.ALLERGEN)
    for code in sorted(chart_diets):
        kinds.setdefault(code, TagKind.DIET)
    for _, raw in _metadata_items(metadata):
        for code in _codes(raw.get("dietaryTags")):
            kinds.setdefault(code, None)
    return kinds


def _parse_tags(
    metadata: Mapping[str, Any],
    chart: tuple[AllergenChartRow, ...],
    kinds: Mapping[str, TagKind | None],
    documents: NutritionDocuments,
) -> tuple[DietaryTag, ...]:
    """Read the published tag vocabulary, labelled and unlabelled alike.

    A code the metadata never names — ``whol`` on the chart, ``wh30`` on the
    items, both of them Whole30 to a reader and neither of them said so in
    print — gets a row with a null name rather than a guessed one or no row
    at all.
    """
    published: dict[str, DietaryTag] = {}
    for group_id, group in _tag_groups(metadata):
        for tag in group.get("tags") or ():
            if not isinstance(tag, Mapping):
                continue
            code = _text_or_none(tag.get("tagCode"))
            if code is None:
                continue
            published[code] = DietaryTag(
                tag_code=code,
                tag_name=_text_or_none(tag.get("tagName")),
                kind=kinds.get(code),
                group_id=group_id,
                group_name=_text_or_none(group.get("groupName")),
                group_subheader=_text_or_none(group.get("subheader")),
                badge_text=_text_or_none(
                    tag.get("preferredBadgeText") or group.get("badgeText")
                ),
                sort_order=_int_or_none(tag.get("sortOrder")),
                source_url=documents.nutrition.source_url,
                harvested_at=documents.nutrition.harvested_at,
            )
    for code in kinds:
        if code in published:
            continue
        document = (
            documents.allergen_chart
            if any(code in row.allergen_codes or code in row.diet_codes for row in chart)
            else documents.nutrition
        )
        published[code] = DietaryTag(
            tag_code=code,
            tag_name=None,
            kind=kinds.get(code),
            group_id=None,
            group_name=None,
            group_subheader=None,
            badge_text=None,
            sort_order=None,
            source_url=document.source_url,
            harvested_at=document.harvested_at,
        )
    return tuple(sorted(published.values(), key=lambda row: row.tag_code))


def _metadata_item_tags(metadata: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    """Return the tag codes the metadata publishes for each item it describes.

    An item the metadata describes but tags with nothing maps to an empty
    set, which is a different fact from an item the metadata does not describe
    at all and therefore is not a key here.
    """
    return {
        item_id: frozenset(_codes(raw.get("dietaryTags")))
        for item_id, raw in _metadata_items(metadata)
    }


def _chart_item_tags(
    chart: tuple[AllergenChartRow, ...], document: CachedDocument
) -> dict[str, frozenset[str]]:
    """Return the tag codes the chart publishes against each item identifier.

    Several chart lines may share one identifier — "Crispy Corn Tortilla" and
    "Tortilla Chips" both answer to ``CMG-1002`` — so the codes returned for an
    identifier are the union across its lines. For diets that union is the
    answer; for allergens a union would be a merge of two different foods'
    safety data, so lines that disagree raise instead. The chart itself is kept
    line by line in ``allergen_chart``, where nothing is unioned at all.

    Raises:
        ChipotleSourceError: If two chart lines share an identifier and mark
            different allergens. They cannot both be that identifier's answer,
            and choosing between two published allergen sets is not something
            a parser should do quietly.
    """
    allergens: dict[str, frozenset[str]] = {}
    combined: dict[str, frozenset[str]] = {}
    lines: dict[str, str] = {}
    for row in chart:
        if row.menu_item_id is None:
            continue
        marked = frozenset(row.allergen_codes)
        seen = allergens.get(row.menu_item_id)
        if seen is not None and seen != marked:
            raise ChipotleSourceError(
                f"{document.source_url}: {row.menu_item_id} is published twice "
                f"with different allergens — {lines[row.menu_item_id]!r} marks "
                f"{sorted(seen)} and {row.name!r} marks {sorted(marked)}"
            )
        allergens[row.menu_item_id] = marked
        lines[row.menu_item_id] = row.name
        combined[row.menu_item_id] = (
            combined.get(row.menu_item_id, frozenset())
            | marked
            | frozenset(row.diet_codes)
        )
    return combined


def _item_universe(
    documents: NutritionDocuments,
    metadata_tags: Mapping[str, frozenset[str]],
    chart_tags: Mapping[str, frozenset[str]],
) -> tuple[str, ...]:
    """Return every item this dataset makes a statement about.

    The restaurant's own menu, its modifiers included, plus everything either
    allergen document describes. The menu is in there so that a thing a
    customer can put in a basket always has a row, even when — as with
    napkins — the row says nothing is published.
    """
    items: set[str] = set(metadata_tags) | set(chart_tags)
    for document in documents.menus:
        menu = _decode(document)
        if not isinstance(menu, Mapping):
            raise ChipotleSourceError(f"{document.source_url} is not a menu")
        for section in ITEM_SECTIONS:
            for raw in menu.get(section) or ():
                if not isinstance(raw, Mapping) or not raw.get("itemId"):
                    continue
                items.add(str(raw["itemId"]))
                for content in raw.get("contents") or ():
                    if isinstance(content, Mapping) and content.get("itemId"):
                        items.add(str(content["itemId"]))
    return tuple(sorted(items))


def _require_agreement(
    metadata_tags: Mapping[str, frozenset[str]],
    chart_tags: Mapping[str, frozenset[str]],
    allergen_codes: tuple[str, ...],
    chart_url: str,
) -> None:
    """Refuse a harvest whose two allergen documents contradict each other.

    They agree today about every food both describe. Asserting it on each run
    is what keeps the single answer in ``item_allergens`` honest: the moment
    the two documents diverge, the choice of which to believe stops being
    arbitrary and starts being a judgement someone has to make in daylight.
    """
    codes = frozenset(allergen_codes)
    for item_id, chart_marks in chart_tags.items():
        metadata_marks = metadata_tags.get(item_id)
        if metadata_marks is None:
            continue
        if chart_marks & codes != metadata_marks & codes:
            raise ChipotleSourceError(
                f"{chart_url}: the allergen chart marks {item_id} with "
                f"{sorted(chart_marks & codes)} and the menu metadata marks it "
                f"with {sorted(metadata_marks & codes)}; the two published "
                f"sources disagree about a safety fact"
            )


def _item_allergens(
    items: tuple[str, ...],
    allergen_codes: tuple[str, ...],
    metadata_tags: Mapping[str, frozenset[str]],
    chart_tags: Mapping[str, frozenset[str]],
    documents: NutritionDocuments,
) -> tuple[ItemAllergen, ...]:
    """Build the three-valued allergen answer for every item and every allergen.

    The metadata is consulted first because it describes the most items; the
    chart second, for the handful it names that the metadata does not. They
    are known to agree by the time this runs, so which is asked first changes
    only the ``source_url`` a row cites, never its status.
    """
    rows: list[ItemAllergen] = []
    for item_id in items:
        marks = metadata_tags.get(item_id)
        document = documents.nutrition
        if marks is None:
            marks = chart_tags.get(item_id)
            document = documents.allergen_chart
        for code in allergen_codes:
            if marks is None:
                status = AllergenStatus.NOT_PUBLISHED
                stated_in = documents.nutrition
            else:
                status = (
                    AllergenStatus.CONTAINS
                    if code in marks
                    else AllergenStatus.NOT_LISTED
                )
                stated_in = document
            rows.append(
                ItemAllergen(
                    item_id=item_id,
                    allergen_code=code,
                    status=status,
                    source_url=stated_in.source_url,
                    harvested_at=stated_in.harvested_at,
                )
            )
    return tuple(rows)


def _item_diets(
    items: tuple[str, ...],
    diet_codes: tuple[str, ...],
    metadata_tags: Mapping[str, frozenset[str]],
    chart_tags: Mapping[str, frozenset[str]],
    documents: NutritionDocuments,
) -> tuple[ItemDiet, ...]:
    """Build one diet statement per item per code *per document*.

    Unmerged, because the two documents genuinely differ here: the chart lists
    nine foods as Whole30 under the code ``whol`` and the metadata lists two
    under ``wh30``, and no published thing says those are the same diet. A
    reader who wants both gets both, each citing where it came from.
    """
    rows: list[ItemDiet] = []
    surfaces = (
        (documents.nutrition, metadata_tags),
        (documents.allergen_chart, chart_tags),
    )
    for item_id in items:
        for document, published in surfaces:
            marks = published.get(item_id)
            for code in diet_codes:
                if marks is None:
                    status = DietStatus.NOT_PUBLISHED
                else:
                    status = DietStatus.LISTED if code in marks else DietStatus.NOT_LISTED
                rows.append(
                    ItemDiet(
                        item_id=item_id,
                        diet_code=code,
                        status=status,
                        source_url=document.source_url,
                        harvested_at=document.harvested_at,
                    )
                )
    return tuple(
        sorted(rows, key=lambda row: (row.item_id, row.diet_code, row.source_url))
    )


def _parse_caveat_rows(document: CachedDocument) -> tuple[Caveat, ...]:
    """Read the published prose from the allergen page, verbatim."""
    return tuple(
        Caveat(
            position=position,
            heading=block.heading,
            text=block.text,
            source_url=document.source_url,
            harvested_at=document.harvested_at,
        )
        for position, block in enumerate(
            parse_caveats(document.text, document.source_url)
        )
    )


def _metadata_items(
    metadata: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    """Return the metadata's item map as sorted ``(item_id, entry)`` pairs."""
    items = metadata.get("items")
    if not isinstance(items, Mapping):
        return []
    return sorted(
        (str(item_id), raw) for item_id, raw in items.items() if isinstance(raw, Mapping)
    )


def _tag_groups(
    metadata: Mapping[str, Any],
) -> list[tuple[str | None, Mapping[str, Any]]]:
    """Return the published tag groups as ``(group_id, group)`` pairs."""
    return [
        (_text_or_none(group.get("id")), group)
        for group in metadata.get("dietaryTagGroups") or ()
        if isinstance(group, Mapping)
    ]


def _codes(value: Any) -> tuple[str, ...]:
    """Return a published list of tag codes, dropping nothing but blanks."""
    if not isinstance(value, list):
        return ()
    codes = (_text_or_none(code) for code in value)
    return tuple(code for code in codes if code is not None)


def _decimal_or_none(value: Any) -> Decimal | None:
    """Return a published number exactly, or ``None`` if none was published.

    ``None`` and ``Decimal("0")`` are the two answers this function exists to
    keep apart. Nothing here defaults, coerces or falls back.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError:
            return None
    return None


def _bool_or_none(value: Any) -> bool | None:
    """Return a published boolean, or ``None`` where none was published."""
    return value if isinstance(value, bool) else None


def _int_or_none(value: Any) -> int | None:
    """Return ``value`` as an integer, or ``None`` if it was omitted."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float | Decimal):
        return int(value)
    return None


def _text_or_none(value: Any) -> str | None:
    """Return a trimmed string, or ``None`` where nothing was published."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
