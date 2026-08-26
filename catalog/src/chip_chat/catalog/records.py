"""The shape of the catalogue: eight tables and what each column promises.

This is RFC-001 section 04's data model, as amended by
``docs/decisions/menu-pricing.md`` — money is a column on a restaurant, not on
an item — and by ``docs/decisions/catalog-shape.md``, which is where the
additions below are argued rather than merely listed.

Three conventions run through all of it, and each exists because of a specific
way this table could otherwise be wrong.

**A merged row carries every provenance it merged.** An item's identity comes
from the online menu; its calories and its allergen marks come from the
nutrition metadata. One ``source_url`` cannot honestly cover both, so a row
built from two documents carries two, and a row built from a document that
said nothing about it carries a null second one. RFC-001 section 08 requires a
quoted figure to cite where it came from, and a citation reconstructed later
is a citation invented later.

**An allergen that is not listed is not an allergen that is absent.** The
three published states — :class:`~chip_chat.harvest.AllergenStatus`'s
``CONTAINS``, ``NOT_LISTED`` and ``NOT_PUBLISHED`` — survive into
:class:`ItemAllergen` unchanged, and are reconstructible from
:class:`MenuItem` alone through :attr:`MenuItem.allergen_disclosure`. Nothing
in this package converts them into a boolean, and nothing should downstream:
a boolean has room for ``CONTAINS`` and exactly one other thing, so the two
kinds of silence merge and both read as "does not contain".

**A published figure is published for something in particular.** ``CMG-2`` is
"Steak Burrito" on the menu and 150 calories of *steak* in the nutrition
metadata — the tortilla, the rice, the beans and the toppings are separate
items the calculator adds. :attr:`MenuItem.is_composed` says which kind of
figure a row's ``calories`` is, because reading a component's figure as a
total is the easiest confidently-wrong number in this dataset.
"""

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.sources.chipotle import AllergenStatus
from chip_chat.harvest.sources.chipotle.tables import describe, to_jsonl, write_tables

DEFAULT_PREFIX = "catalog/chipotle"
"""Where the catalogue lands. Beside ``parsed/``, never inside it: the parsed
tables are one harvest of one site, and this is the consolidation three other
subsystems resolve against."""

TABLES = (
    "menu_items",
    "item_prices",
    "modifiers",
    "stores",
    "item_allergens",
    "allergens",
    "caveats",
    "vocabulary",
)
"""Table names, in the order the manifest lists them."""

PROVENANCE_SUFFIXES = ("source_url", "harvested_at")
"""Column name endings that mark a field as provenance rather than content.

:meth:`MenuCatalog.content_version` strips these, which is what lets a caller
ask whether two harvests describe the same orderable menu — as distinct from
whether they are the same harvest.
"""


class AllergenDisclosure(StrEnum):
    """Whether Chipotle publishes allergen data about an item at all.

    This is the item-level half of the three-valued answer, and it is what
    keeps :attr:`MenuItem.allergens` from lying by omission. A code absent
    from that tuple means one of two different things, and this column says
    which:

    Attributes:
        PUBLISHED: Chipotle publishes allergen marks for this item. A code
            absent from :attr:`MenuItem.allergens` is ``NOT_LISTED`` — which
            is *not* "does not contain", because the published caveat says
            foods contact one another during preparation and the chart does
            not reflect it.
        NOT_PUBLISHED: Chipotle publishes nothing about this item. Every code
            is ``NOT_PUBLISHED`` and nothing whatever is known; the honest
            answer to a question about it is to say so.
    """

    PUBLISHED = "PUBLISHED"
    NOT_PUBLISHED = "NOT_PUBLISHED"


class Slot(StrEnum):
    """The slots RFC-001 section 07's stage-4 schema describes a meal in.

    A vision model returns a value per slot; the deterministic matcher
    resolves each to catalogue rows. The names are the RFC's, spelled as the
    JSON schema spells them, so that the generated enum module and the schema
    fragment cannot drift apart.
    """

    VESSEL = "vessel"
    PROTEIN = "protein"
    RICE = "rice"
    BEANS = "beans"
    SALSAS = "salsas"
    TOPPINGS = "toppings"


