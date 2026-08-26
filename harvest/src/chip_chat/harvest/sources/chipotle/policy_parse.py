"""Turning the landed policy bytes into the ten tables of issue #21.

Nothing here fetches. Four decisions are worth knowing before reading it.

**Section boundaries are preserved, not re-derived.** The rewards terms arrive
as one authored block and leave as eighteen sections, because issue #21 says
the policy corpus chunks by section and a boundary thrown away at harvest time
cannot be recovered at index time. :mod:`sections` holds the rule.

**The published point costs are read, not reconstructed.** Chipotle's Rewards
Exchange itself is behind a login, but the rewards landing page publishes the
whole line-up with its prices in plain markup — 85 points for a side tortilla,
1,625 for an entrée — and that is what lands. Nothing here converts a number
of dollars into a number of points; the earn rate is published as prose and
stays prose, in the FAQ and in the terms, where an answer can quote it.

**A day nobody published hours for is a row saying so.** ``store_hours`` holds
seven rows per store whether or not the locator published seven, for the same
reason ``item_allergens`` holds a row per allergen: a missing row reads as
"nothing to worry about", and "we publish nothing about Sunday" is not the
same answer as "closed on Sunday".

**Thirty stores is checked here.** If the locator changes shape and the
selection comes back thin, this raises rather than shipping a ``stores`` table
too small for a home store to mean anything.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any

from chip_chat.harvest.cache import CachedDocument
from chip_chat.harvest.sources.chipotle.errors import ChipotleSourceError
from chip_chat.harvest.sources.chipotle.locator import DAYS, parse_store_page
from chip_chat.harvest.sources.chipotle.menu import REFERENCE_RESTAURANT_ID
from chip_chat.harvest.sources.chipotle.policy import PolicyDocuments
from chip_chat.harvest.sources.chipotle.policy_records import (
    MINIMUM_STORES,
    CateringOption,
    CateringPackage,
    FaqCategory,
    FaqEntry,
    PolicyDataset,
    PolicyDocument,
    PolicySection,
    Reward,
    Store,
    StoreHours,
    StoreProfile,
)
from chip_chat.harvest.sources.chipotle.sections import parse_document

TILE_CLASS = "aem-flip-tile"
"""The class on each reward in the published Rewards Exchange line-up."""

POINTS_CLASS = f"{TILE_CLASS}__points"
TITLE_CLASS = f"{TILE_CLASS}__title"

TILE_MARKERS = frozenset(
    {
        TILE_CLASS,
        f"{TILE_CLASS}__front",
        f"{TILE_CLASS}__flipped",
        f"{TILE_CLASS}__icon",
        f"{TILE_CLASS}__image",
        POINTS_CLASS,
        TITLE_CLASS,
    }
)
"""Every class the page marks a part of a reward tile with.

Each one starting is what tells the reader that the part before it has ended.
"""

CATERING_SLOTS = (
    "bases",
    "proteins",
    "toppings",
    "premiumToppings",
    "salsas",
    "tortillas",
)
"""The lists a catering package publishes its choosable components in.

