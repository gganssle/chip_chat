"""The five tables of synthetic account data, and what each column promises.

This is RFC-001 section 04's "demo accounts" half — ``personas``,
``demo_visitors``, ``orders``, ``order_items``, ``loyalty_ledger`` — and the
boundary it sits on is the one issue #25 is told not to blur: **the menu is
real and the accounts are fake.** Nothing in this module describes food. Every
food an order refers to is a foreign key into
:class:`~chip_chat.catalog.MenuCatalog`, and
``test_referential_integrity.py`` asserts that every one of them resolves.

Seven columns are here that section 04 does not list, and each is here
because a table without it cannot be checked:

**``order_items.line_number``.** Section 04 keys an order line by
``(order_id, item_id)``, which cannot hold two burritos built differently —
and a group order that cannot hold two different burritos is not a group
order. The line number is the key; the item is a column on it.

**``order_items.unit_price`` and ``order_items.line_total``.** Without them
``orders.total`` is a number no one can audit, and "prices computed from the
catalogue, not invented" is a claim rather than a test. With them a reviewer
re-derives the total from ``item_prices`` and finds it.

**``orders.channel`` and ``orders.priced_restaurant_id``.** The catalogue
publishes two prices per item — counter and delivery — so a total is
unexplainable until the row says which was used. And Chipotle publishes prices
per restaurant while the population orders from thirty stores, so the row also
says whose prices priced it. See ``docs/decisions/synthetic-population.md``.

**``loyalty_ledger.order_id``.** Issue #27 reconciles the ledger against the
published rewards terms. Reconciling it against the orders that earned it
should be a join, not a regeneration.

**``loyalty_ledger.reward_name``.** Issue #27's third consistency requirement
is that "every redemption traces to a real published reward", and a redemption
that only records what it cost traces to nothing: two rewards may be priced
the same, and a cost is not an identity. The column holds the published
``rewards.name`` verbatim, so the trace is a join onto the harvested Rewards
Exchange rather than an inference from a number.

A sixth table is here that section 04 does not list at all —
``persona_fixtures``, from issue #26 — and
``docs/decisions/persona-fixtures.md`` argues it. In one line: ``personas``
says what kinds of customer exist, and ``persona_fixtures`` says *which
particular customers* are worth showing a visitor, which is a different
question and one the population cannot answer until it has been generated.
"""

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from chip_chat.harvest.blobs import BlobStore
from chip_chat.harvest.sources.chipotle.tables import describe, to_jsonl, write_tables

DEFAULT_PREFIX = "accounts/synthetic"
"""Where the population lands. Beside ``catalog/``, never inside it: the
catalogue is harvested and the population is invented, and the lakehouse of
issue #33 ingests them as two streams with different clocks."""

TABLES = (
    "personas",
    "persona_fixtures",
    "demo_visitors",
    "orders",
    "order_items",
    "loyalty_ledger",
)
"""Table names, in the order the manifest lists them."""

DEMO_ID_FORMAT = "demo-{index:04d}"
"""How a customer is identified. Not a tuning parameter: ``demo_id`` is the
value Snowflake's row access policies compare against, so its shape is part of
the serving contract rather than part of the population."""

ORDER_ID_FORMAT = "ord-{index:07d}"
"""How an order is identified."""

ENTRY_ID_FORMAT = "loy-{index:07d}"
"""How a loyalty ledger entry is identified."""


class Channel(StrEnum):
    """Which published price an order was priced at.

    Attributes:
        IN_STORE: ``item_prices.unit_price``.
        DELIVERY: ``item_prices.unit_delivery_price``, which Chipotle
            publishes as a separate and higher number.
    """

    IN_STORE = "IN_STORE"
    DELIVERY = "DELIVERY"