class Derivation(StrEnum):
    """How a vocabulary term came to be in the slot it is in.

    Four of these are published structure and one is not, and the difference
    is a column rather than a footnote — because "the model's vocabulary is
    generated from the catalogue" is only worth saying if a reader can see
    which parts of it Chipotle published and which part this package inferred.

    Attributes:
        ITEM_TYPE: The published ``itemType`` of an orderable entree —
            ``Burrito``, ``Bowl``, ``Tacos``. This is the vessel vocabulary.
        PRIMARY_FILLING: The published ``primaryFillingName`` of an entree —
            ``Chicken``, ``Steak``, ``Sofritas``. This is the protein
            vocabulary, and it is a property of the entree rather than a
            modifier of it.
        MODIFIER_TYPE: The published ``itemType`` of a modifier — ``Rice``,
            ``Beans``, ``Salsa``, ``Toppings``. Four of RFC-001 section 07's
            six slots are this column read directly.
        NAME_SUFFIX: Not published structure, and the only inference in this
            package. Chipotle publishes a ``Salsa`` modifier type, but only
            for its build-your-own items; on an ordinary entree the same four
            salsas are published as ``Toppings`` and marked apart nowhere
            except in their names, all of which end in "Salsa". RFC-001
            section 07 gives salsas their own slot, so the split is made here,
            from the published name, and labelled as the inference it is.
    """

    ITEM_TYPE = "ITEM_TYPE"
    PRIMARY_FILLING = "PRIMARY_FILLING"
    MODIFIER_TYPE = "MODIFIER_TYPE"
    NAME_SUFFIX = "NAME_SUFFIX"


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One thing the catalogue knows about, orderable alone or only inside another.

    Attributes:
        item_id: Chipotle's own identifier, e.g. ``CMG-2``. It is published
            rather than derived, which is what makes it survive a re-harvest
            and what keeps an order generated against one harvest from being
            orphaned by the next.
        name: The name as published, e.g. ``Steak Burrito``.
        category: ``Entree``, ``Side``, ``Drink`` or ``Non Food Items`` for
            something orderable on its own, and ``None`` for something that
            only ever appears inside another item — the black beans in a
            burrito. ``category is not None`` is the test for whether a thing
            can be ordered by itself.
        item_type: The finer published type, e.g. ``Burrito``, ``Jarritos``.
            The vessel vocabulary is generated from this column.
        primary_filling: The protein an entree is built around, where the menu
            names one. The protein vocabulary is generated from this column,
            and ``(item_type, primary_filling)`` is how a described meal
            resolves to an entree SKU.
        description: What Chipotle publishes about the item, where it
            publishes anything. Most entrees have none.
        calories: The published total-calorie figure for *this item*, or
            ``None`` where nobody published one. ``None`` is not zero and must
            never be defaulted to zero on the way to a total.
        is_composed: Whether this item is built from modifiers, in which case
            :attr:`calories` is the figure for its own component only and a
            total has to add the chosen modifiers' figures. A Steak Burrito's
            published 150 calories are the steak.
        allergens: The allergen codes published as ``CONTAINS`` for this item,
            sorted. A code absent from this tuple is ``NOT_LISTED`` when
            :attr:`allergen_disclosure` is ``PUBLISHED`` and ``NOT_PUBLISHED``
            otherwise — read :class:`AllergenDisclosure` before rendering any
            of it as a sentence, and read :attr:`MenuCatalog.caveats` before
            answering an allergen question from it at all.
        allergen_disclosure: Whether anything is published about this item's
            allergens. See :class:`AllergenDisclosure`.
        source_url: The endpoint the item's identity was read from.
        harvested_at: When that endpoint was fetched.
        nutrition_source_url: The endpoint :attr:`calories` was read from, or
            ``None`` where the nutrition data had never heard of this item.
        nutrition_harvested_at: When *that* endpoint was fetched.
        allergen_source_url: The document the allergen marks were read from.
            It is usually the same as :attr:`nutrition_source_url` and is not
            always: a handful of foods are named by the published chart and
            not by the nutrition metadata, and a row that cited the wrong one
            of the two would be a citation that does not support it.
        allergen_harvested_at: When *that* document was fetched.
    """

    item_id: str
    name: str
    category: str | None
    item_type: str
    primary_filling: str | None
    description: str | None
    calories: Decimal | None
    is_composed: bool
    allergens: tuple[str, ...]
    allergen_disclosure: AllergenDisclosure
    source_url: str
    harvested_at: datetime
    nutrition_source_url: str | None
    nutrition_harvested_at: datetime | None
    allergen_source_url: str | None
    allergen_harvested_at: datetime | None

    def allergen_status(self, allergen_code: str) -> AllergenStatus:
        """Return the three-valued answer for one allergen on this item.

        The point of this method is that there is nowhere in it for a boolean
        to appear. Reconstructing the status from :attr:`allergens` and
        :attr:`allergen_disclosure` gives back exactly what the harvest's
        ``item_allergens`` said, which
        ``test_catalog_allergens.py`` asserts for every item crossed with
        every published allergen.

        Args:
            allergen_code: A code from :attr:`MenuCatalog.allergens`.

        Returns:
            ``CONTAINS`` if Chipotle marks this item with it, ``NOT_LISTED``
            if it publishes marks for this item and this is not among them,
            and ``NOT_PUBLISHED`` if it publishes nothing about the item.
            ``NOT_LISTED`` does not mean the item is free of the allergen.
        """
        if allergen_code in self.allergens:
            return AllergenStatus.CONTAINS
        if self.allergen_disclosure is AllergenDisclosure.NOT_PUBLISHED:
            return AllergenStatus.NOT_PUBLISHED
        return AllergenStatus.NOT_LISTED


@dataclass(frozen=True, slots=True)
class ItemPrice:
    """What one restaurant charged for one item, and whether it had it.

    Carried through from the harvest unchanged. Prices are per restaurant
    because Chipotle's really are — an eighteen percent spread on a Steak
    Burrito between two stores on one afternoon — so there is no
    ``base_price`` column here and a quoted price always has a restaurant and
    a ``harvested_at`` attached to it. See ``docs/decisions/menu-pricing.md``.

    Attributes:
        restaurant_id: Chipotle's numeric restaurant identifier.
        item_id: The item priced. Modifiers are priced here too, under their
            own item identifier.
        unit_price: The in-store price.
        unit_delivery_price: The delivery price, which is a different
            published number and not a markup on the other one.
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
class Modifier:
    """One thing that may go in one slot on one item.

    Attributes:
        modifier_id: ``<item_id>:<modifier_item_id>``. The same ingredient is
            a different modifier on a different item — a different slot, a
            different allowance — so identity is the pair. Both halves are
            published identifiers, so this survives a re-harvest.
        item_id: The item being modified.
        modifier_item_id: The identifier of the thing added, which is a
            :class:`MenuItem` in its own right. This is the column that joins
            a modifier to its price and to its nutrition.
        name: The name as published, e.g. ``Black Beans``.
        slot: Which of RFC-001 section 07's slots this modifier belongs to, or
            ``None`` for one that is not part of the described-meal vocabulary
            — extra and half portions, mostly.
        derivation: How :attr:`slot` was arrived at. See :class:`Derivation`.
        group_name: The published content group, e.g. ``RiceContentGroup``,
            or ``None`` for a modifier the menu leaves ungrouped.
        modifier_type: The published type, e.g. ``Beans``, ``Toppings``.
        min_quantity: Fewest choices the group accepts, where the menu
            declares bounds for it.
        max_quantity: Most it accepts.
        is_default: Whether it is included unless removed.
        delta_calories: The published total-calorie figure for the thing
            added, or ``None`` where nobody published one. It is called a
            delta because that is what adding it does to a total; it is the
            component's own published figure and not a difference between two
            figures. ``None`` is not zero.
        portion_options: The portion words this modifier accepts on this item,
            in published order — ``Light``, ``Extra``, ``Side``, ``Half``.
            "Extra cheese" resolves to a term here or is refused.
        source_url: The endpoint the modifier was read from.
        harvested_at: When that endpoint was fetched.
        nutrition_source_url: The endpoint :attr:`delta_calories` came from,
            or ``None`` where that document was silent.
        nutrition_harvested_at: When *that* endpoint was fetched.
    """

    modifier_id: str
    item_id: str
    modifier_item_id: str
    name: str
    slot: Slot | None
    derivation: Derivation | None
    group_name: str | None
    modifier_type: str
    min_quantity: int | None
    max_quantity: int | None
    is_default: bool
    delta_calories: Decimal | None
    portion_options: tuple[str, ...]
    source_url: str
    harvested_at: datetime
    nutrition_source_url: str | None
    nutrition_harvested_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoreHours:
    """When one store is open on one day, or the fact that nobody said.

    Nested inside :class:`Store` rather than kept as a side table, because
    seven rows that belong to one store are the store's hours and splitting
    them out would be a schema invented to suit a serialiser.

    Attributes:
        day_of_week: The day, e.g. ``Monday``.
        opens: Published opening time, ``HH:MM`` as published.
        closes: Published closing time.
        is_published: Whether the page published times for this day at all.
            ``False`` with null times means "nothing was published", which is
            a different answer from "closed on Sunday" and must not be
            rendered as one.
    """

    day_of_week: str
    opens: str | None
    closes: str | None
    is_published: bool


