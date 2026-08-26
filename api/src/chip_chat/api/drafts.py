"""The draft store: app-owned, catalogue-priced, and the reason confirmation binds.

RFC-001 section 06 puts the confirmation rule in the ops API rather than in the
prompt, and issue #62 is the half of that rule which has to exist first. The ops
API rejects any draft not marked confirmed; that sentence is only enforceable if
the flag it reads lives somewhere the model cannot reach. So it lives here, in
the app tier, on a record the agent can cause to be *minted* and can never cause
to be *confirmed*.

Four properties, each of which is a test in ``api/tests/test_drafts.py``:

**A draft is composed only of real catalogue rows.** Every ``item_id``,
every modifier pairing and every portion word is looked up in
:class:`~chip_chat.catalog.records.MenuCatalog` at proposal time, and anything
that is not there is a typed rejection rather than a line on a card. A draft
naming a SKU that does not exist is not something this module rejects later --
it is something it cannot mint.

**A draft is priced from the catalogue, at a named restaurant.** Money is a
column on a restaurant (``docs/decisions/menu-pricing.md``), so a draft carries
the ``restaurant_id`` it was priced at and the ``harvested_at`` of the price rows
it used. Modifier deltas are the modifier's own published price, which is $0.00
for every rice, bean and salsa and real money for a second protein.

**A draft belongs to one visitor.** :meth:`DraftStore.propose` binds the draft to
the ``demo_id`` the app resolved from the session cookie, and every other method
takes that ``demo_id`` again. A draft id presented with the wrong one is a
``DRAFT_NOT_FOUND`` -- the same answer as an id that never existed, because
"someone else has this" is a fact a stranger is not owed. See
``docs/action-surface.md`` section 7, first bullet.

**A draft goes stale.** Drafts expire, and an expired draft is neither
confirmable nor claimable. A price quoted from a harvest is a quote and not a
promise, and a confirmation card left open in a tab overnight is exactly how a
stale quote becomes a wrong charge.

**What the model can do, and what it cannot.** ``propose_order`` reaches
:meth:`DraftStore.propose` and :meth:`DraftStore.revise`. Nothing the model emits
reaches :meth:`DraftStore.confirm`, which is called by the request handler when
the visitor presses Confirm -- the ``confirm_draft_id`` field of
:class:`~chip_chat.api.app.ChatRequest`, which arrives on the request beside the
session cookie rather than inside a tool call.

**Where this sits relative to** :mod:`chip_chat.agent.orders`. That module holds
the same rule against the three hardcoded items of the week-one slice, in the
agent package, and says in its own docstring that it goes away when the ops API
lands. This is the app-tier store the ops API (#63) reads and the FastAPI app
(#66) confirms against; the switchover belongs to those issues, not this one.

**In memory, and therefore per replica.** The same honest limitation
:class:`~chip_chat.api.ledger.BudgetLedger` carries: one replica, one store, and
one obvious place for a shared implementation to land. A restart forgets every
open draft, which costs a visitor one re-proposal and costs correctness nothing
-- a forgotten draft is a ``DRAFT_NOT_FOUND``, never a draft placed unconfirmed.
"""

import secrets
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from chip_chat.agent.hardcoded import SIMULATION_NOTICE
from chip_chat.api.clock import Clock, SystemClock
from chip_chat.catalog import ItemPrice, MenuCatalog, MenuItem, Modifier, Store

__all__ = [
    "DEFAULT_DRAFT_TTL_SECONDS",
    "Draft",
    "DraftLine",
    "DraftRejectedError",
    "DraftStore",
    "OrderType",
    "RejectionCode",
    "Selection",
]

DEFAULT_DRAFT_TTL_SECONDS = 900.0
"""How long a draft stays confirmable. Fifteen minutes.

**[INVENTED: draft TTL]**, as ``docs/action-surface.md`` section 7.1 rule 11 and
section 10 both say. Nothing published fixes a number; what is published is that
prices move between stores and between harvests, so a quote has to age.
"""

ENTREE_CATEGORY = "Entree"
"""The one category whose quantity cap is 1. See :data:`_MAX_QUANTITY`."""

_MAX_QUANTITY = 5
"""Most of anything but an entree that may go on one line.

Section 7.1 rule 4 is ``1 <= quantity <= menu_items.max_quantity``, and
``max_quantity`` is a harvest column the catalogue does not carry -- see
``docs/decisions/catalog-shape.md`` for what it does carry and why. The rule is
therefore flattened to the two numbers section 7.1 says it takes in practice: one
for every entree, five for the sides, drinks and hardware. Carrying the column
through the catalogue -- with the aggregate-cap columns rule 9 needs, which it
also drops -- is ``cc-of1``, and is its own piece of work because it moves the
catalogue's stability digests.
"""