Spelled as Chipotle spells them. A slot renamed here is a slot nobody can
check against the source document.
"""

_SLOT_COUNTS = {
    "bases": "baseCount",
    "proteins": "proteinCount",
    "toppings": "toppingCount",
    "premiumToppings": "premiumToppingCount",
    "salsas": "salsaCount",
    "tortillas": "tortillaCount",
}

_BLOCK_NODES = frozenset({"paragraph", "list-item", "heading"})
_LIST_NODES = frozenset({"unordered-list", "ordered-list"})
_INLINE_NODES = frozenset({"span", "text", "link"})


def parse_policy(documents: PolicyDocuments) -> PolicyDataset:
    """Parse the harvested documents into the ten tables.

    Args:
        documents: What :func:`~...policy.harvest_policy` or
            :func:`~...policy.load_policy` returned.

    Returns:
        The dataset, every table sorted so that two runs produce identical
        bytes.

    Raises:
        ChipotleSourceError: If a document is not the shape this source
            expects, if the rewards page publishes no rewards, or if fewer
            than :data:`~...policy_records.MINIMUM_STORES` stores came back.
    """
    policy_documents, policy_sections = _policy_documents(documents)
    faq_categories, faq_entries = _faq(documents.faq)
    packages, options = _catering(documents.catering_menu)
    stores, hours = _stores(documents.stores)
    profiles = _profiles(documents.store_profiles)

    if len(stores) < MINIMUM_STORES:
        raise ChipotleSourceError(
            f"the store locator yielded {len(stores)} stores, fewer than the "
            f"{MINIMUM_STORES} issue #21 requires; the locator's page layout "
            f"has probably changed"
        )
    reference = int(REFERENCE_RESTAURANT_ID)
    if not any(store.store_id == reference for store in stores):
        raise ChipotleSourceError(
            f"restaurant {reference} is the one the menu harvest prices at, "
            f"and no locator page published it; the harvested prices would "
            f"have no address to belong to"
        )

    return PolicyDataset(
        store_ids=tuple(store.store_id for store in stores),
        reference_restaurant_id=reference,
        policy_documents=policy_documents,
        policy_sections=policy_sections,
        faq_categories=faq_categories,
        faq_entries=faq_entries,
        rewards=_rewards(documents.rewards),
        catering_packages=packages,
        catering_package_options=options,
        stores=stores,
        store_profiles=profiles,
        store_hours=hours,
    )


def _policy_documents(
    documents: PolicyDocuments,
) -> tuple[tuple[PolicyDocument, ...], tuple[PolicySection, ...]]:
    """Split each policy page into a document row and its sections."""
    published = (
        ("rewards-terms", "TERMS", documents.rewards_terms),
        ("rewards", "OVERVIEW", documents.rewards),
    )
    rows: list[PolicyDocument] = []
    sections: list[PolicySection] = []
    for document_id, kind, document in published:
        parsed = parse_document(document.text, document.source_url)
        rows.append(
            PolicyDocument(
                document_id=document_id,
                kind=kind,
                title=parsed.title,
                section_count=len(parsed.sections),
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
        )
        sections.extend(
            PolicySection(
                document_id=document_id,
                position=position,
                heading=section.heading,
                text=section.text,
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
            for position, section in enumerate(parsed.sections)
        )
    rows.sort(key=lambda row: row.document_id)
    sections.sort(key=lambda row: (row.document_id, row.position))
    return tuple(rows), tuple(sections)


class _TileCollector(HTMLParser):
    """Collects the published Rewards Exchange tiles.

    Each is a two-sided tile: the front carries the price in points, the back
    carries the name. Both halves are needed, so a tile missing either is
    dropped rather than becoming a row with a hole in it, and a page with no
    tiles at all is an error.

    **This follows the published class markers rather than element depth,**
    which is unusual enough to say why: the rewards page ships an unbalanced
    ``</span>`` inside every tile's price. Counting elements through that goes
    wrong by one and never recovers, and the markers — ``__front``,
    ``__points``, ``__image``, ``__flipped``, ``__title``, ``__icon`` — say
    where each part of a tile begins and therefore where the one before it
    ended. A reader that only works on well-formed markup is a reader that
    silently returns nothing on this page.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tiles: list[tuple[str, str, str | None]] = []
        self._open = False
        self._capture: str | None = None
        self._parts: list[str] = []
        self._points: str | None = None
        self._title: str | None = None
        self._image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = frozenset((attributes.get("class") or "").split())
        if classes & TILE_MARKERS:
            self._close_capture()
        if TILE_CLASS in classes:
            self._close_tile()
            self._open = True
        if not self._open:
            return
        if tag == "br" and self._capture is not None:
            self._parts.append(" ")
        if POINTS_CLASS in classes:
            self._capture, self._parts = "points", []
        elif TITLE_CLASS in classes:
            self._capture, self._parts = "title", []
        if tag == "img" and self._image is None:
            self._image = attributes.get("src")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._parts.append(data)

    def finish(self) -> None:
        """Emit the tile still being read, if there is one."""
        self._close_capture()
        self._close_tile()

    def _close_capture(self) -> None:
        """Record whichever half of a tile was being read."""
        if self._capture is None:
            return
        text = " ".join("".join(self._parts).split())
        if self._capture == "points":
            self._points = text
        else:
            self._title = text
        self._capture = None
        self._parts = []

    def _close_tile(self) -> None:
        """Emit a completed tile and start afresh."""
        if self._open and self._points and self._title:
            self.tiles.append((self._points, self._title, self._image))
        self._open = False
        self._points = self._title = self._image = None