@dataclass(frozen=True, slots=True)
class Store:
    """One restaurant: where it is, what it is called, when it is open.

    Two documents, and therefore two provenances. Every locator page in the
    country calls its restaurant "Chipotle Mexican Grill"; the name that makes
    "the Ballard store" mean something is published only by the restaurant
    endpoint.

    Attributes:
        store_id: Chipotle's restaurant number — the same identifier
            :attr:`ItemPrice.restaurant_id` uses, which is what makes a price
            and a place joinable.
        name: The name the company uses for the restaurant, or ``None`` where
            the restaurant endpoint published none. Not backfilled from the
            locator page, which would make every store in the country share
            one name.
        street_address: The street line, as published.
        city: The city, as published. The locator appends the county for some
            stores — "Lakewood, Los Angeles" — and it is kept as published
            rather than trimmed to a guess about which half is which.
        region: The state or territory code, e.g. ``CA``.
        postal_code: The postcode.
        hours: Seven entries, one per day, whether or not seven were
            published. See :class:`StoreHours`.
        page_url: The locator page a visitor would be shown.
        source_url: The page the address was read from.
        harvested_at: When that page was fetched.
        profile_source_url: The endpoint :attr:`name` was read from, or
            ``None`` where there was no profile for this store.
        profile_harvested_at: When *that* endpoint was fetched.
    """

    store_id: int
    name: str | None
    street_address: str | None
    city: str | None
    region: str | None
    postal_code: str | None
    hours: tuple[StoreHours, ...]
    page_url: str
    source_url: str
    harvested_at: datetime
    profile_source_url: str | None
    profile_harvested_at: datetime | None