_MAX_DRAFTS = 4_096
"""Open drafts held before the oldest are forgotten.

A bound on memory and nothing else. Eviction drops the least recently minted,
which is the one whose visitor is least likely to still be looking at its card,
and a dropped draft is a ``DRAFT_NOT_FOUND`` rather than anything worse.
"""

_CENTS = Decimal("0.01")

_FREE = Decimal("0")
"""What a modifier costs when the catalogue publishes no price row for it.

Not a guess at what it might cost: the published menu charges separately for
twenty of the seventy-two modifier items and includes the rest, and a modifier
with no ``item_prices`` row is one of the included ones.
:mod:`chip_chat.data_gen.catalogue` reads it the same way, for the same reason.

An *orderable* item with no price row is read the other way -- unavailable --
because there the missing row would otherwise become a free burrito. Both
readings are fail-closed; they just fail closed in opposite directions.
"""


class OrderType(StrEnum):
    """Which of the two published price columns a draft is quoted in.

    ``docs/action-surface.md`` section 7.1 rule 10: a delivery draft is priced at
    ``unit_delivery_price`` throughout and every item on it has to be eligible
    for delivery. Mixing the columns on one card is a wrong total rather than a
    rounding difference, so the choice is made once per draft and applied to the
    lines and the modifiers alike.

    It is an argument to :meth:`DraftStore.propose` and *not* a tool argument:
    whether the model is given one is issue ``cc-c2c``, and until it is answered
    the app decides.
    """

    PICKUP = "pickup"
    DELIVERY = "delivery"


class RejectionCode(StrEnum):
    """The typed refusals a draft can produce.

    The spellings are ``docs/action-surface.md`` section 7.1's, not new ones, so
    that Phase 9's evaluations group on one vocabulary rather than two.
    :data:`RejectionCode.DRAFT_NOT_FOUND` and :data:`RejectionCode.EMPTY_ORDER`
    are the two :mod:`chip_chat.agent.orders` already publishes.

    Three of section 7.1's codes are absent, and it is worth saying which:
    ``CAP_EXCEEDED`` needs the ``counts_toward_*`` weights the catalogue does not
    carry, ``BUDGET_EXCEEDED`` belongs to :mod:`chip_chat.api.guard`, and
    ``ORDER_NOT_FOUND`` belongs to a placed order rather than to a draft.
    """

    EMPTY_ORDER = "EMPTY_ORDER"
    ITEM_NOT_ORDERABLE = "ITEM_NOT_ORDERABLE"
    ITEM_UNAVAILABLE_AT_STORE = "ITEM_UNAVAILABLE_AT_STORE"
    QUANTITY_EXCEEDS_MAX = "QUANTITY_EXCEEDS_MAX"
    MODIFIER_NOT_OFFERED = "MODIFIER_NOT_OFFERED"
    PORTION_NOT_OFFERED = "PORTION_NOT_OFFERED"
    REQUIRED_SLOT_EMPTY = "REQUIRED_SLOT_EMPTY"
    SLOT_OVERFILLED = "SLOT_OVERFILLED"
    NOT_ELIGIBLE_FOR_DELIVERY = "NOT_ELIGIBLE_FOR_DELIVERY"
    STORE_NOT_PRICED = "STORE_NOT_PRICED"
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    DRAFT_NOT_CONFIRMED = "DRAFT_NOT_CONFIRMED"
    DRAFT_EXPIRED = "DRAFT_EXPIRED"