@dataclass(frozen=True, slots=True)
class Persona:
    """One archetype, minted from the config and the store roster.

    An archetype, not a customer: five hundred customers share seven of these.
    A visitor arriving at the public site is assigned a *customer* — a
    :class:`DemoVisitor` with eighteen months of history — and this row says
    what kind of customer that is, which is what the opening message tells
    them.

    Attributes:
        persona_id: Stable identifier, from the config.
        label: What the demo calls this archetype out loud.
        home_store: The store this archetype's narrative is set at, drawn from
            the roster by seed. A *customer's* own store is
            ``customer_360.favourite_store``, derived from their orders, and
            may legitimately differ; RFC-001 section 04 is explicit that the
            serving layer says so rather than reconciling silently.
        seed_points: Loyalty points the archetype starts with.
        narrative: One sentence describing this *kind* of customer, from the
            config. The archetype's tagline, not any customer's story: the
            sentence a visitor is actually shown is
            :attr:`PersonaFixture.narrative`, which is written from the
            history of the particular customer they were assigned and can
            therefore name their store, their points and their usual. Issue
            #26 asks for a narrative "specific enough to write that sentence
            from"; a row shared by eighty customers cannot be, so the two
            columns are different sentences with different jobs.
    """

    persona_id: str
    label: str
    home_store: int
    seed_points: int
    narrative: str


@dataclass(frozen=True, slots=True)
class PersonaFixture:
    """One customer chosen to *be* an archetype for a visitor arriving.

    Issue #26's subject. ``personas`` says what kinds of customer the
    population contains; this says which particular ones are worth handing to
    a visitor, and it is a different question — the answer depends on
    eighteen months of generated history, so it cannot be written in the
    config beside the archetype and has to be measured after the fact.

    The selection rule is the ticket's own: *"if a fixture cannot demonstrate
    its own metric, it is not finished."* A customer becomes a fixture only by
    clearing the bounds its archetype sets on its own defining behaviour — the
    Regular needs a genuinely dominant usual, the Lapsed Customer needs
    unredeemed points and months of silence, the Explorer needs orders varied
    enough that no usual stands out. There is no code path that admits a
    customer who fails its archetype's bounds, which is what makes
    "every archetype's behaviour is really in the data" checkable rather than
    hopeful. See :mod:`chip_chat.data_gen.fixtures`.

    Every measured column here is also an argument for the sentence in
    :attr:`narrative`, which is written *from* them. A reviewer who doubts the
    narrative re-derives it from the row rather than trusting it, which is the
    same reason ``order_items`` carries ``unit_price``.

    Two absences are deliberate.

    **No display name, and no name in the narrative.** ``display_name`` is one
    of the three columns a visitor may edit (RFC-001 section 04), so a
    narrative with a name baked into it is a sentence that goes stale the
    moment they change it. The opening message of issue #67 joins the live
    name to this sentence; the sentence itself is nameless, which is also how
    the ticket writes it — *"a regular at the Ballard store, 1,250 points, and
    a well-documented weakness for double barbacoa."*

    **No claim about rewards.** :attr:`points_balance` is what this
    population's ledger sums to under the provisional arithmetic in
    ``[loyalty]``. It is stored value the assistant can surface, which is what
    PRD requirement P3 needs of it; it is not a reconciled Chipotle Rewards
    balance, and issue #27 owns making it one.

    Attributes:
        demo_id: Which customer. Joins to ``demo_visitors`` and to every
            visitor-scoped table.
        persona_id: Which archetype they are an exemplar of.
        label: That archetype's label, carried here so the entry flow can
            render a fixture without a second lookup.
        rank: Position among this archetype's fixtures, from one. Rank one is
            the strongest exemplar by the archetype's own measure — the most
            dominant usual for the Regular, the most varied orders for the
            Explorer. A demo that wants one customer takes rank one; a demo
            showing that personas differ takes several.
        home_store: Where they actually order, derived from ``orders.store_id``
            — which is ``customer_360.favourite_store``'s definition and not
            ``personas.home_store``, an archetype's narrative setting. The two
            may legitimately differ.
        home_store_name: That store's published name, or ``None`` if the
            locator publishes none. Read off the catalogue, never invented:
            the narrative names a real restaurant or it names none.
        store_share: The fraction of their orders placed there. The Regular's
            is near one and the Explorer's is not, which is half of what makes
            them different people.
        distinct_stores: How many stores they have ordered from at all.
        entrees_per_order: Mean entrees per order. Above two is somebody
            ordering for other people, which is what makes the group orderer
            visible in data generated before V0 scopes them in.
        order_count: How many orders they have placed.
        lifetime_spend: What those orders totalled.
        first_order_at: Their first order.
        last_order_at: Their most recent.
        days_since_order: Days from :attr:`last_order_at` to the end of the
            generated window — *not* to now. The window's end is fixed in the
            config, so this number is reproducible; measuring it against the
            wall clock would make a lapsed customer lapse further every day.
        points_balance: What their ledger sums to. Unredeemed stored value,
            under the provisional arithmetic named above.
        usual_item_id: The entree in the basket they order most often, or
            ``None`` when they have placed no order carrying one. A
            ``menu_items.item_id``, as everywhere else in this package.
        usual_modifiers: How they have it built. ``modifiers.modifier_id``
            values of that item, sorted.
        usual_share: The fraction of their orders that are exactly that
            basket. The Regular's defining number and the Explorer's, read in
            opposite directions. Deliberately *not* called confidence: the
            ``usual_order`` gold mart computes a confidence its own way and
            this must not be mistaken for it. Fixtures are chosen at the
            extremes of this measure precisely so that any reasonable
            definition of confidence agrees about them.
        distinct_baskets: How many different baskets they have ordered. The
            Explorer's defining number.
        narrative: The sentence a visitor is shown on being assigned this
            customer. Filled from the columns above out of the template its
            archetype carries in the config, so every number in it is a
            number in this row and every food in it is a food in the
            catalogue.
    """

    demo_id: str
    persona_id: str
    label: str
    rank: int
    home_store: int
    home_store_name: str | None
    store_share: float
    distinct_stores: int
    entrees_per_order: float
    order_count: int
    lifetime_spend: Decimal
    first_order_at: datetime
    last_order_at: datetime
    days_since_order: int
    points_balance: int
    usual_item_id: str | None
    usual_modifiers: tuple[str, ...]
    usual_share: float
    distinct_baskets: int
    narrative: str


