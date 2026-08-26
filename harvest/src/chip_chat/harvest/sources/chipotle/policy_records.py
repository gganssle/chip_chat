"""The parsed shape of Chipotle's published policies, FAQ, catering and stores.

Issue #21 is the policy half of the corpus: the rewards terms, the ordering
and refund answers, what catering is, and enough real stores that "the Ballard
store" resolves to somewhere. Ten flat tables come out, and the same two
conventions run through them as through the menu tables of issue #19.

**Every row carries ``source_url`` and ``harvested_at``.** Not the table, the
row — and here that matters more than anywhere else in the harvest, because a
policy answer is exactly the kind of answer a visitor is entitled to see
sourced. RFC-001 section 08 requires the citation to survive into the response
payload, and a section that lost its URL on the way into the index cannot get
one back.

**A row cites exactly one document.** That is why a store's address and a
store's *name* are two tables rather than two halves of one row: the locator
page publishes the address, hours and coordinates but calls every restaurant
"Chipotle Mexican Grill", and the restaurant API publishes the name the
company actually uses for it. Merging them would produce a row with two
provenances and therefore, in practice, none.

Money is :class:`~decimal.Decimal`, parsed from the JSON token's own text and
serialised back as a string, for the reason the menu tables give: a price is
not a measurement and must not pick up binary-float noise on the way through a
dataset whose job is to reproduce byte for byte.
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.sources.chipotle.tables import describe, write_tables

DEFAULT_PARSED_PREFIX = "parsed/chipotle/policy"
"""Where the parsed tables land. Beside ``raw/``, never inside it."""

TABLES = (
    "policy_documents",
    "policy_sections",
    "faq_categories",
    "faq_entries",
    "rewards",
    "catering_packages",
    "catering_package_options",
    "stores",
    "store_profiles",
    "store_hours",
)
"""Table names, in the order the manifest lists them."""

MINIMUM_STORES = 30
"""How many stores issue #21 requires.