class DraftRejectedError(Exception):
    """A draft was refused, naming the rule that refused it.

    Never repaired into validity. RFC-001 section 06 forbids the agent rounding a
    bad draft into a good one, so the caller is told which rule said no and asks
    the visitor rather than trying again with something adjacent.
    """

    def __init__(self, code: RejectionCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_result(self) -> Mapping[str, str]:
        """The rejection as a tool result: what the model reads, and the span."""
        return {"rejected": self.code.value, "detail": self.message}


@dataclass(frozen=True, slots=True)
class Selection:
    """One modifier on one line, priced and with its published portion.

    Attributes:
        modifier_item_id: The catalogue identifier of the thing added.
        name: Its published name.
        group_name: The published content group it belongs to, or ``None`` for
            an ungrouped modifier. Read off the ``modifiers`` row rather than
            taken from the caller, so a caller cannot move a modifier into a
            group it was not published in.
        portion: The published portion word, or ``None`` for a whole one.
        unit_price: What this modifier adds to one of the item, in the draft's
            order type. :data:`_FREE` for a modifier the menu includes.
        is_default: Whether the item comes with it unless it is removed. A
            default the visitor never named still belongs on the card, because
            it is on the food -- section 7.1, *Pricing*.
    """

    modifier_item_id: str
    name: str
    group_name: str | None
    portion: str | None
    unit_price: Decimal
    is_default: bool = False

    def as_dict(self) -> Mapping[str, Any]:
        """The selection as the card renders it."""
        return {
            "modifier_item_id": self.modifier_item_id,
            "name": self.name,
            "group_name": self.group_name,
            "portion": self.portion,
            "unit_price": _money(self.unit_price),
            "is_default": self.is_default,
        }


@dataclass(frozen=True, slots=True)
class DraftLine:
    """One line of a draft: an item, how many, and what is on it.

    Attributes:
        item_id: The catalogue identifier.
        name: The published name.
        quantity: How many, within the cap of :data:`_MAX_QUANTITY`.
        unit_price: The item's own published price, in the draft's order type.
        selections: The modifiers, in the order they were resolved: the ones the
            caller named first, then the defaults it did not.
    """

    item_id: str
    name: str
    quantity: int
    unit_price: Decimal
    selections: tuple[Selection, ...] = ()

    @property
    def unit_total(self) -> Decimal:
        """What one of this line costs, modifiers included."""
        return self.unit_price + sum(
            (selection.unit_price for selection in self.selections), _FREE
        )

    @property
    def line_total(self) -> Decimal:
        """What the whole line costs, rounded to cents once."""
        return (self.unit_total * self.quantity).quantize(_CENTS, rounding=ROUND_HALF_UP)

    def as_dict(self) -> Mapping[str, Any]:
        """The line as the card renders it."""
        return {
            "item_id": self.item_id,
            "name": self.name,
            "quantity": self.quantity,
            "unit_price": _money(self.unit_price),
            "selections": [selection.as_dict() for selection in self.selections],
            "line_total": _money(self.line_total),
        }


@dataclass(frozen=True, slots=True)
class Draft:
    """A proposed order, waiting for the visitor to confirm it.

    Frozen, and confirmation replaces the record rather than mutating it, so
    there is no attribute anywhere in the process that a stray assignment could
    set to ``True``.

    Attributes:
        draft_id: The opaque id the card carries and ``place_order`` takes.
        demo_id: The visitor it was minted for. Resolved from the session by the
            app; never supplied by a client and never by a tool argument.
        restaurant_id: Whose published prices this draft is quoted in.
        order_type: Which price column it is quoted in.
        lines: What is on it.
        created_at: When it was minted.
        expires_at: When it stops being confirmable, in wall-clock time, for the
            card. The check itself reads a monotonic clock -- see
            :attr:`expires_after`.
        expires_after: The monotonic instant the TTL ends at. Monotonic because a
            system clock that steps backwards must not resurrect a dead draft.
        confirmed: Whether the visitor pressed Confirm. Set only by
            :meth:`DraftStore.confirm`.
        priced_at: The newest ``harvested_at`` among the price rows this draft
            used. A quoted price cites when it was read, per RFC-001 section 08.
        content_version: The catalogue build it was composed against.
        supersedes: The draft this one replaced, for a card edited in place, or
            ``None`` for a first proposal.
    """

    draft_id: str
    demo_id: str
    restaurant_id: int
    order_type: OrderType
    lines: tuple[DraftLine, ...]
    created_at: datetime
    expires_at: datetime
    expires_after: float
    confirmed: bool = False
    priced_at: datetime | None = None
    content_version: str | None = None
    supersedes: str | None = None

    @property
    def total(self) -> Decimal:
        """What the card shows, rounded to cents once at the end."""
        return sum((line.line_total for line in self.lines), _FREE).quantize(
            _CENTS, rounding=ROUND_HALF_UP
        )

    def as_card(self, store: Store | None = None) -> Mapping[str, Any]:
        """The confirmation card, which is what the widget renders.

        Args:
            store: The store row for :attr:`restaurant_id`, where the catalogue
                has one. :meth:`DraftStore.card` supplies it.

        Returns:
            A JSON-serialisable mapping. Money is a string throughout: a card
            that went through a float on the way to a browser is a card that can
            show a total nobody computed.
        """
        return {
            "draft_id": self.draft_id,
            "store": _store_block(self.restaurant_id, store),
            "order_type": self.order_type.value,
            "lines": [line.as_dict() for line in self.lines],
            "total": _money(self.total),
            "requires_confirmation": True,
            "confirmed": self.confirmed,
            "expires_at": self.expires_at.isoformat(),
            "supersedes": self.supersedes,
            "pricing": {
                "restaurant_id": self.restaurant_id,
                "harvested_at": (
                    self.priced_at.isoformat() if self.priced_at is not None else None
                ),
                "content_version": self.content_version,
            },
            "notice": SIMULATION_NOTICE,
        }


class DraftStore:
    """Mints drafts against the catalogue, and holds the confirmation flag.

    Thread-safe. A Container App serves concurrent requests, and a draft
    dictionary mutated from two of them is the kind of bug that appears once,
    in front of somebody.

    One instance per process holds every visitor's drafts, keyed by draft id and
    scoped by ``demo_id`` on the way in and out. A per-visitor store would put
    the scoping in the caller, which is exactly where it must not be.
    """

    __slots__ = (
        "_by_item",
        "_catalog",
        "_clock",
        "_content_version",
        "_drafts",
        "_groups",
        "_lock",
        "_max_drafts",
        "_modifiers",
        "_offered",
        "_prices",
        "_stores",
        "_ttl_seconds",
    )

    def __init__(
        self,
        catalog: MenuCatalog,
        *,
        clock: Clock | None = None,
        ttl_seconds: float = DEFAULT_DRAFT_TTL_SECONDS,
        max_drafts: int = _MAX_DRAFTS,
    ) -> None:
        """Assemble the store around a catalogue.

        Args:
            catalog: The built catalogue. Required and without a default: a
                store that could be built without one would be a store pricing
                against nothing.
            clock: Source of time. Defaults to the system clock.
            ttl_seconds: How long a draft stays confirmable.
            max_drafts: Open drafts held before the oldest are evicted.
        """
        self._catalog = catalog
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._ttl_seconds = ttl_seconds
        self._max_drafts = max_drafts
        self._by_item = {item.item_id: item for item in catalog.menu_items}
        self._prices = _price_index(catalog.item_prices)
        self._modifiers = _modifier_index(catalog.modifiers)
        self._offered = _offer_index(catalog.modifiers)
        self._groups = _group_index(catalog.modifiers)
        self._stores = {store.store_id: store for store in catalog.stores}
        # Hashes every table, so it is computed once here rather than per draft.
        self._content_version = catalog.content_version()
        self._lock = threading.Lock()
        self._drafts: dict[str, Draft] = {}

    @property
    def catalog(self) -> MenuCatalog:
        """The catalogue in force, for an ops surface that wants to report it."""
        return self._catalog

    @property
    def content_version(self) -> str:
        """The catalogue build every draft in this store was composed against."""
        return self._content_version

    def propose(
        self,
        demo_id: str,
        items: Sequence[Mapping[str, Any]],
        *,
        restaurant_id: int | None = None,
        order_type: OrderType | str = OrderType.PICKUP,
    ) -> Draft:
        """Mint a priced draft for ``demo_id``, or refuse it.

        Writes nothing anybody can be charged for and requires no confirmation:
        a draft is a quote. What it does do is fix, at proposal time, everything
        ``place_order`` will later be held to.

        Args:
            demo_id: The visitor the draft belongs to, resolved from the session
                by the app.
            items: ``{"item_id": ..., "quantity": ..., "selections": [...]}``
                mappings, as the model supplies them. A selection is
                ``{"modifier_item_id": ..., "portion": ...}``; its group is read
                off the catalogue rather than taken from here.
            restaurant_id: Whose prices to quote. Defaults to the catalogue's
                reference restaurant.
            order_type: Which price column to quote in.

        Returns:
            The unconfirmed draft.

        Raises:
            DraftRejectedError: Naming the first rule the order broke.
            ValueError: If ``demo_id`` is empty or ``order_type`` is not one of
                the two published columns. Both are wiring bugs -- neither
                argument comes from the model -- so neither is a rejection the
                visitor is told about.
        """
        return self._mint(demo_id, items, restaurant_id, order_type, supersedes=None)

    def revise(
        self,
        demo_id: str,
        draft_id: str,
        items: Sequence[Mapping[str, Any]],
        *,
        restaurant_id: int | None = None,
        order_type: OrderType | str | None = None,
    ) -> Draft:
        """Re-propose a draft the visitor edited, and retire the one it replaces.

        PRD T3's card is editable in place, and ``docs/action-surface.md``
        section 6 says why there is no write tool behind that: modification is
        possible only before submission, which is what a draft is. Editing
        therefore mints a *new* draft rather than mutating one -- so an edited
        card is unconfirmed again by construction, and there is no window in
        which a confirmation granted for one basket applies to another.

        The superseded draft is discarded rather than left to expire. A stale id
        that is still confirmable is a second, differently-priced order sitting
        in a browser tab.

        Args:
            demo_id: The visitor. The draft must be theirs.
            draft_id: The draft being edited.
            items: The edited lines, in the same shape :meth:`propose` takes.
            restaurant_id: Whose prices to quote. Defaults to the superseded
                draft's, so an edit does not silently move store.
            order_type: Which price column. Defaults to the superseded draft's.

        Returns:
            The new, unconfirmed draft.

        Raises:
            DraftRejectedError: ``DRAFT_NOT_FOUND`` for an id that is not this
                visitor's, ``DRAFT_EXPIRED`` for one that has aged out, or
                whichever composition rule the edited order broke.
        """
        previous = self._require_live(demo_id, draft_id)
        revised = self._mint(
            demo_id,
            items,
            previous.restaurant_id if restaurant_id is None else restaurant_id,
            previous.order_type if order_type is None else order_type,
            supersedes=previous.draft_id,
        )
        # Only after the new draft exists: a rejected edit must leave the
        # visitor the card they were already looking at.
        self.discard(demo_id, draft_id)
        return revised

    def confirm(self, demo_id: str, draft_id: str) -> Draft:
        """Mark a draft confirmed on behalf of ``demo_id``.

        **The launch gate.** Called by the request handler when a request
        carrying the visitor's session cookie says the Confirm button was
        pressed, and by nothing else. There is deliberately no tool that reaches
        it and no argument to it the model can supply: an agent cannot confirm
        its own draft, so an agent that skips the confirmation step produces a
        rejected ``place_order`` and an eval failure rather than an order.

        Args:
            demo_id: The visitor the confirming request resolved to.
            draft_id: The draft they were shown.

        Returns:
            The confirmed draft.

        Raises:
            DraftRejectedError: ``DRAFT_NOT_FOUND`` if there is no such draft for
                this visitor -- including the case where it belongs to somebody
                else -- or ``DRAFT_EXPIRED`` if it has aged out.
        """
        with self._lock:
            draft = self._live_locked(demo_id, draft_id)
            confirmed = replace(draft, confirmed=True)
            self._drafts[draft_id] = confirmed
            return confirmed

    def claim(self, demo_id: str, draft_id: str) -> Draft:
        """Return a confirmed draft for writing, and retire it.

        What the ops API calls before it writes anything: section 7.1 rule 11,
        in one place, so that the check is not re-implemented per write tool.
        The draft is removed as it is handed over, so one draft becomes at most
        one order however many times the call is retried.

        Args:
            demo_id: The visitor the writing request resolved to.
            draft_id: The draft to place.

        Returns:
            The confirmed, unexpired draft.

        Raises:
            DraftRejectedError: ``DRAFT_NOT_FOUND``, ``DRAFT_EXPIRED``, or
                ``DRAFT_NOT_CONFIRMED``. The last is the launch gate, and it is a
                refusal rather than a warning.
        """
        with self._lock:
            draft = self._live_locked(demo_id, draft_id)
            if not draft.confirmed:
                raise DraftRejectedError(
                    RejectionCode.DRAFT_NOT_CONFIRMED,
                    f"draft {draft_id!r} has not been confirmed by the visitor",
                )
            del self._drafts[draft_id]
            return draft

    def get(self, demo_id: str, draft_id: str) -> Draft | None:
        """Return a live draft belonging to ``demo_id``, or ``None``.

        The non-raising read, for a caller rendering a card rather than
        enforcing a rule.
        """
        with self._lock:
            try:
                return self._live_locked(demo_id, draft_id)
            except DraftRejectedError:
                return None

    def card(self, draft: Draft) -> Mapping[str, Any]:
        """Render ``draft`` as a confirmation card, store details included."""
        return draft.as_card(self._stores.get(draft.restaurant_id))

    def discard(self, demo_id: str, draft_id: str) -> bool:
        """Drop a draft. Returns whether there was one of ``demo_id``'s to drop."""
        with self._lock:
            draft = self._drafts.get(draft_id)
            if draft is None or draft.demo_id != demo_id:
                return False
            del self._drafts[draft_id]
            return True

    def __len__(self) -> int:
        """How many drafts are held, expired ones included until they are swept."""
        with self._lock:
            return len(self._drafts)

    # --- minting ----------------------------------------------------------

    def _mint(
        self,
        demo_id: str,
        items: Sequence[Mapping[str, Any]],
        restaurant_id: int | None,
        order_type: OrderType | str,
        *,
        supersedes: str | None,
    ) -> Draft:
        """Validate, price and store one draft. The body of :meth:`propose`."""
        if not demo_id:
            raise ValueError("a draft has to belong to a visitor")
        resolved_type = OrderType(order_type)
        restaurant = (
            self._catalog.reference_restaurant_id
            if restaurant_id is None
            else restaurant_id
        )
        self._require_priced(restaurant)

        lines: list[DraftLine] = []
        harvested: list[datetime] = []
        for entry in _entries(items):
            line, dates = self._line(entry, restaurant, resolved_type)
            lines.append(line)
            harvested.extend(dates)
        if not lines:
            raise DraftRejectedError(
                RejectionCode.EMPTY_ORDER, "an order needs at least one item"
            )

        now = self._clock.now()
        draft = Draft(
            draft_id=f"draft-{secrets.token_urlsafe(9)}",
            demo_id=demo_id,
            restaurant_id=restaurant,
            order_type=resolved_type,
            lines=tuple(lines),
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
            expires_after=self._clock.monotonic() + self._ttl_seconds,
            priced_at=max(harvested) if harvested else None,
            content_version=self._content_version,
            supersedes=supersedes,
        )
        with self._lock:
            self._sweep_locked()
            self._drafts[draft.draft_id] = draft
        return draft

    def _line(
        self, entry: Any, restaurant_id: int, order_type: OrderType
    ) -> tuple[DraftLine, list[datetime]]:
        """Compose and price one line, in the order section 7.1 applies its rules.

        Args:
            entry: One element of what the model supplied, which is JSON of
                whatever shape it emitted and so is checked rather than trusted.
            restaurant_id: Whose prices this draft quotes.
            order_type: Which price column it quotes.

        Returns:
            The line, and the ``harvested_at`` of every price row it read.
        """
        if not isinstance(entry, Mapping):
            raise DraftRejectedError(
                RejectionCode.ITEM_NOT_ORDERABLE,
                f"{entry!r} does not name an item_id",
            )
        item = self._orderable(str(entry.get("item_id", "")).strip())
        price = self._sellable(item.item_id, restaurant_id, order_type)
        quantity = _quantity(item, entry.get("quantity", 1))
        selections, dates = self._selections(item, entry, restaurant_id, order_type)
        return (
            DraftLine(
                item_id=item.item_id,
                name=item.name,
                quantity=quantity,
                unit_price=_column(price, order_type),
                selections=selections,
            ),
            [price.harvested_at, *dates],
        )

    def _orderable(self, item_id: str) -> MenuItem:
        """Rule 2: a real row, with a category, or a refusal."""
        item = self._by_item.get(item_id)
        if item is None or item.category is None:
            raise DraftRejectedError(
                RejectionCode.ITEM_NOT_ORDERABLE,
                f"{item_id or '(nothing)'!r} is not orderable on its own",
            )
        return item

    def _sellable(
        self, item_id: str, restaurant_id: int, order_type: OrderType
    ) -> ItemPrice:
        """Rules 3 and 10, for an orderable item: priced, in stock, deliverable."""
        price = self._prices.get((restaurant_id, item_id))
        if price is None or not price.is_available:
            raise DraftRejectedError(
                RejectionCode.ITEM_UNAVAILABLE_AT_STORE,
                f"{item_id!r} was not available at restaurant {restaurant_id} "
                "when the menu was read",
            )
        if order_type is OrderType.DELIVERY and not price.eligible_for_delivery:
            raise DraftRejectedError(
                RejectionCode.NOT_ELIGIBLE_FOR_DELIVERY,
                f"{item_id!r} cannot be delivered; it can be ordered for pickup",
            )
        return price

    def _selections(
        self,
        item: MenuItem,
        entry: Mapping[str, Any],
        restaurant_id: int,
        order_type: OrderType,
    ) -> tuple[tuple[Selection, ...], list[datetime]]:
        """Resolve every modifier on one line: rules 6, 7, 8 and 10.

        The caller's selections are resolved first, then the published defaults
        it did not name are added, and the group bounds are checked over both --
        a default rice fills the rice slot exactly as a chosen one does.
        """
        chosen: list[Selection] = []
        dates: list[datetime] = []
        seen: set[str] = set()
        for selection in _entries(entry.get("selections")):
            if not isinstance(selection, Mapping):
                raise DraftRejectedError(
                    RejectionCode.MODIFIER_NOT_OFFERED,
                    f"{item.item_id} takes selections naming a modifier_item_id",
                )
            modifier = self._offers(item, str(selection.get("modifier_item_id", "")))
            if modifier.modifier_item_id in seen:
                raise DraftRejectedError(
                    RejectionCode.SLOT_OVERFILLED,
                    f"{modifier.name} is on {item.name} twice",
                )
            seen.add(modifier.modifier_item_id)
            resolved, harvested = self._priced_selection(
                modifier,
                _portion(modifier, selection.get("portion")),
                restaurant_id,
                order_type,
            )
            chosen.append(resolved)
            dates.extend(harvested)

        for modifier in self._offered.get(item.item_id, ()):
            if modifier.is_default and modifier.modifier_item_id not in seen:
                resolved, harvested = self._priced_selection(
                    modifier, None, restaurant_id, order_type
                )
                chosen.append(resolved)
                dates.extend(harvested)

        self._require_groups(item, chosen)
        return tuple(chosen), dates

    def _offers(self, item: MenuItem, modifier_item_id: str) -> Modifier:
        """Rule 7: ``(item_id, modifier_item_id)`` is a published pairing."""
        modifier = self._modifiers.get((item.item_id, modifier_item_id.strip()))
        if modifier is None:
            raise DraftRejectedError(
                RejectionCode.MODIFIER_NOT_OFFERED,
                f"{modifier_item_id or '(nothing)'!r} is not offered on {item.name}",
            )
        return modifier

    def _priced_selection(
        self,
        modifier: Modifier,
        portion: str | None,
        restaurant_id: int,
        order_type: OrderType,
    ) -> tuple[Selection, list[datetime]]:
        """Price one modifier, and check it can travel the way the draft does."""
        price = self._prices.get((restaurant_id, modifier.modifier_item_id))
        if price is None:
            # Included rather than unavailable -- see `_FREE`.
            return (
                Selection(
                    modifier_item_id=modifier.modifier_item_id,
                    name=modifier.name,
                    group_name=modifier.group_name,
                    portion=portion,
                    unit_price=_FREE,
                    is_default=modifier.is_default,
                ),
                [],
            )
        if not price.is_available:
            raise DraftRejectedError(
                RejectionCode.ITEM_UNAVAILABLE_AT_STORE,
                f"{modifier.name} was not available at restaurant "
                f"{restaurant_id} when the menu was read",
            )
        if order_type is OrderType.DELIVERY and not price.eligible_for_delivery:
            raise DraftRejectedError(
                RejectionCode.NOT_ELIGIBLE_FOR_DELIVERY,
                f"{modifier.name} cannot be delivered",
            )
        return (
            Selection(
                modifier_item_id=modifier.modifier_item_id,
                name=modifier.name,
                group_name=modifier.group_name,
                portion=portion,
                unit_price=_column(price, order_type),
                is_default=modifier.is_default,
            ),
            [price.harvested_at],
        )

    def _require_groups(self, item: MenuItem, chosen: Sequence[Selection]) -> None:
        """Rule 6: every published group is filled within its published bounds.

        A missing rice choice on a bowl is a rejection and not a default, which
        is section 7.1's own example.
        """
        for group, (minimum, maximum) in self._groups.get(item.item_id, {}).items():
            filled = sum(1 for selection in chosen if selection.group_name == group)
            if minimum is not None and filled < minimum:
                raise DraftRejectedError(
                    RejectionCode.REQUIRED_SLOT_EMPTY,
                    f"{item.name} needs {minimum} from {group}; {filled} were chosen",
                )
            if maximum is not None and filled > maximum:
                raise DraftRejectedError(
                    RejectionCode.SLOT_OVERFILLED,
                    f"{item.name} takes at most {maximum} from {group}; "
                    f"{filled} were chosen",
                )

    def _require_priced(self, restaurant_id: int) -> None:
        """Rule 1, as the catalogue can decide it: is this restaurant priced?

        The harvest prices as many restaurants as it was asked to and the
        catalogue publishes which -- so a draft at any other restaurant cannot be
        priced at all. Quoting the reference restaurant's prices instead would be
        a total that looks right and is not.
        """
        if restaurant_id not in self._catalog.restaurant_ids:
            raise DraftRejectedError(
                RejectionCode.STORE_NOT_PRICED,
                f"restaurant {restaurant_id} has no published prices in this catalogue",
            )

    # --- the drafts themselves --------------------------------------------

    def _require_live(self, demo_id: str, draft_id: str) -> Draft:
        with self._lock:
            return self._live_locked(demo_id, draft_id)

    def _live_locked(self, demo_id: str, draft_id: str) -> Draft:
        """Return this visitor's unexpired draft, or raise the right refusal.

        A draft belonging to somebody else is a ``DRAFT_NOT_FOUND`` and not a
        forbidden: the answer to a well-formed id from the wrong session must be
        the same as the answer to an id that never existed.
        """
        draft = self._drafts.get(draft_id)
        if draft is None or draft.demo_id != demo_id:
            raise DraftRejectedError(
                RejectionCode.DRAFT_NOT_FOUND,
                f"no draft {draft_id!r} is waiting on this conversation",
            )
        if draft.expires_after <= self._clock.monotonic():
            del self._drafts[draft_id]
            raise DraftRejectedError(
                RejectionCode.DRAFT_EXPIRED,
                f"draft {draft_id!r} has expired; propose it again",
            )
        return draft

    def _sweep_locked(self) -> None:
        """Drop expired drafts, then the oldest, until the store is under its cap."""
        now = self._clock.monotonic()
        self._drafts = {
            draft_id: draft
            for draft_id, draft in self._drafts.items()
            if draft.expires_after > now
        }
        while len(self._drafts) >= self._max_drafts:
            # Insertion-ordered, so this is the least recently minted.
            self._drafts.pop(next(iter(self._drafts)))


def _money(value: Decimal) -> str:
    """Money as a card shows it: a string, in cents, never a float.

    A card that went through a float on the way to a browser is a card that can
    show a total nobody computed. The cents are a rendering: the published price
    of a bag of chips is ``2.3`` and the card says ``2.30``, which is the same
    money spelled the way money is spelled.
    """
    return str(value.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _entries(raw: Any) -> Sequence[Any]:
    """Read a list out of model-supplied JSON, or nothing.

    A tool argument is whatever the model emitted. Anything that is not a list
    of things is no lines at all, which composes into ``EMPTY_ORDER`` -- and a
    list of the wrong things is refused element by element, so the rejection
    names what was wrong with it rather than saying the order was empty.
    """
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        return raw
    return ()


def _column(price: ItemPrice, order_type: OrderType) -> Decimal:
    """The published price for one order type. Never a markup on the other."""
    if order_type is OrderType.DELIVERY:
        return price.unit_delivery_price
    return price.unit_price


def _quantity(item: MenuItem, raw: Any) -> int:
    """Rule 4, flattened to :data:`_MAX_QUANTITY`. See its docstring."""
    cap = 1 if item.category == ENTREE_CATEGORY else _MAX_QUANTITY
    try:
        quantity = 1 if raw is None else int(str(raw))
    except ValueError:
        quantity = 0
    if not 1 <= quantity <= cap:
        raise DraftRejectedError(
            RejectionCode.QUANTITY_EXCEEDS_MAX,
            f"quantity for {item.item_id} must be between 1 and {cap}",
        )
    return quantity


def _portion(modifier: Modifier, raw: Any) -> str | None:
    """Rule 8: a portion word published for *this* pairing, or a refusal.

    Matched case-insensitively against the published options and returned in the
    published spelling, so a card never shows a word the menu does not use.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    wanted = str(raw).strip().casefold()
    for published in modifier.portion_options:
        if published.casefold() == wanted:
            return published
    offered = ", ".join(modifier.portion_options) or "none"
    raise DraftRejectedError(
        RejectionCode.PORTION_NOT_OFFERED,
        f"{raw!r} is not a portion offered for {modifier.name}; "
        f"published options: {offered}",
    )


def _store_block(restaurant_id: int, store: Store | None) -> Mapping[str, Any]:
    """The store as a card shows it. ``None`` where the catalogue has no row."""
    if store is None:
        return {"restaurant_id": restaurant_id, "name": None, "address": None}
    address = ", ".join(
        part
        for part in (
            store.street_address,
            store.city,
            f"{store.region or ''} {store.postal_code or ''}".strip(),
        )
        if part
    )
    return {
        "restaurant_id": restaurant_id,
        "name": store.name,
        "address": address or None,
    }


def _price_index(prices: Iterable[ItemPrice]) -> Mapping[tuple[int, str], ItemPrice]:
    """Map ``(restaurant, item)`` to its published price row."""
    return {(row.restaurant_id, row.item_id): row for row in prices}


def _modifier_index(
    modifiers: Iterable[Modifier],
) -> Mapping[tuple[str, str], Modifier]:
    """Map ``(item, modifier item)`` to the row that joins them.

    The same ingredient on a different item is a different modifier -- a
    different group, a different portion allowance -- so the pair is the key.
    """
    return {(row.item_id, row.modifier_item_id): row for row in modifiers}


def _offer_index(modifiers: Iterable[Modifier]) -> Mapping[str, tuple[Modifier, ...]]:
    """Map an item to every modifier published for it, in catalogue order."""
    offered: dict[str, list[Modifier]] = {}
    for row in modifiers:
        offered.setdefault(row.item_id, []).append(row)
    return {item_id: tuple(rows) for item_id, rows in offered.items()}


def _group_index(
    modifiers: Iterable[Modifier],
) -> Mapping[str, Mapping[str, tuple[int | None, int | None]]]:
    """Map an item to the published bounds of each of its content groups.

    The catalogue publishes ``min_quantity`` and ``max_quantity`` on the
    modifier rather than on the group, because that is how the menu publishes
    them, and every member of a published group carries the same pair -- one rice
    from ``RiceContentGroup``, one bean from ``BeansContentGroup``. Where two
    members disagreed the strictest published floor and the widest published
    ceiling win, which refuses an under-filled group without inventing a limit
    the menu never stated. A modifier the menu leaves ungrouped constrains
    nothing.
    """
    bounds: dict[str, dict[str, tuple[int | None, int | None]]] = {}
    for row in modifiers:
        if row.group_name is None:
            continue
        groups = bounds.setdefault(row.item_id, {})
        minimum, maximum = groups.get(row.group_name, (None, None))
        groups[row.group_name] = (
            _larger(minimum, row.min_quantity),
            _larger(maximum, row.max_quantity),
        )
    return bounds


def _larger(current: int | None, published: int | None) -> int | None:
    """Fold one published bound into the running one, ignoring the unpublished."""
    if published is None:
        return current
    if current is None:
        return published
    return max(current, published)
