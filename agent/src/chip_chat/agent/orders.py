"""Order drafts, and the confirmation rule the agent is not allowed to talk past.

RFC-001 section 06 puts confirmation in the ops API rather than in the prompt,
and the reason is a launch gate: a write the visitor never confirmed is a
failure whatever the model said about it. That property is cheap to hold and
expensive to retrofit, so it is held here from the first commit even though
everything either side of it is hardcoded.

The rule, in one sentence: :meth:`OrderDesk.place` refuses any draft that is not
marked confirmed, and the only thing that can mark one confirmed is a *request*
carrying the session -- never a tool argument, never a model output. The widget
renders a confirm button; pressing it is what sets the flag.

**This module stands in for the ops API**, which is an Azure Function in the real
design and holds the twelve validation rules of ``docs/action-surface.md`` §7.1.
What is here is the confirmation check and the arithmetic, in process, against
three hardcoded items. When the real ops API lands (#60) this file goes away and
the rule moves with it -- the rule is the part worth keeping.

Drafts are held in memory and expire. A single-replica demo can afford that; a
second replica cannot, which is the same honest limitation
:class:`~chip_chat.api.ledger.BudgetLedger` carries and for the same reason.
"""

import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from chip_chat.agent.desk import ActionOutcome, OrderableMenu
from chip_chat.agent.hardcoded import MENU, SIMULATION_NOTICE, STORE, MenuItem
from chip_chat.otel import OpsAction

__all__ = [
    "DEFAULT_DRAFT_TTL_SECONDS",
    "HARDCODED_MENU",
    "Draft",
    "DraftLine",
    "OrderDesk",
    "OrderRejectedError",
    "Receipt",
    "RejectionCode",
]

DEFAULT_DRAFT_TTL_SECONDS = 900.0
"""How long a draft stays placeable. Fifteen minutes; INVENTED, as §10 says."""

_MAX_QUANTITY = 5
"""Per ``docs/action-surface.md`` §7.1 rule 4, flattened to one number here."""


HARDCODED_MENU: OrderableMenu = OrderableMenu(
    item_ids=tuple(sorted(MENU)),
    described=" | ".join(
        f"{item.item_id} = {item.name} (${item.unit_price})"
        for item in sorted(MENU.values(), key=lambda row: row.item_id)
    ),
)
"""What the week-one desk can price, as ``propose_order``'s schema wants it.

A module constant rather than something :meth:`OrderDesk.orderable_menu` builds
per call, because :data:`chip_chat.agent.tools.TOOL_SCHEMAS` needs it at import
and constructing a whole desk to read three names would be a strange way to ask.

No required groups, because the three hardcoded items have no modifier structure
at all -- which is exactly why the week-one slice got away with an enum and
nothing else, and why the real catalogue could not. :mod:`chip_chat.agent.desk`
records what that cost when it was found out.
"""


class RejectionCode:
    """The typed rejections this slice can produce.

    A subset of the twelve in ``docs/action-surface.md`` §7.1 -- the ones the
    three hardcoded items can actually reach. They are the published spellings
    rather than new ones, so the eval that groups on them later does not have to
    learn two vocabularies.
    """

    ITEM_NOT_ORDERABLE = "ITEM_NOT_ORDERABLE"
    ACTION_NOT_AVAILABLE = "ACTION_NOT_AVAILABLE"
    QUANTITY_EXCEEDS_MAX = "QUANTITY_EXCEEDS_MAX"
    DRAFT_NOT_FOUND = "DRAFT_NOT_FOUND"
    DRAFT_NOT_CONFIRMED = "DRAFT_NOT_CONFIRMED"
    DRAFT_EXPIRED = "DRAFT_EXPIRED"
    EMPTY_ORDER = "EMPTY_ORDER"