def _rewards(document: CachedDocument) -> tuple[Reward, ...]:
    """Read the published rewards and their point costs off the rewards page."""
    collector = _TileCollector()
    collector.feed(document.text)
    collector.finish()
    if not collector.tiles:
        raise ChipotleSourceError(
            f"{document.source_url} publishes no reward tiles; the point costs "
            f"issue #21 asks for are not there to read, and the signed-in "
            f"Rewards Exchange is not public"
        )
    rewards: list[Reward] = []
    for position, (points, name, image) in enumerate(collector.tiles):
        digits = "".join(character for character in points if character.isdigit())
        if not digits:
            raise ChipotleSourceError(
                f"{document.source_url} publishes a reward priced {points!r}, "
                f"which is not a number of points"
            )
        rewards.append(
            Reward(
                position=position,
                name=name,
                point_cost=int(digits),
                image_path=image,
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
        )
    return tuple(rewards)


def _faq(
    document: CachedDocument,
) -> tuple[tuple[FaqCategory, ...], tuple[FaqEntry, ...]]:
    """Read the published FAQ, keeping its two-level structure."""
    payload = _decode(document)
    items = _path(payload, ("data", "faqsList", "items"), document.source_url)
    if not isinstance(items, list) or not items:
        raise ChipotleSourceError(
            f"{document.source_url} published no FAQ items; the ordering and "
            f"refund answers issue #21 asks for are not there to read"
        )
    categories: list[FaqCategory] = []
    entries: list[FaqEntry] = []
    for item in items:
        for index, category in enumerate(_sequence(item, "categories")):
            name = _text(category.get("category")) or ""
            position = _integer(category.get("position"), index) or index
            for order, subcategory in enumerate(_sequence(category, "subcategory")):
                title = _text(subcategory.get("title"))
                questions = _sequence(subcategory, "questions")
                categories.append(
                    FaqCategory(
                        category=name,
                        category_position=position,
                        subcategory=title,
                        subcategory_position=order,
                        entry_count=len(questions),
                        source_url=document.source_url,
                        harvested_at=document.harvested_at,
                    )
                )
                entries.extend(
                    _faq_entry(name, title, rank, question, document)
                    for rank, question in enumerate(questions)
                )
    categories.sort(key=lambda row: (row.category_position, row.subcategory_position))
    entries.sort(key=lambda row: (row.category, row.subcategory or "", row.rank))
    return tuple(categories), tuple(entries)


def _faq_entry(
    category: str,
    subcategory: str | None,
    order: int,
    question: Mapping[str, Any],
    document: CachedDocument,
) -> FaqEntry:
    """Read one published question and its answer."""
    answer = question.get("answer")
    nodes = answer.get("json") if isinstance(answer, Mapping) else None
    text, links = _rich_text(nodes, document.source_url)
    return FaqEntry(
        category=category,
        subcategory=subcategory,
        rank=_integer(question.get("rank"), order) or order,
        question=_text(question.get("question")) or "",
        answer=text,
        links=links,
        is_top_question=bool(question.get("topQuestion")),
        source_url=document.source_url,
        harvested_at=document.harvested_at,
    )


def _rich_text(nodes: Any, source_url: str) -> tuple[str, tuple[str, ...]]:
    """Render an answer's node tree as text, collecting the URLs it links to.

    Args:
        nodes: The published node list.
        source_url: Where it came from. Used in the error message.

    Returns:
        The text, with block boundaries as newlines, and the hrefs in order.

    Raises:
        ChipotleSourceError: On a node type this does not know. An answer that
            silently lost a list because the FAQ started using a new node is
            an answer that is quietly wrong.
    """
    parts: list[str] = []
    links: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        if not isinstance(node, Mapping):
            raise ChipotleSourceError(
                f"{source_url} publishes an answer node that is not an object: {node!r}"
            )
        kind = node.get("nodeType")
        if kind == "text":
            parts.append(str(node.get("value") or ""))
            return
        if kind == "line-break":
            parts.append("\n")
            walk(node.get("content") or [])
            return
        if kind == "link":
            data = node.get("data")
            href = _text(data.get("href")) if isinstance(data, Mapping) else None
            if href is not None:
                links.append(href)
            walk(node.get("content") or [])
            return
        if kind in _BLOCK_NODES or kind in _LIST_NODES or kind in _INLINE_NODES:
            if kind in _BLOCK_NODES:
                parts.append("\n")
            walk(node.get("content") or [])
            if kind in _BLOCK_NODES:
                parts.append("\n")
            return
        raise ChipotleSourceError(
            f"{source_url} publishes an answer containing a {kind!r} node, "
            f"which this source does not know how to read"
        )

    walk(nodes or [])
    lines = [" ".join(line.split()) for line in "".join(parts).split("\n")]
    return "\n".join(line for line in lines if line), tuple(links)


def _catering(
    document: CachedDocument,
) -> tuple[tuple[CateringPackage, ...], tuple[CateringOption, ...]]:
    """Read the published catering packages and what goes in them."""
    payload = _decode(document)
    if not isinstance(payload, Mapping):
        raise ChipotleSourceError(f"{document.source_url} is not a catering menu")
    menu = payload.get("menu")
    if not isinstance(menu, list) or not menu:
        raise ChipotleSourceError(
            f"{document.source_url} publishes no catering packages; whether "
            f"Chipotle caters is a question this dataset has to answer"
        )
    packages: list[CateringPackage] = []
    options: list[CateringOption] = []
    for entry in menu:
        if not isinstance(entry, Mapping):
            raise ChipotleSourceError(
                f"{document.source_url} publishes a catering package that is "
                f"not an object: {entry!r}"
            )
        package_id = _text(entry.get("id"))
        if package_id is None:
            raise ChipotleSourceError(
                f"{document.source_url} publishes a catering package with no identifier"
            )
        config = entry.get("config")
        config = config if isinstance(config, Mapping) else {}
        item_config = entry.get("itemConfig")
        item_config = item_config if isinstance(item_config, Mapping) else {}
        packages.append(
            CateringPackage(
                package_id=package_id,
                name=_text(entry.get("name")) or package_id,
                display_name=_text(entry.get("displayName")),
                display_sub_name=_text(entry.get("displaySubName")),
                description=_text(entry.get("description")),
                tagline=_text(entry.get("tagline")),
                unit=_text(entry.get("unit")),
                display_unit=_text(entry.get("displayUnit")),
                min_price=_money(entry.get("minPrice"), package_id, document.source_url),
                max_price=_money(entry.get("maxPrice"), package_id, document.source_url),
                min_quantity=_integer(item_config.get("min"), None),
                max_quantity=_integer(item_config.get("max"), None),
                quantity_increment=_integer(item_config.get("increment"), None),
                serves=_text(item_config.get("serves")),
                base_count=_integer(config.get(_SLOT_COUNTS["bases"]), None),
                protein_count=_integer(config.get(_SLOT_COUNTS["proteins"]), None),
                topping_count=_integer(config.get(_SLOT_COUNTS["toppings"]), None),
                premium_topping_count=_integer(
                    config.get(_SLOT_COUNTS["premiumToppings"]), None
                ),
                salsa_count=_integer(config.get(_SLOT_COUNTS["salsas"]), None),
                tortilla_count=_integer(config.get(_SLOT_COUNTS["tortillas"]), None),
                sort_order=_integer(entry.get("sortOrder"), None),
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
        )
        for slot in CATERING_SLOTS:
            options.extend(
                _catering_options(package_id, slot, entry.get(slot), False, document)
            )
        included = config.get("readonly")
        for slot, listed in sorted((included or {}).items()):
            options.extend(_catering_options(package_id, slot, listed, True, document))
    packages.sort(key=lambda row: row.package_id)
    options.sort(
        key=lambda row: (row.package_id, row.is_included, row.slot, row.position)
    )
    return tuple(packages), tuple(options)


def _catering_options(
    package_id: str,
    slot: str,
    listed: Any,
    is_included: bool,
    document: CachedDocument,
) -> Iterable[CateringOption]:
    """Read one of a package's component lists."""
    if not isinstance(listed, list):
        return ()
    rows: list[CateringOption] = []
    for position, option in enumerate(listed):
        if not isinstance(option, Mapping):
            raise ChipotleSourceError(
                f"{document.source_url}: {package_id} publishes a {slot} entry "
                f"that is not an object: {option!r}"
            )
        item_id = _text(option.get("id"))
        if item_id is None:
            raise ChipotleSourceError(
                f"{document.source_url}: {package_id} publishes a {slot} entry "
                f"with no identifier"
            )
        rows.append(
            CateringOption(
                package_id=package_id,
                slot=slot,
                position=position,
                item_id=item_id,
                name=_text(option.get("name")) or item_id,
                pos_id=_text(option.get("posId")),
                is_included=is_included,
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
        )
    return rows


def _stores(
    documents: Sequence[CachedDocument],
) -> tuple[tuple[Store, ...], tuple[StoreHours, ...]]:
    """Read every harvested locator page into a store and seven days of hours."""
    stores: list[Store] = []
    hours: list[StoreHours] = []
    for document in documents:
        page = parse_store_page(document.text, document.source_url)
        stores.append(
            Store(
                store_id=page.store_id,
                street_address=page.street_address,
                city=page.city,
                region=page.region,
                postal_code=page.postal_code,
                country=page.country,
                latitude=page.latitude,
                longitude=page.longitude,
                telephone=page.telephone,
                page_url=page.page_url,
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
        )
        published = {entry.day: entry for entry in page.hours}
        hours.extend(
            StoreHours(
                store_id=page.store_id,
                day_of_week=day,
                opens=published[day].opens if day in published else None,
                closes=published[day].closes if day in published else None,
                is_published=day in published,
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
            for day in DAYS
        )
    stores.sort(key=lambda row: row.store_id)
    hours.sort(key=lambda row: (row.store_id, DAYS.index(row.day_of_week)))
    return tuple(stores), tuple(hours)


def _profiles(documents: Sequence[CachedDocument]) -> tuple[StoreProfile, ...]:
    """Read the restaurant API's answer for every harvested store."""
    profiles: list[StoreProfile] = []
    for document in documents:
        payload = _decode(document)
        if not isinstance(payload, Mapping):
            raise ChipotleSourceError(
                f"{document.source_url} is not a restaurant profile"
            )
        store_id = _integer(payload.get("restaurantNumber"), None)
        if store_id is None:
            raise ChipotleSourceError(
                f"{document.source_url} publishes no restaurant number"
            )
        profiles.append(
            StoreProfile(
                store_id=store_id,
                name=_text(payload.get("restaurantName")),
                location_type=_text(payload.get("restaurantLocationType")),
                status=_text(payload.get("restaurantStatus")),
                real_estate_category=_text(payload.get("realEstateCategory")),
                operational_region=_text(payload.get("operationalRegion")),
                operational_sub_region=_text(payload.get("operationalSubRegion")),
                market_area=_text(payload.get("designatedMarketAreaName")),
                opened_on=_text(payload.get("openDate")),
                source_url=document.source_url,
                harvested_at=document.harvested_at,
            )
        )
    profiles.sort(key=lambda row: row.store_id)
    return tuple(profiles)


def _decode(document: CachedDocument) -> Any:
    """Parse a document as JSON, keeping every number's own text.

    ``parse_float=Decimal`` is the whole reason this is not
    :func:`json.loads` at the call site: a catering price must not become a
    binary float on the way in and back out again.
    """
    try:
        return json.loads(document.content, parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise ChipotleSourceError(
            f"{document.source_url} is not valid JSON: {error}"
        ) from error


def _path(payload: Any, keys: tuple[str, ...], source_url: str) -> Any:
    """Follow a chain of keys, saying which one was missing if one is."""
    current = payload
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            raise ChipotleSourceError(
                f"{source_url} publishes no {'.'.join(keys)}; the document's "
                f"shape has changed"
            )
        current = current[key]
    return current


def _sequence(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    """Return a list of objects under ``key``, or an empty list."""
    listed = payload.get(key)
    if not isinstance(listed, list):
        return []
    return [entry for entry in listed if isinstance(entry, Mapping)]


def _text(value: Any) -> str | None:
    """Return a published string with its edges trimmed, or ``None``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _integer(value: Any, default: int | None) -> int | None:
    """Return a published number as an integer, or ``default``."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal | float):
        return int(value)
    return default


def _money(value: Any, package_id: str, source_url: str) -> Decimal | None:
    """Return a published price as an exact decimal, or ``None`` if absent."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    raise ChipotleSourceError(
        f"{source_url}: {package_id} has a price that is not a number: {value!r}"
    )