@dataclass(frozen=True, slots=True)
class DemoVisitor:
    """One synthetic customer: the thing a public visitor is assigned.

    Attributes:
        demo_id: The identifier every visitor-scoped row carries and every row
            access policy compares against.
        display_name: An invented name. There is no PII in this system by
            construction (RFC-001 section 02), and none here.
        persona_id: Which archetype this customer is.
        thread_id: The Foundry thread, or ``None``. Always ``None`` here: a
            thread exists once a visitor has said something, and this
            population has never spoken to anyone.
        home_store_override: A store the visitor has said they prefer, or
            ``None``. Editable by the visitor at runtime, and read by no
            Databricks job — that containment is the whole mechanism behind
            RFC-001 section 04's answer to PRD Q2.
        stated_preferences: Free text the visitor has typed, or ``None``.
            About preference, never about allergy: an allergen answer comes
            from the published chart.
        created_at: When this customer's history begins.
        last_seen: Their most recent order, or ``created_at`` if they never
            placed one.
    """

    demo_id: str
    display_name: str
    persona_id: str
    thread_id: str | None
    home_store_override: int | None
    stated_preferences: str | None
    created_at: datetime
    last_seen: datetime


@dataclass(frozen=True, slots=True)
class Order:
    """One order, at one store, at one instant, for one published total.

    Attributes:
        order_id: The identifier.
        demo_id: Whose order it is.
        store_id: Where it happened. A ``stores.store_id`` from the catalogue.
        placed_at: When, in UTC, inside that store's published opening hours.
        status: One of the configured statuses.
        total: The sum of every line, in dollars.
        channel: Which published price priced it.
        priced_restaurant_id: Whose published prices priced it. Equal to
            ``store_id`` when the catalogue priced that restaurant, and the
            catalogue's reference restaurant when it did not — Chipotle
            publishes a menu per restaurant, and the harvest prices as many as
            it was asked to.
    """

    order_id: str
    demo_id: str
    store_id: int
    placed_at: datetime
    status: str
    total: Decimal
    channel: Channel
    priced_restaurant_id: int


@dataclass(frozen=True, slots=True)
class OrderItem:
    """One line of one order: a real catalogue item, built from real modifiers.

    Attributes:
        order_id: Which order.
        line_number: Which line, from one. Two lines may carry the same item
            built differently, which is what a group order is.
        item_id: A ``menu_items.item_id``. Never anything else.
        qty: How many.
        modifiers: ``modifiers.modifier_id`` values, sorted. Each is keyed
            ``(item_id, modifier_item_id)`` in the catalogue, so a modifier
            here names both the thing modified and the thing added, and a
            modifier belonging to a different item cannot be attached to this
            one without the check noticing.
        unit_price: The published price of one, at :attr:`Order.channel`.
        line_total: ``qty`` times ``unit_price`` plus every modifier's own
            published price. A modifier the catalogue prices at zero is free
            here too, because that is what is published.
    """

    order_id: str
    line_number: int
    item_id: str
    qty: int
    modifiers: tuple[str, ...]
    unit_price: Decimal
    line_total: Decimal


