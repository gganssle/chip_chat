"""The parsed shape of Chipotle's published nutrition and allergen data.

Eight flat tables, keyed on the same ``item_id`` the menu tables in issue #19
use, so the catalogue in issue #24 joins rather than reconciles.

One convention runs through all of them, and it is the reason this dataset is
separate from the menu one.

**Absence is a value, not a gap.** "Does this contain dairy?" is a safety
question, and the honest answers to it are three, not two: *Chipotle says it
does*, *Chipotle publishes allergen data for this item and does not mark
dairy*, and *Chipotle publishes no allergen data for this item at all*. A
boolean column can hold the first and then has to pretend the other two are
the same thing, which turns a silence into a reassurance. So the answer is
:class:`AllergenStatus`, every orderable item gets a row for every published
allergen whether or not anything was said about it, and there is no encoding
in which a missing value can be mistaken for a negative one.

The same rule applies a column at a time in :class:`ItemNutrient`, where
``value`` is ``None`` when the item did not publish that nutrient and
``Decimal("0")`` when it published zero. Those are different facts. Trans fat
that is published as ``0`` is a measurement; trans fat that is absent is not.

**And even the negative is not a guarantee.** ``NOT_LISTED`` means the chart
does not mark the item, which Chipotle itself says is not the same as the item
being free of the allergen — see :class:`Caveat` and the prose it carries.
Nothing downstream should render ``NOT_LISTED`` as "dairy free". PRD K3.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.sources.chipotle.tables import describe, write_tables

DEFAULT_PARSED_PREFIX = "parsed/chipotle/nutrition"
"""Where the parsed tables land. Beside the menu tables, not inside them."""

TABLES = (
    "nutrients",
    "item_nutrition",
    "item_group_calories",
    "dietary_tags",
    "item_allergens",
    "item_diets",
    "allergen_chart",
    "caveats",
)
"""Table names, in the order the manifest lists them."""

TABLE_KEYS: dict[str, tuple[str, ...]] = {
    "nutrients": ("nutrient_key",),
    "item_nutrition": ("item_id", "nutrient_key", "portion_unit", "portion_value"),
    "item_group_calories": ("group_key",),
    "dietary_tags": ("tag_code",),
    "item_allergens": ("item_id", "allergen_code"),
    "item_diets": ("item_id", "diet_code"),
    "allergen_chart": ("menu_item_id", "name"),
    "caveats": ("position",),
}
"""What identifies a row, for the week-on-week diff of issue #38.

``item_allergens`` and ``item_diets`` are the two rows in this repository where
the diff is not merely interesting. A tag means CONTAINS and an absent tag is
not a published negative (cc-2bv), so a status flipping from ``CONTAINS`` to
``NOT_LISTED`` is a change in what the corpus will tell somebody about their
allergy — and it degrades silently, because nothing errors when it happens.
Keyed by ``(item_id, allergen_code)`` so that flip is reported as a
modification of one named row rather than as a churn of counts.

``item_nutrition`` carries the portion in its key because the same nutrient is
published for an item at more than one portion size, and a key of
``(item_id, nutrient_key)`` alone would collide and cost the table its keyed
diff. ``allergen_chart`` is keyed by ``(menu_item_id, name)`` rather than by
``sort_order``: the chart lists "Crispy Corn Tortilla" and "Tortilla Chips"
separately and reorders more readily than it renames.