Checked at parse time rather than asserted in a document, so a locator change
that quietly shrank the dataset stops the harvest instead of shipping a
``stores`` table too small to make a home store mean anything.
"""


@dataclass(frozen=True, slots=True)
class PolicyDocument:
    """One published policy page, identified so its sections can point at it.

    Attributes:
        document_id: A stable slug for the document, e.g. ``rewards-terms``.
            Chosen here rather than read from the page, because the page
            publishes no identifier and the sections need something to join
            on.
        kind: What sort of document it is: ``TERMS`` for a contract a visitor
            is bound by, ``OVERVIEW`` for a page that explains one. The
            distinction is the retrieval layer's business — an answer about
            what the rules *are* should prefer the former.
        title: The page's own title component, where it publishes one.
        section_count: How many sections it was split into.
        source_url: The page this row describes.
        harvested_at: When it was fetched.
    """

    document_id: str
    kind: str
    title: str | None
    section_count: int
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class PolicySection:
    """One section of a policy document, with its boundary preserved.

    Attributes:
        document_id: The document this came from.
        position: Where the section falls in it, from zero.
        heading: The heading that opened it, or ``None`` for the prose before
            the first one.
        text: The section's visible text, paragraph breaks kept as newlines.
        source_url: The page this row was read from.
        harvested_at: When that page was fetched.
    """

    document_id: str
    position: int
    heading: str | None
    text: str
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class FaqCategory:
    """One heading in the published FAQ's two-level table of contents.

    Chipotle files 136 answers under ten categories and their subcategories,
    and publishes an explicit order for both. That order is the document
    structure of a FAQ, so it is a table rather than something the parser
    flattens away.

    Attributes:
        category: The top-level heading, e.g. ``Rewards Program``.
        category_position: Its published position, from zero.
        subcategory: The heading below it, or ``None`` where the FAQ files
            questions directly under the category.
        subcategory_position: Its position within the category, from zero.
        entry_count: How many questions it holds.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    category: str
    category_position: int
    subcategory: str | None
    subcategory_position: int
    entry_count: int
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class FaqEntry:
    """One published question and its answer.

    Attributes:
        category: The category it is filed under.
        subcategory: The subcategory, or ``None``.
        rank: The position Chipotle publishes for it within the subcategory.
        question: The question as published.
        answer: The answer as published, with its paragraph and list breaks
            kept as newlines.
        links: Every URL the answer links to, in the order they appear.
            Carried separately because the answer text keeps the *words* a
            link was made of, and a URL that only existed in an ``href`` would
            otherwise be lost — several answers point at the page that has the
            rest of the story.
        is_top_question: Whether the FAQ marks it as a top question.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    category: str
    subcategory: str | None
    rank: int
    question: str
    answer: str
    links: tuple[str, ...]
    is_top_question: bool
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class Reward:
    """One reward in the published Rewards Exchange, and what it costs.

    This is the table issue #23's ``redeem_points`` is meant to be derived
    from, which is why it holds published point costs and not plausible ones.

    Attributes:
        position: Where the reward falls in the published line-up, from zero.
        name: The reward's name, e.g. ``ENTRÉE AND CHIPS``.
        point_cost: What it costs, in points.
        image_path: The path of the picture published with it. Kept verbatim,
            and deliberately *not* resolved into an item identifier: half the
            tiles use marketing art, and an "ENTRÉE" is not the burrito its
            picture happens to show. Joining a reward to what it redeems for
            is issue #24's job, with the whole catalogue in hand.
        source_url: The page this row was read from.
        harvested_at: When that page was fetched.
    """

    position: int
    name: str
    point_cost: int
    image_path: str | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class CateringPackage:
    """One thing a catering order can be made of, priced nationally.

    Catering prices come back with ``restaurantNumber`` zero — one national
    price list rather than the per-restaurant pricing the ordinary menu has.
    That is Chipotle's own arrangement and it is preserved rather than
    reconciled: see ``docs/decisions/menu-pricing.md`` for what a quoted price
    means on the other side of the menu.

    Attributes:
        package_id: Chipotle's identifier, e.g. ``CMG-4105``.
        name: The internal name, e.g. ``Build Your Own``.
        display_name: The name shown to a customer, where it differs.
        display_sub_name: The tier's own subtitle, where it has one.
        description: The published prose about the package.
        tagline: The short line published beside it, where there is one.
        unit: What one of it is, as published: ``EACH``, ``FEE``.
        display_unit: The word a quantity is counted in: ``burrito``,
            ``person``, ``pack``.
        min_price: The lowest published price per unit.
        max_price: The highest. Chipotle publishes a range because the price
            depends on which protein is chosen.
        min_quantity: The smallest order accepted.
        max_quantity: The largest.
        quantity_increment: What a quantity must be a multiple of.
        serves: How many people one unit serves, as published prose.
        base_count: How many bases a customer chooses.
        protein_count: How many proteins.
        topping_count: How many toppings.
        premium_topping_count: How many premium toppings.
        salsa_count: How many salsas.
        tortilla_count: How many tortillas.
        sort_order: The published display order.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    package_id: str
    name: str
    display_name: str | None
    display_sub_name: str | None
    description: str | None
    tagline: str | None
    unit: str | None
    display_unit: str | None
    min_price: Decimal | None
    max_price: Decimal | None
    min_quantity: int | None
    max_quantity: int | None
    quantity_increment: int | None
    serves: str | None
    base_count: int | None
    protein_count: int | None
    topping_count: int | None
    premium_topping_count: int | None
    salsa_count: int | None
    tortilla_count: int | None
    sort_order: int | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class CateringOption:
    """One item that may go into a catering package.

    Attributes:
        package_id: The package.
        slot: The list Chipotle published it in, spelled as Chipotle spells
            it: ``bases``, ``proteins``, ``toppings``, ``premiumToppings``,
            ``salsas``, ``tortillas``, and for the included ones ``fillings``
            and ``sides``. Kept verbatim rather than renamed, because the
            published name is the one that can be checked against the source.
        position: Where it falls in that list, from zero.
        item_id: Chipotle's identifier for the item.
        name: Its published name.
        pos_id: The point-of-sale identifier, where the package publishes one.
        is_included: Whether it comes with the package rather than being
            chosen. Burritos by the Box arrives with chips, salsa and sour
            cream nobody picked.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    package_id: str
    slot: str
    position: int
    item_id: str
    name: str
    pos_id: str | None
    is_included: bool
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class Store:
    """One restaurant, as its published locator page describes it.

    Attributes:
        store_id: Chipotle's restaurant number. The same identifier
            ``item_prices.restaurant_id`` uses, which is what makes a price
            and a place joinable.
        street_address: The street line, as published.
        city: The city. The locator publishes it with the county appended for
            some stores — "Lakewood, Los Angeles" — and it is kept as
            published rather than trimmed to a guess about which half is
            which.
        region: The state or territory code, e.g. ``CA``.
        postal_code: The postcode.
        country: The country code.
        latitude: Published latitude.
        longitude: Published longitude.
        telephone: The published telephone number.
        page_url: The locator page for this store. The same value as
            ``source_url``; kept as its own column because it is the link an
            answer would show a visitor, whereas ``source_url`` is provenance.
        source_url: The page this row was read from.
        harvested_at: When that page was fetched.
    """

    store_id: int
    street_address: str | None
    city: str | None
    region: str | None
    postal_code: str | None
    country: str | None
    latitude: float | None
    longitude: float | None
    telephone: str | None
    page_url: str
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class StoreProfile:
    """What the restaurant API publishes about a store, including its name.

    A separate table from :class:`Store` because it is a separate document.
    It is also the only place the store's *name* is published: the locator
    page calls all four thousand of them "Chipotle Mexican Grill", and it is
    this endpoint that says one of them is "Ballard".

    Attributes:
        store_id: Chipotle's restaurant number.
        name: The name the company uses for the restaurant, e.g. ``Lakewood
            Mall``.
        location_type: What kind of location it is, e.g. ``RESTAURANT``.
        status: Whether it is open, as published.
        real_estate_category: e.g. ``Freestanding``, ``Endcap``, ``Inline``.
        operational_region: Chipotle's own region, e.g. ``Pacific North``.
        operational_sub_region: The region below that.
        market_area: The designated market area, e.g. ``SEATTLE-TACOMA``.
        opened_on: The published opening date.
        source_url: The endpoint this row was read from.
        harvested_at: When that endpoint was fetched.
    """

    store_id: int
    name: str | None
    location_type: str | None
    status: str | None
    real_estate_category: str | None
    operational_region: str | None
    operational_sub_region: str | None
    market_area: str | None
    opened_on: str | None
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class StoreHours:
    """When one store is open on one day of the week.

    There is a row for every store crossed with all seven days, and a day
    nobody published opening times for is a row saying so rather than a row
    that is not there — the same rule the allergen tables follow, for the same
    reason. "We publish nothing about Sunday" and "closed on Sunday" are
    different answers and a missing row cannot tell them apart.

    Attributes:
        store_id: Chipotle's restaurant number.
        day_of_week: The day, e.g. ``Monday``.
        opens: Published opening time, ``HH:MM`` as published.
        closes: Published closing time.
        is_published: Whether the page published times for this day at all.
        source_url: The page this row was read from.
        harvested_at: When that page was fetched.
    """

    store_id: int
    day_of_week: str
    opens: str | None
    closes: str | None
    is_published: bool
    source_url: str
    harvested_at: datetime


@dataclass(frozen=True, slots=True)
class PolicyDataset:
    """Everything issue #21 harvests, parsed and flattened.

    Attributes:
        store_ids: The restaurants whose locator pages were read, in order.
        reference_restaurant_id: The restaurant the menu harvest prices at,
            which is in ``stores`` by construction so that a price and a place
            can be quoted together.
        policy_documents: The policy pages, one row each.
        policy_sections: Their sections, boundaries intact.
        faq_categories: The FAQ's two-level table of contents.
        faq_entries: Its questions and answers.
        rewards: The published Rewards Exchange line-up and its point costs.
        catering_packages: What a catering order can be made of.
        catering_package_options: What goes in each package.
        stores: Address, coordinates and telephone, per store.
        store_profiles: Name and operational metadata, per store.
        store_hours: Opening times, per store per day.
    """

    store_ids: tuple[int, ...]
    reference_restaurant_id: int
    policy_documents: tuple[PolicyDocument, ...]
    policy_sections: tuple[PolicySection, ...]
    faq_categories: tuple[FaqCategory, ...]
    faq_entries: tuple[FaqEntry, ...]
    rewards: tuple[Reward, ...]
    catering_packages: tuple[CateringPackage, ...]
    catering_package_options: tuple[CateringOption, ...]
    stores: tuple[Store, ...]
    store_profiles: tuple[StoreProfile, ...]
    store_hours: tuple[StoreHours, ...]

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

        Returns:
            A JSON-ready mapping.
        """
        return {
            "store_ids": list(self.store_ids),
            "reference_restaurant_id": self.reference_restaurant_id,
            "tables": describe(self.tables()),
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
        return write_tables(blobs, prefix, self.tables(), self.manifest())