@dataclass(frozen=True, slots=True)
class ItemAllergen:
    """The three-valued allergen answer, for one item and one allergen.

    Carried through from the harvest with the status untouched. There is a row
    for every catalogue item crossed with every published allergen code, so
    "nothing is published about this" is a row that says so rather than a row
    that is not there — a join that misses is a silence, and a silence about
    an allergen reads as reassurance.

    Attributes:
        item_id: The item.
        allergen_code: The allergen, keyed into :class:`Allergen`.
        status: :class:`~chip_chat.harvest.AllergenStatus`. Read its docstring
            before rendering any of the three as a sentence.
        source_url: The document the status was read from — including for
            ``NOT_PUBLISHED``, where it names the document that was consulted
            and found silent.
        harvested_at: When that document was fetched.
    """

    item_id: str
    allergen_code: str
    status: AllergenStatus
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class Allergen:
    """One allergen code Chipotle publishes, and what it calls it.

    Attributes:
        allergen_code: The published code, e.g. ``glut``.
        name: The published label, or ``None`` where Chipotle publishes a code
            and no label for it. A null name is carried rather than guessed;
            the nutrition harvest found two codes in that state and inventing
            a label for one of them would put words in the source's mouth.
        badge_text: The published badge text, where there is one.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    allergen_code: str
    name: str | None
    badge_text: str | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class Caveat:
    """One paragraph of Chipotle's published prose about the chart's limits.

    Carried into the catalogue rather than left behind in the harvest, because
    a table that answers allergen questions without its caveats makes a
    stronger claim than the source does. Chipotle states that cross-contact is
    not reflected on the chart, and that it cannot guarantee the absence of
    eggs, mustard, peanuts, tree nuts, sesame, shellfish or fish even though
    those are not used as ingredients.

    Attributes:
        position: Where the paragraph falls in the published prose, from zero.
        heading: The heading it sits under, where it sits under one.
        text: The paragraph, verbatim.
        source_url: The page it was read from.
        harvested_at: When that page was fetched.
    """

    position: int
    heading: str | None
    text: str
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class VocabularyTerm:
    """One value the vision model may return in one slot.

    This table is the whole of RFC-001 section 07's promise that "every enum
    is generated from the live catalogue at build time, so the model's
    vocabulary cannot drift from what is orderable". The generated enum module
    is rendered from these rows and from nothing else.

    Attributes:
        slot: Which slot the term belongs to. See :class:`Slot`.
        value: The enum member's value — the published name, lowercased and
            slugified. This is what the model returns.
        name: The published name the value was derived from.
        item_ids: Every catalogue item this term may resolve to, sorted, and
            empty for a term that is a property of an entree rather than a row
            of its own.

            It is a tuple and not one identifier because Chipotle really does
            publish one ingredient under several identifiers: guacamole is
            ``CMG-1001`` on some entrees and ``CMG-5301`` on others, and white
            rice, black beans and the honey vinaigrette are the same. Which
            one a described meal means depends on which entree it is on, so
            the matcher resolves ``(entree, term)`` against
            :class:`Modifier` — this column is the candidate set, not the
            answer.

            ``vessel`` and ``protein`` are empty for a different reason: each
            is half of an entree, and a described bowl of chicken resolves
            through ``(item_type, primary_filling)`` rather than through
            either half alone.
        derivation: How the term got its slot. See :class:`Derivation`.
        source_url: The endpoint the published name was read from.
        harvested_at: When that endpoint was fetched.
    """

    slot: Slot
    value: str
    name: str
    item_ids: tuple[str, ...]
    derivation: Derivation
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class MenuCatalog:
    """Everything issue #24 consolidates: what is orderable, and where from.

    Attributes:
        reference_restaurant_id: The restaurant whose prices a demo quotes
            when it quotes one, and whose menu defined the catalogue's
            structure.
        restaurant_ids: Every restaurant priced, in the order harvested.
        menu_items: Item identity, structure, calories and allergen marks.
        item_prices: Money and availability, per restaurant.
        modifiers: What may go in which slot on which item.
        stores: Where the restaurants are and when they are open.
        item_allergens: The three-valued allergen answer, per item per
            allergen.
        allergens: The published allergen vocabulary.
        caveats: What the published allergen data does not cover.
        vocabulary: The vision model's constrained vocabulary, per slot.
    """

    reference_restaurant_id: int
    restaurant_ids: tuple[int, ...]
    menu_items: tuple[MenuItem, ...]
    item_prices: tuple[ItemPrice, ...]
    modifiers: tuple[Modifier, ...]
    stores: tuple[Store, ...]
    item_allergens: tuple[ItemAllergen, ...]
    allergens: tuple[Allergen, ...]
    caveats: tuple[Caveat, ...]
    vocabulary: tuple[VocabularyTerm, ...]

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

    def item(self, item_id: str) -> MenuItem | None:
        """Return one item by identifier, or ``None`` if the catalogue has none.

        Args:
            item_id: A published item identifier, e.g. ``CMG-2``.

        Returns:
            The item, or ``None``. Callers resolving a model's output must
            treat ``None`` as a refusal rather than as a reason to invent a
            row: nothing may be quoted that is not in this table.
        """
        for row in self.menu_items:
            if row.item_id == item_id:
                return row
        return None

    def version(self) -> str:
        """Return the digest that identifies this catalogue exactly.

        Provenance included, so two harvests of an unchanged menu have
        different versions — they are different harvests, and
        :attr:`MenuItem.harvested_at` is one of the things a citation quotes.
        Use :meth:`content_version` to ask the other question.

        Returns:
            A SHA-256 hex digest over every table's serialised bytes.
        """
        return _digest(self.tables())

    def content_version(self) -> str:
        """Return the digest of *what is orderable*, provenance stripped.

        Every ``source_url`` and ``harvested_at`` column is removed before the
        digest is taken, so this changes when the menu changes and not when it
        is merely re-read. That is the version issue #25's generator records
        against a batch of synthetic orders: two harvests sharing a
        ``content_version`` compose the same orders, whatever their
        ``harvested_at`` says.

        Returns:
            A SHA-256 hex digest over every table's serialised bytes, with the
            provenance columns of :data:`PROVENANCE_SUFFIXES` dropped.
        """
        running = hashlib.sha256()
        for name, rows in self.tables():
            running.update(name.encode("utf-8"))
            running.update(b"\n")
            for row in _json_rows(rows):
                running.update(_canonical(_without_provenance(row)))
                running.update(b"\n")
        return running.hexdigest()

    def manifest(self) -> dict[str, Any]:
        """Return a description of this catalogue, digests and versions included.

        Comparing two manifests is how issue #24's stability criterion is
        checked rather than asserted, and the ``content_version`` in it is
        what a downstream dataset records to say which catalogue it was built
        against.

        Returns:
            A JSON-ready mapping.
        """
        return {
            "catalog_version": self.version(),
            "content_version": self.content_version(),
            "reference_restaurant_id": self.reference_restaurant_id,
            "restaurant_ids": list(self.restaurant_ids),
            "tables": describe(self.tables()),
        }

    def write(self, blobs: BlobStore, prefix: str = DEFAULT_PREFIX) -> Mapping[str, str]:
        """Write every table, and the manifest, to the blob store.

        Args:
            blobs: Where to write.
            prefix: Key prefix for the catalogue.

        Returns:
            Table name to the key it was written at, with the manifest under
            the key ``manifest``.
        """
        return write_tables(blobs, prefix, self.tables(), self.manifest())


def _digest(tables: Iterator[tuple[str, Sequence[Any]]]) -> str:
    """Return one SHA-256 over every table's name and serialised bytes.

    The name goes into the digest as well as the rows, so that moving a row
    from one table to another changes the version even in the unlikely case
    that the two tables' bytes swap cleanly.
    """
    running = hashlib.sha256()
    for name, rows in tables:
        running.update(name.encode("utf-8"))
        running.update(b"\n")
        running.update(to_jsonl(rows))
    return running.hexdigest()


def _json_rows(rows: Sequence[Any]) -> list[Any]:
    """Return rows as the plain objects :func:`to_jsonl` would have written.

    Going through the canonical serialiser rather than around it means the
    content digest and the written bytes cannot disagree about how a
    :class:`~decimal.Decimal` or a nested row is spelled.
    """
    payload = to_jsonl(rows).decode("utf-8")
    return [json.loads(line) for line in payload.splitlines()]


def _without_provenance(value: Any) -> Any:
    """Return one JSON-ready row with its provenance columns dropped.

    Nested rows go through the same treatment, so a store's hours are compared
    on their content too.
    """
    if isinstance(value, list):
        return [_without_provenance(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _without_provenance(item)
            for key, item in value.items()
            if not key.endswith(PROVENANCE_SUFFIXES)
        }
    return value


def _canonical(value: Any) -> bytes:
    """Serialise one row the way :func:`to_jsonl` serialises one row."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