See ``TABLE_KEYS`` in :mod:`chip_chat.harvest.sources.chipotle.records` for the
general rule, and :mod:`chip_chat.harvest.changes` for what happens when one of
these is wrong.
"""


class TagKind(StrEnum):
    """What Chipotle publishes a dietary tag code as.

    Read off the allergen and diet endpoint, which sorts every code it uses
    into an ``allergens`` list or a ``diets`` list. That is Chipotle's own
    classification; nothing here decides from a code's spelling or a group's
    display name what kind of thing it is.
    """

    ALLERGEN = "allergen"
    DIET = "diet"


class AllergenStatus(StrEnum):
    """What the published data says about one allergen in one item.

    Attributes:
        CONTAINS: Chipotle marks this item with this allergen. Its own wording
            for the control that applies these marks is "Tagged items contain
            your selection".
        NOT_LISTED: Chipotle publishes allergen data for this item and this
            allergen is not among the marks. **This is not "does not
            contain."** The published caveat says foods contact one another
            during preparation and that the chart does not reflect it.
        NOT_PUBLISHED: Chipotle publishes no allergen data for this item at
            all. Nothing whatever is known, and the honest answer to a
            question about it is to say so.
    """

    CONTAINS = "CONTAINS"
    NOT_LISTED = "NOT_LISTED"
    NOT_PUBLISHED = "NOT_PUBLISHED"


class DietStatus(StrEnum):
    """What the published data says about one diet and one item.

    Attributes:
        LISTED: Chipotle lists this item under this diet.
        NOT_LISTED: Chipotle publishes dietary tags for this item in this
            document and does not list this diet among them.
        NOT_PUBLISHED: This document says nothing about this item.
    """

    LISTED = "LISTED"
    NOT_LISTED = "NOT_LISTED"
    NOT_PUBLISHED = "NOT_PUBLISHED"


@dataclass(frozen=True, slots=True)
class Nutrient:
    """One nutrient Chipotle publishes, with its label and unit.

    The published keys are four characters long and opaque — ``tcal``,
    ``satu``, ``vitc`` — and the labels and units that make them readable are
    published beside them rather than assumed here. ``Sodium`` is in
    milligrams and ``Calcium`` is a percentage of a daily value; a dataset that
    guessed either would be publishing a number with the wrong unit on it.

    Attributes:
        nutrient_key: The published key, e.g. ``tcal``.
        name: The published label, e.g. ``Total Calories``.
        unit: The published unit, e.g. ``g``, ``mg``, ``cal``, ``%``, or
            ``None`` where the page publishes none.
        section_key: The key of the nutrient this one is a component of —
            ``satu`` is a component of ``tfat`` — or ``None`` when it is a
            section in its own right.
        section_name: The published name of the section it appears under.
        sort_order: Where it falls in the published order.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    nutrient_key: str
    name: str
    unit: str | None
    section_key: str | None
    section_name: str
    sort_order: int
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class ItemNutrient:
    """One published nutrient figure for one item, and the portion it is for.

    Long rather than wide on purpose. A row per nutrient means an item that
    publishes thirteen of the fourteen has one explicit null rather than a
    schema that has to grow a column, and it means the unit travels with the
    number instead of being looked up somewhere else at the point of use.

    **The figure is for this item alone, at the portion named here.** For an
    entree that is assembled from other items, Chipotle publishes the entree's
    own contribution and leaves the tortilla, the rice and the toppings to be
    added as the separate items they are. ``CMG-2`` is 150 calories of steak,
    not a Steak Burrito. Reading a component figure as a total is the single
    easiest way to publish a confidently wrong calorie count, so it is written
    here rather than left to be discovered.

    Attributes:
        item_id: The item, in the same namespace the menu tables use. A
            modifier is a menu item with its own identifier, so a modifier's
            nutrition is a row here too.
        nutrient_key: Which nutrient, keyed into :class:`Nutrient`.
        value: The published figure, or ``None`` where this item published no
            figure for this nutrient. ``Decimal("0")`` and ``None`` are
            different facts and must stay different all the way to the answer.
        unit: The unit that figure is in, as published.
        portion_unit: The unit of the portion the figure describes, e.g.
            ``oz``, or ``None`` where none is published.
        portion_value: The size of that portion, or ``None``. Published as
            ``0`` for the extra-portion items, which is a published zero and
            not a missing one.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    item_id: str
    nutrient_key: str
    value: Decimal | None
    unit: str | None
    portion_unit: str | None
    portion_value: Decimal | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class ItemGroupCalories:
    """A published calorie range for a group of interchangeable items.

    Chipotle publishes "170-250 cal" for its lemonades rather than a figure
    per flavour. The range is what is published, so the range is what is
    stored — narrowing it to a midpoint here would be inventing a number that
    nobody published.

    Attributes:
        group_key: The published key for the group, e.g. ``TractorLemonade``.
        display_name: Its published name, e.g. ``Organic Lemonade``.
        default_item_id: The item the group stands in for by default.
        calories_min: The low end of the published range.
        calories_max: The high end.
        display_range_format: Whether Chipotle displays it as a range. ``False``
            where the two ends are equal and it shows a single figure.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    group_key: str
    display_name: str | None
    default_item_id: str | None
    calories_min: Decimal | None
    calories_max: Decimal | None
    display_range_format: bool | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class DietaryTag:
    """One dietary or allergen tag code, and what Chipotle publishes about it.

    Attributes:
        tag_code: The published code, e.g. ``dair``.
        tag_name: Its published label, e.g. ``Dairy``, or ``None`` for a code
            that is used on items but never given a label. Two such codes
            exist — ``whol`` and ``wh30`` — and neither is renamed or guessed
            at here.
        kind: :class:`TagKind`, or ``None`` where the published data does not
            classify the code either way.
        group_id: The published identifier of the group it belongs to.
        group_name: That group's published name, e.g. ``I'm Avoiding``.
        group_subheader: The group's published explanation, e.g. ``Tagged
            items contain your selection.`` — which is what makes a tag a
            statement of presence rather than of suitability.
        badge_text: The short badge the site shows for it.
        sort_order: Where it falls in the published order within its group.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    tag_code: str
    tag_name: str | None
    kind: TagKind | None
    group_id: str | None
    group_name: str | None
    group_subheader: str | None
    badge_text: str | None
    sort_order: int | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class ItemAllergen:
    """What the published data says about one allergen in one item.

    There is a row for every item on the menu paired with every allergen
    Chipotle publishes, so that "nothing is published about this" is a row
    that says so rather than a row that is not there. A join that misses is a
    silence, and this dataset's whole job is to make silences speak.

    Attributes:
        item_id: The item, in the menu's namespace.
        allergen_code: The allergen, keyed into :class:`DietaryTag`.
        status: :class:`AllergenStatus`. Read its docstring before rendering
            any of the three as a sentence.
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
class ItemDiet:
    """What one published document says about one diet and one item.

    Keyed by document as well as by item and diet, which
    :class:`ItemAllergen` is not. The two documents disagree about diets —
    one spells Whole30 ``whol`` and the other ``wh30``, and neither publishes
    a label for either — so a single merged answer would have to pick a
    winner. They do not disagree about allergens, and that agreement is
    checked rather than assumed.

    Covers every published code that is not an allergen, including the two the
    published data never classifies at all. ``wh30`` is a diet to any reader
    and is carried here, but its :class:`DietaryTag` row keeps a null kind
    because nothing published says so.

    Attributes:
        item_id: The item, in the menu's namespace.
        diet_code: The code, keyed into :class:`DietaryTag`.
        status: :class:`DietStatus`, as this document has it.
        source_url: The document the status was read from.
        harvested_at: When that document was fetched.
    """

    item_id: str
    diet_code: str
    status: DietStatus
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class AllergenChartRow:
    """One row of the published allergen chart, as published.

    The chart is the human-facing artefact — the thing a customer reads at
    ``/allergens`` — and is kept here in its own shape as well as flattened
    into :class:`ItemAllergen`, so an answer can cite the chart line it came
    from.

    ``menu_item_id`` is **not** unique in this table. Chipotle publishes
    "Crispy Corn Tortilla" and "Tortilla Chips" as separate chart lines
    against one identifier, and "Romaine Lettuce" and "Supergreens Lettuce
    Blend" against another. The identity of a chart line is its
    ``sort_order``.

    Attributes:
        sort_order: The line's published position, and its identity.
        name: The food as the chart names it.
        menu_item_id: The item identifier the chart gives it, which several
            lines may share.
        allergen_codes: The allergens the chart marks, in published order.
        diet_codes: The diets it lists, in published order.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    sort_order: int
    name: str
    menu_item_id: str | None
    allergen_codes: tuple[str, ...]
    diet_codes: tuple[str, ...]
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class Caveat:
    """One block of Chipotle's published prose about its own allergen data.

    Kept verbatim. The hedges are the point: the paragraph that says foods
    contact one another during preparation is what makes
    :attr:`AllergenStatus.NOT_LISTED` mean "not marked" rather than "free
    of", and an answer that reports the mark without the hedge has changed
    what Chipotle said.

    Attributes:
        position: Where the block falls on the page, and its identity.
        heading: The block's own heading, where it has one.
        text: The published wording, with paragraph breaks kept and nothing
            else altered.
        source_url: The page this was read from.
        harvested_at: When that page was fetched.
    """

    position: int
    heading: str | None
    text: str
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class NutritionDataset:
    """Everything issue #20 harvests, parsed and flattened.

    Attributes:
        nutrients: The published nutrient vocabulary, with labels and units.
        item_nutrition: Every published figure, one row per item per nutrient.
        item_group_calories: Published calorie ranges for grouped items.
        dietary_tags: The published tag vocabulary, allergens and diets alike.
        item_allergens: The three-valued allergen answer, for every item.
        item_diets: What each document says about each diet, per item.
        allergen_chart: The published chart, in its own shape.
        caveats: Chipotle's published prose about the chart's limits.
        allergen_codes: The codes the published data classifies as allergens,
            which is the vocabulary ``item_allergens`` covers.
    """

    nutrients: tuple[Nutrient, ...]
    item_nutrition: tuple[ItemNutrient, ...]
    item_group_calories: tuple[ItemGroupCalories, ...]
    dietary_tags: tuple[DietaryTag, ...]
    item_allergens: tuple[ItemAllergen, ...]
    item_diets: tuple[ItemDiet, ...]
    allergen_chart: tuple[AllergenChartRow, ...]
    caveats: tuple[Caveat, ...]
    allergen_codes: tuple[str, ...]

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

    def coverage(self) -> dict[str, int]:
        """Return how much of the dataset is silence, counted rather than felt.

        A harvest that quietly stopped seeing allergen data would otherwise
        look exactly like a successful one — the tables would still be there
        and every row would still be well formed. Putting the counts in the
        manifest means the day the chart moves, the diff says so.

        Returns:
            Counts of items, of allergen statements by status, and of the
            published nutrient figures that came back null.
        """
        statuses = [row.status for row in self.item_allergens]
        return {
            "items": len({row.item_id for row in self.item_allergens}),
            "allergen_codes": len(self.allergen_codes),
            "contains": statuses.count(AllergenStatus.CONTAINS),
            "not_listed": statuses.count(AllergenStatus.NOT_LISTED),
            "not_published": statuses.count(AllergenStatus.NOT_PUBLISHED),
            "nutrient_figures": len(self.item_nutrition),
            "nutrient_figures_null": sum(
                1 for row in self.item_nutrition if row.value is None
            ),
        }

    def manifest(self) -> dict[str, Any]:
        """Return a description of this dataset, digests and coverage included.

        Returns:
            A JSON-ready mapping.
        """
        return {
            "allergen_codes": list(self.allergen_codes),
            "coverage": self.coverage(),
            "tables": describe(self.tables()),
        }

    def write(
        self, blobs: BlobStore, prefix: str = DEFAULT_PARSED_PREFIX
    ) -> dict[str, str]:
        """Write every table, and the manifest, to the blob store.

        Args:
            blobs: Where to write. The same store the raw bytes landed in,
                under a different prefix.
            prefix: Key prefix for the parsed tables.

        Returns:
            Table name to the key it was written at, with the manifest under
            the key ``manifest``.
        """
        return write_tables(blobs, prefix, self.tables(), self.manifest())