class OrderRejectedError(Exception):
    """A draft or a write was refused, naming the rule that refused it.

    Never repaired into validity: RFC-001 forbids the agent rounding a bad draft
    into a good one, so the model is told what was wrong and asks the visitor.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_result(self) -> Mapping[str, str]:
        """The rejection as the model sees it, on the tool span and in the reply."""
        return {"rejected": self.code, "detail": self.message}


@dataclass(frozen=True, slots=True)
class DraftLine:
    """One line of a draft: an item and how many of it."""

    item: MenuItem
    quantity: int

    @property
    def total(self) -> Decimal:
        return self.item.unit_price * self.quantity

    def as_dict(self) -> Mapping[str, object]:
        return {
            "item_id": self.item.item_id,
            "name": self.item.name,
            "quantity": self.quantity,
            "unit_price": str(self.item.unit_price),
            "line_total": str(self.total),
        }


@dataclass(frozen=True, slots=True)
class Draft:
    """A proposed order, waiting for the visitor to confirm it."""

    draft_id: str
    session_id: str
    lines: tuple[DraftLine, ...]
    expires_at: float
    confirmed: bool = False

    @property
    def total(self) -> Decimal:
        """What the card shows, rounded to cents once at the end."""
        return sum((line.total for line in self.lines), Decimal("0")).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    def as_card(self) -> Mapping[str, object]:
        """The confirmation card, which is what the widget renders."""
        return {
            "draft_id": self.draft_id,
            "store": {
                "restaurant_id": STORE.restaurant_id,
                "name": STORE.name,
                "address": STORE.address,
            },
            "order_type": "pickup",
            "lines": [line.as_dict() for line in self.lines],
            "total": str(self.total),
            "requires_confirmation": True,
            "notice": SIMULATION_NOTICE,
        }


@dataclass(frozen=True, slots=True)
class Receipt:
    """What a placed order returns. Simulated, and it says so."""

    order_id: str
    draft: Draft

    def as_dict(self) -> Mapping[str, object]:
        card = dict(self.draft.as_card())
        card.pop("requires_confirmation", None)
        return {"order_id": self.order_id, **card}


class OrderDesk:
    """Mints drafts, records confirmations, and places what was confirmed.

    Thread-safe, because a Container App serves concurrent requests and a draft
    dictionary mutated from two of them is the kind of bug that only appears
    once somebody is watching.
    """

    __slots__ = ("_drafts", "_lock", "_monotonic", "_ttl_seconds")

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_DRAFT_TTL_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        """Initialise an empty desk.

        Args:
            ttl_seconds: How long a draft stays placeable.
            monotonic: Source of monotonic time, for tests that need a draft to
                expire without waiting a quarter of an hour.
        """
        self._ttl_seconds = ttl_seconds
        self._monotonic: Callable[[], float] = (
            monotonic if monotonic is not None else time.monotonic
        )
        self._lock = threading.Lock()
        self._drafts: dict[str, Draft] = {}

    @property
    def writes_here(self) -> bool:
        """True: this desk *is* the write, so the agent emits ``ops.<action>``.

        :mod:`chip_chat.agent.desk` gives the argument. The counterpart is the
        deployed ops API, where the span is opened in the other process as a
        child of the tool span this one is called inside.
        """
        return True

    def offers_every_write(self) -> bool:
        """False. Three hardcoded items are not an account to cancel against.

        ``cancel_order``, ``redeem_points`` and ``update_preferences`` name rows
        in ``CHIP_CHAT.ACCOUNTS`` -- an order, a published reward, a visitor's
        stored preferences -- and there is no honest stand-in for any of them
        here. A hardcoded redemption would be a points balance moving in a
        fixture, reported to a visitor as their own, which is the plausible
        number PRD A4 forbids in a different costume.
        """
        return False

    def orderable_menu(self) -> OrderableMenu:
        """The three items this desk can price. See :data:`HARDCODED_MENU`."""
        return HARDCODED_MENU

    def available(self) -> bool:
        """True. An in-process dictionary is not a service that can be down."""
        return True

    def act(
        self, session_id: str, action: OpsAction, arguments: Mapping[str, Any]
    ) -> ActionOutcome:
        """Refuse the three writes this desk has no rows for.

        Unreachable on any deployment, because
        :func:`chip_chat.agent.tools.offered_tools` asks
        :meth:`offers_every_write` and does not offer a tool this desk would
        only refuse. It exists so that the refusal is a typed rejection the
        model could read rather than an ``AttributeError`` -- the same reason
        :func:`chip_chat.agent.tools._not_implemented` exists one layer up.

        Raises:
            OrderRejectedError: Always.
        """
        del session_id, arguments
        raise OrderRejectedError(
            RejectionCode.ACTION_NOT_AVAILABLE,
            f"{action.value} needs the ops API, and this deployment runs the "
            "week-one order desk against three hardcoded items",
        )

    def propose(self, session_id: str, items: Sequence[Mapping[str, object]]) -> Draft:
        """Mint a draft for ``items``, or refuse it.

        Args:
            session_id: The conversation the draft belongs to. Drafts are never
                placeable from another session, which is the same "a well-formed
                id belonging to someone else is a not-found" rule the real ops
                API applies.
            items: ``{"item_id": ..., "quantity": ...}`` mappings, as the model
                supplies them.

        Returns:
            The unconfirmed draft.

        Raises:
            OrderRejectedError: If the order is empty, names something not on the
                menu, or asks for a quantity above the cap.
        """
        lines = tuple(self._line(entry) for entry in items)
        if not lines:
            raise OrderRejectedError(
                RejectionCode.EMPTY_ORDER, "an order needs at least one item"
            )
        draft = Draft(
            draft_id=f"draft-{uuid.uuid4().hex[:8]}",
            session_id=session_id,
            lines=lines,
            expires_at=self._monotonic() + self._ttl_seconds,
        )
        with self._lock:
            self._sweep_locked()
            self._drafts[draft.draft_id] = draft
        return draft

    def confirm(self, session_id: str, draft_id: str) -> Draft | None:
        """Mark a draft confirmed on behalf of ``session_id``.

        Called by the request handler when the visitor presses the confirm
        button, and by nothing else. In particular there is no tool that reaches
        it: the agent cannot confirm its own draft, which is the whole point.

        Args:
            session_id: The session the confirming request carried.
            draft_id: The draft the visitor was shown.

        Returns:
            The confirmed draft, or ``None`` if there is no such live draft for
            this session.
        """
        with self._lock:
            draft = self._live_locked(session_id, draft_id)
            if draft is None:
                return None
            confirmed = Draft(
                draft_id=draft.draft_id,
                session_id=draft.session_id,
                lines=draft.lines,
                expires_at=draft.expires_at,
                confirmed=True,
            )
            self._drafts[draft_id] = confirmed
            return confirmed

    def place(self, session_id: str, draft_id: str) -> Receipt:
        """Place a confirmed draft and return its receipt.

        Args:
            session_id: The session the placing request carried.
            draft_id: The draft to place.

        Returns:
            The simulated receipt.

        Raises:
            OrderRejectedError: If the draft is unknown, expired, or was never
                confirmed by the visitor. ``DRAFT_NOT_CONFIRMED`` is the launch
                gate; it is a refusal and never a warning.
        """
        with self._lock:
            draft = self._drafts.get(draft_id)
            if draft is None or draft.session_id != session_id:
                raise OrderRejectedError(
                    RejectionCode.DRAFT_NOT_FOUND,
                    f"no draft {draft_id!r} is waiting on this conversation",
                )
            if draft.expires_at <= self._monotonic():
                del self._drafts[draft_id]
                raise OrderRejectedError(
                    RejectionCode.DRAFT_EXPIRED,
                    f"draft {draft_id!r} has expired; propose it again",
                )
            if not draft.confirmed:
                raise OrderRejectedError(
                    RejectionCode.DRAFT_NOT_CONFIRMED,
                    f"draft {draft_id!r} has not been confirmed by the visitor",
                )
            del self._drafts[draft_id]
        return Receipt(order_id=f"CC-{uuid.uuid4().hex[:6].upper()}", draft=draft)

    def get(self, session_id: str, draft_id: str) -> Draft | None:
        """Return a live draft belonging to ``session_id``, if there is one."""
        with self._lock:
            return self._live_locked(session_id, draft_id)

    def _live_locked(self, session_id: str, draft_id: str) -> Draft | None:
        draft = self._drafts.get(draft_id)
        if draft is None or draft.session_id != session_id:
            return None
        if draft.expires_at <= self._monotonic():
            del self._drafts[draft_id]
            return None
        return draft

    def _sweep_locked(self) -> None:
        """Drop drafts nobody placed. Cheap: there are never many."""
        now = self._monotonic()
        self._drafts = {
            draft_id: draft
            for draft_id, draft in self._drafts.items()
            if draft.expires_at > now
        }

    @staticmethod
    def _line(entry: Mapping[str, object]) -> DraftLine:
        item_id = str(entry.get("item_id", "")).strip()
        item = MENU.get(item_id)
        if item is None:
            raise OrderRejectedError(
                RejectionCode.ITEM_NOT_ORDERABLE,
                f"{item_id or '(nothing)'!r} is not on the menu. "
                f"Orderable items: {', '.join(sorted(MENU))}.",
            )
        raw_quantity = entry.get("quantity", 1)
        try:
            quantity = 1 if raw_quantity is None else int(str(raw_quantity))
        except ValueError:
            quantity = 0
        if not 1 <= quantity <= _MAX_QUANTITY:
            raise OrderRejectedError(
                RejectionCode.QUANTITY_EXCEEDS_MAX,
                f"quantity for {item_id} must be between 1 and {_MAX_QUANTITY}",
            )
        return DraftLine(item=item, quantity=quantity)