@dataclass(frozen=True, slots=True)
class LoyaltyEntry:
    """One movement of loyalty points, and what moved them.

    Attributes:
        entry_id: The identifier.
        demo_id: Whose points.
        delta: Signed. Positive earns, negative redeems or expires.
        reason: One of the configured reasons.
        order_id: The order this movement happened on, or ``None`` for an
            opening balance and for an expiry. A redemption names the order it
            was spent on, which is the order that also earned on it.
        reward_name: The published ``rewards.name`` this redemption was spent
            on, or ``None`` for every entry that is not a redemption. Never a
            name this package composed: the value comes off a harvested
            Rewards Exchange row.
        created_at: When.
    """

    entry_id: str
    demo_id: str
    delta: int
    reason: str
    order_id: str | None
    reward_name: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SyntheticPopulation:
    """Everything issue #25 generates, and which catalogue it was composed from.

    Attributes:
        seed: The seed that produced it.
        catalog_content_version: The catalogue's
            :meth:`~chip_chat.catalog.MenuCatalog.content_version` — the
            digest of *what is orderable*, with provenance stripped. Recorded
            rather than the full ``catalog_version`` on purpose: two harvests
            of an unchanged menu compose the same orders, and a population
            should not be invalidated by having been read on a Tuesday.
        rewards_content_version: The published rewards programme's
            :meth:`~chip_chat.data_gen.rewards.RewardsTerms.content_version` —
            the digest of the earn rate, the expiry window, the daily cap and
            the Rewards Exchange line-up. Recorded for the same reason as the
            catalogue's: the ledger's arithmetic is an input to this
            population, and a balance that looks wrong should be traceable to
            the terms it was computed under rather than argued about.
        window_starts_at: The first instant an order could fall in.
        window_ends_at: The last.
        personas: The archetypes.
        persona_fixtures: The customers chosen to demonstrate each archetype.
        demo_visitors: The synthetic customers.
        orders: Their orders.
        order_items: Those orders' lines.
        loyalty_ledger: Their points.
    """

    seed: int
    catalog_content_version: str
    rewards_content_version: str
    window_starts_at: datetime
    window_ends_at: datetime
    personas: tuple[Persona, ...]
    persona_fixtures: tuple[PersonaFixture, ...]
    demo_visitors: tuple[DemoVisitor, ...]
    orders: tuple[Order, ...]
    order_items: tuple[OrderItem, ...]
    loyalty_ledger: tuple[LoyaltyEntry, ...]

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

    def version(self) -> str:
        """Return the digest that identifies this population exactly.

        This is the number issue #25's first acceptance criterion is about.
        Two runs at the same seed against the same catalogue produce the same
        digest; any change to any row changes it.

        Returns:
            A SHA-256 hex digest over every table's serialised bytes.
        """
        running = hashlib.sha256()
        for name, rows in self.tables():
            running.update(name.encode("utf-8"))
            running.update(b"\n")
            running.update(to_jsonl(rows))
        return running.hexdigest()

    def manifest(self) -> dict[str, Any]:
        """Return a description of this population, digests and inputs included.

        The manifest names every half of what made it — the seed, the
        catalogue's ``content_version`` and the published rewards programme's —
        so a gold mart that looks wrong can be traced back to an input rather
        than argued about.

        Returns:
            A JSON-ready mapping.
        """
        return {
            "population_version": self.version(),
            "seed": self.seed,
            "catalog_content_version": self.catalog_content_version,
            "rewards_content_version": self.rewards_content_version,
            "window_starts_at": self.window_starts_at.isoformat(),
            "window_ends_at": self.window_ends_at.isoformat(),
            "tables": describe(self.tables()),
        }

    def write(self, blobs: BlobStore, prefix: str = DEFAULT_PREFIX) -> Mapping[str, str]:
        """Write every table, and the manifest, to the blob store.

        Args:
            blobs: Where to write. In the deployed system this is the ADLS
                Gen2 landing zone; on a laptop it is a directory.
            prefix: Key prefix for the population.

        Returns:
            Table name to the key it was written at, with the manifest under
            the key ``manifest``.
        """
        return write_tables(blobs, prefix, self.tables(), self.manifest())
