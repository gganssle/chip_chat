"""The action lane, wired: the app's records on one side, the ops API on the other.

:class:`chip_chat.agent.desk.Desk` is the shape the five action tools need and
:class:`chip_chat.agent.orders.OrderDesk` is the week-one answer to it -- an
in-process dictionary of three hardcoded items that simulates a write. This is
the deployed answer, and the whole of what it adds is that a write here is a
write: :class:`~chip_chat.api.drafts.DraftStore` prices against the published
catalogue, :class:`~chip_chat.api.confirmations.ConfirmationLedger` holds the
flag for the other three, and :class:`~chip_chat.api.opsclient.OpsClient` posts
to the only service in the estate with the Snowflake write role.

**The confirmation flag stays here and the write happens there, and the join
between them is a signature rather than a shared table.** That is the design
decision this module exists to carry out; ``docs/decisions/confirmation-grants.md``
argues it and :mod:`chip_chat.api.grants` implements it. In one paragraph:

    A draft minted in this process is invisible to the ops API, which is a
    different process. So this desk claims the confirmed record *here* -- the
    same :meth:`DraftStore.claim` that has always been the gate, retiring the
    draft as it hands it over -- and then signs what it claimed with a key
    derived from the secret both tiers already share. The ops API verifies the
    signature and writes what is inside it. It never sees a draft store and it
    never trusts a field on the request: the arguments the procedure is called
    with are inside the signature, so there is nothing on the wire a model could
    alter between the card the visitor read and the row that gets written.

**Identity is resolved here and nowhere else.** Every method takes a
``session_id``, and :meth:`_visitor` is the single place it becomes a
``demo_id`` -- by asking :class:`~chip_chat.api.visitors.VisitorDesk`, which
reads the store the session cookie is bound in. There is no argument on this
class through which a caller could name somebody, which is RFC-001 §05 carried
into the one object that reaches a write.

**Everything it cannot do is a typed rejection.** The action tools' contract is
that a refusal is a *result* the model can read and act on rather than an
exception that ends the turn, so this class raises
:class:`~chip_chat.agent.orders.OrderRejectedError` -- the exception
:func:`chip_chat.agent.tools.dispatch` already catches and renders -- rather than
inventing a second vocabulary for the same idea. The codes it raises are the
published ones from ``docs/action-surface.md`` §7 wherever there is one.
"""

import logging
import os
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from chip_chat.agent.desk import ActionOutcome, OrderableMenu
from chip_chat.agent.orders import OrderRejectedError
from chip_chat.api.confirmations import (
    Confirmation,
    ConfirmationLedger,
    ConfirmationRejectedError,
)
from chip_chat.api.drafts import Draft, DraftRejectedError, DraftStore
from chip_chat.api.grants import GrantSigner
from chip_chat.api.ops import (
    OPS_UNAVAILABLE_MESSAGE,
    OpsRejectedError,
    OpsUnavailableError,
    offer_cancellation,
    offer_preferences,
    offer_redemption,
    preferences_reference,
    unavailable_card,
)
from chip_chat.api.opsclient import OpsClient
from chip_chat.api.visitors import VisitorDesk, VisitorSession
from chip_chat.catalog import MenuCatalog
from chip_chat.otel import OpsAction

__all__ = ["NO_VISITOR", "OPS_DECLINED", "OpsDesk", "RewardLookup"]

_log = logging.getLogger("chip_chat.api.orderdesk")

NO_VISITOR: Final = "SESSION_NOT_BOUND"
"""What a write is refused with when the session has no synthetic customer.

Not a rejection the model can do anything about and not one a visitor caused: it
means the roster assigned nobody, which is the empty-account failure PRD §06 says
loses the demo, and it is refused rather than written for nobody. The read lanes
already decline for the same reason and say so in the same words.
"""

OPS_DECLINED: Final = "OPS_UNAVAILABLE"
"""The code a refused-because-the-service-is-down write carries.

Distinct from every rejection in ``docs/action-surface.md`` §7, because those are
answers about the *order* and this one is an answer about the *service*. RFC-001
§10 gives it copy and :data:`~chip_chat.api.ops.OPS_UNAVAILABLE_MESSAGE` is that
copy, so the model is handed the sentence rather than asked to compose one.
"""

RewardLookup = Callable[[str, str], tuple[str, int] | None]
"""How the desk finds what a reward is called and what it costs.

``(session_id, reward_id) -> (name, point_cost)``, or ``None`` where the reward
is not in this visitor's published catalogue. A callable rather than a client,
for the reason :mod:`chip_chat.agent.lanes` gives about every other backing
service: the account lane already reads ``CHIP_CHAT.CATALOGUE.rewards`` through
#44's pool for ``get_points_balance``, and a second reader here would be a second
place a connection is resolved.

**The cost matters and is not decoration.** ``redeem_points`` sends the procedure
the point cost that was *on the card*, because the rewards terms let Chipotle
change a cost at any time; a procedure that finds it has moved rejects with
``REWARD_COST_CHANGED`` rather than silently charging the new price. A lookup
that returns ``None`` therefore produces a card with no quoted cost, and
``sql/12_procedures.sql`` reads a null ``QUOTED_POINT_COST`` as *skip the check*
-- which is the honest behaviour for a deployment whose reward catalogue has not
been published, and is visible on the card as an absent figure rather than as an
invented one.
"""


class OpsDesk:
    """One visitor's action lane, over the app's records and the deployed ops API.

    Satisfies :class:`chip_chat.agent.desk.Desk`. Structural rather than
    declared, because ``api/`` may import ``agent/`` and the protocol is
    deliberately not a base class -- see :mod:`chip_chat.agent.desk`.

    Thread-safe to the extent its collaborators are, which is to say entirely:
    the two record stores hold their own locks and the client's only mutable
    state is a guarded availability answer.
    """

    __slots__ = (
        "_client",
        "_confirmations",
        "_drafts",
        "_grants",
        "_rewards",
        "_visitors",
    )

    def __init__(
        self,
        drafts: DraftStore,
        confirmations: ConfirmationLedger,
        client: OpsClient,
        grants: GrantSigner,
        visitors: VisitorDesk,
        *,
        rewards: RewardLookup | None = None,
    ) -> None:
        """Assemble the lane.

        Every argument but the last is required and positional, which is the
        same shape :class:`~chip_chat.api.ops.OpsService` uses and for the same
        reason: there must be no configuration in which the gate is absent. A
        desk without a draft store has no flag to claim; a desk without a signer
        can produce no grant and would be refused by the ops API on every call,
        one layer away from where anybody would look for the cause.

        Args:
            drafts: Where ``propose_order`` mints and ``place_order`` claims.
            confirmations: Where the other three mint and claim.
            client: The deployed ops API.
            grants: What turns a claimed record into something the ops API can
                verify without seeing this process's memory.
            visitors: The session-to-visitor binding. Read, never written.
            rewards: See :data:`RewardLookup`.
        """
        self._drafts = drafts
        self._confirmations = confirmations
        self._client = client
        self._grants = grants
        self._visitors = visitors
        self._rewards = rewards

    # --- what the agent asks about this deployment -------------------------

    @property
    def writes_here(self) -> bool:
        """False. The write is an HTTP call and the ops span is the ops API's.

        :mod:`chip_chat.agent.desk` gives the argument in full, and the short
        version is that ``ops.<action>`` is opened in the *other* process, as a
        child of the ``tool.<name>`` this call is already inside, from the W3C
        trace context :class:`~chip_chat.api.opsclient.OpsClient` injects.
        """
        return False

    def offers_every_write(self) -> bool:
        """True. All four of PRD T1's actions have a service behind them here."""
        return True

    def orderable_menu(self) -> OrderableMenu:
        """What the published catalogue can price, as ``propose_order`` needs it.

        The whole catalogue rather than a curated subset, and it is small enough
        for that to be reasonable -- ``CHIP_CHAT.CATALOGUE.menu_items`` is the
        ten rows the harvest published. A catalogue that grew past what belongs
        in a tool definition would want
        :meth:`chip_chat.agent.desk.Desk.orderable_menu` to answer ``None`` and
        leave the schema open, and the enforcement would fall back to where it
        has always really been: the draft store's own pricing, which refuses an
        unpriced item, and the procedure's catalogue check behind that.

        The description is composed rather than the ids alone, because a model
        that can see ten opaque ``CMG-*`` ids and nothing else cannot compose a
        draft that survives :meth:`DraftStore._require_groups` -- which is not a
        guess, it is what the first deployment of this lane did, three times,
        until it hit the loop's step ceiling. :mod:`chip_chat.agent.desk` records
        that at length.
        """
        catalog = self._drafts.catalog
        return OrderableMenu(
            item_ids=tuple(sorted(item.item_id for item in catalog.menu_items)),
            described=_describe(catalog),
        )

    def available(self) -> bool:
        """Whether a card composed now should say ordering is available."""
        return self._client.available()

    # --- the two halves of an order ----------------------------------------

    def propose(self, session_id: str, items: Sequence[Mapping[str, Any]]) -> "_Card":
        """Price a set of lines against the published catalogue.

        Priced at the **visitor's home store**, which is not the model's to
        choose: ``docs/action-surface.md`` §7.1 and
        :class:`~chip_chat.api.drafts.DraftStore` both say a live order is priced
        at the store it is placed at or it is rejected, and the store comes off
        the roster binding rather than off the call.

        Raises:
            OrderRejectedError: With the draft store's own published code, or
                :data:`NO_VISITOR` where the session is unbound.
        """
        visitor = self._visitor(session_id)
        try:
            draft = self._drafts.propose(
                visitor.demo_id,
                items,
                restaurant_id=self._home_store(visitor),
            )
        except DraftRejectedError as rejection:
            raise OrderRejectedError(
                rejection.code.value, rejection.message
            ) from rejection
        return _Card(self._card_for(draft))

    def _home_store(self, visitor: VisitorSession) -> int | None:
        """Where this visitor's order is priced, or ``None`` for the catalogue's.

        ``docs/action-surface.md`` §7.1: a live order is priced at the store it
        is placed at, and never at one the visitor is not standing in. The store
        is the roster row's, which reaches this process on
        :attr:`~chip_chat.api.visitors.VisitorSession.fixture`.

        Two things make that answer ``None`` more often than it looks, and both
        are honest rather than defensive.

        **The fixture is deliberately not journalled**, so a binding that
        survived a restart carries the ``demo_id`` and not the row --
        :meth:`VisitorSession.as_record` gives the reason: journalling a copy
        would let a restart serve an account summary Snowflake has since reset.

        **And the harvest priced two restaurants, not thirty.** The synthetic
        population is spread across every published store, and
        :attr:`~chip_chat.catalog.MenuCatalog.restaurant_ids` names only the
        ones with prices in the catalogue -- so most visitors' home stores have
        no published price list at all. Handing one to the draft store produces
        ``STORE_NOT_PRICED``, which is
        :meth:`~chip_chat.api.drafts.DraftStore._require_priced` doing exactly
        its job (*"quoting the reference restaurant's prices instead would be a
        total that looks right and is not"*) and which, on this deployment,
        would mean no visitor could ever order. So the check is made *here*,
        where the alternative is not a wrong total but a different and correctly
        priced store, and the card names which one it was.

        This is a **data limitation surfacing as a pricing decision**, and it
        goes away by harvesting prices for more restaurants rather than by
        changing anything in this file. Until then the honest reading of a card
        is: this is what these items cost at the restaurant named on it.
        """
        if visitor.fixture is None:
            return None
        home = visitor.fixture.home_store
        if home is None or home not in self._drafts.catalog.restaurant_ids:
            return None
        return home

    def _card_for(self, draft: Draft) -> Mapping[str, Any]:
        """Render one draft, and say so when ordering is unavailable.

        :meth:`DraftStore.card` rather than :meth:`Draft.as_card` because the
        store is what knows the restaurant's published name and address, and a
        card that showed a bare ``restaurant_id`` would be a card nobody could
        check against the shop they are standing in.
        """
        card = self._drafts.card(draft)
        return card if self.available() else unavailable_card(card)

    def confirm(self, session_id: str, reference: str) -> object | None:
        """Mark a card confirmed. **The launch gate**, and no tool reaches it.

        One method for both record types, because a browser pressing Confirm
        does not know which of the two it is looking at and should not have to:
        the id is either a draft this visitor holds or a confirmation they do,
        and never both. Tried in that order because drafts are much the commoner
        case and the miss costs a dictionary lookup.

        Args:
            session_id: The session the confirming request carried.
            reference: The ``draft_id`` or ``confirmation_id`` on the card.

        Returns:
            The confirmed record, or ``None`` when this visitor has no such live
            card. ``None`` is deliberately not an error: a visitor pressing
            Confirm on a card that aged out in another tab is not an incident.
        """
        visitor = self._visitors.visitor(session_id)
        if visitor is None:
            return None
        try:
            return self._drafts.confirm(visitor.demo_id, reference)
        except DraftRejectedError:
            pass
        try:
            return self._confirmations.confirm(visitor.demo_id, reference)
        except ConfirmationRejectedError:
            return None

    def place(self, session_id: str, draft_id: str) -> "_Receipt":
        """Claim the confirmed draft, sign what was claimed, and write.

        The order of the three verbs is the gate. The claim happens first and in
        this process, because this is where the confirmation flag lives; it
        raises where nobody confirmed, and the ops API is never called at all.
        The signature happens second, over what the claim returned. The write
        happens third, and carries no field the claim did not put inside the
        signature.

        Raises:
            OrderRejectedError: The gate refused, the procedure refused, or the
                write path is down -- the last with :data:`OPS_DECLINED` and
                RFC-001 §10's own sentence.
        """
        visitor = self._visitor(session_id)
        try:
            draft = self._drafts.claim(visitor.demo_id, draft_id)
        except DraftRejectedError as rejection:
            # Logged, and this is not debugging left in by accident. A draft
            # store lives in one process, so "the card the visitor is looking at
            # is not in the store" has two very different causes -- an agent
            # that skipped the confirmation step, which is the gate working, and
            # a second replica or a restarted process, which is the honest
            # limitation `chip_chat.api.ledger.BudgetLedger` carries and which
            # would otherwise present as the gate refusing a visitor who did
            # everything right. The count and the pid are what tell them apart
            # in a log, without either reaching the model or the visitor.
            _log.warning(
                "%s refused for %s in pid %d: %s (this process holds %d draft(s))",
                OpsAction.PLACE_ORDER.value,
                draft_id,
                os.getpid(),
                rejection.code.value,
                len(self._drafts),
            )
            raise OrderRejectedError(
                rejection.code.value, rejection.message
            ) from rejection
        return _Receipt(
            self._write(
                OpsAction.PLACE_ORDER,
                visitor.demo_id,
                draft.draft_id,
                _order_arguments(draft),
                reference=draft.draft_id,
            )
        )

    # --- the three that name a row -----------------------------------------

    def act(
        self, session_id: str, action: OpsAction, arguments: Mapping[str, Any]
    ) -> ActionOutcome:
        """Offer a card for one of the three, or write the one already confirmed.

        Called once by the model and answered twice; :func:`chip_chat.agent.tools._act`
        has the argument for why that is the gate holding rather than the gate
        asking to be talked past.

        Raises:
            OrderRejectedError: The procedure refused, the write path is down, or
                the session is unbound.
        """
        visitor = self._visitor(session_id)
        reference, payload = self._reference(session_id, action, arguments)
        record = self._confirmations.find(visitor.demo_id, action, reference)
        if record is None or not record.confirmed:
            return ActionOutcome(
                card=self._card(self._offer(visitor.demo_id, action, reference, payload))
            )
        claimed = self._confirmations.claim(visitor.demo_id, action, reference)
        return ActionOutcome(
            receipt=self._write(
                action,
                visitor.demo_id,
                claimed.confirmation_id,
                _action_arguments(action, claimed),
                reference=reference,
            )
        )

    # --- one write, once, for all four -------------------------------------

    def _write(
        self,
        action: OpsAction,
        demo_id: str,
        retry_key: str,
        arguments: Sequence[Any],
        *,
        reference: str,
    ) -> Mapping[str, Any]:
        """Sign the claimed record and post it. The body of all four writes.

        The retry key is the *record's* id rather than a fresh one, which is what
        makes a retried HTTP request replay a receipt instead of writing twice:
        the procedure spends the key inside its own transaction with a ``MERGE``.
        ``docs/ops-api.md`` describes that mechanism for a connection that dies
        after the procedure committed, and here it does the same job one layer
        further out.

        Raises:
            OrderRejectedError: For a refusal and for an outage alike, because
                the tool contract is that a write that did not happen is
                something the model can read and say. The two are told apart by
                the code.
        """
        _, token = self._grants.mint(action, demo_id, reference, arguments)
        try:
            return self._client.write(
                action, demo_id=demo_id, reference=reference, confirmation=token
            )
        except OpsRejectedError as refused:
            raise OrderRejectedError(refused.code, refused.detail) from refused
        except OpsUnavailableError as down:
            _log.warning("the ops API declined %s: %s", action.value, down.detail)
            raise OrderRejectedError(OPS_DECLINED, OPS_UNAVAILABLE_MESSAGE) from down
        finally:
            del retry_key

    # --- records -----------------------------------------------------------

    def _offer(
        self,
        demo_id: str,
        action: OpsAction,
        reference: str,
        payload: Mapping[str, Any],
    ) -> Confirmation:
        """Mint the unconfirmed record for one of the three, as its card.

        The three ``offer_*`` helpers in :mod:`chip_chat.api.ops` are used rather
        than :meth:`ConfirmationLedger.offer` directly, and the reason is in that
        module: composing the card and composing the write's arguments from the
        *same* function is what stops the two sides of a confirmation drifting.
        """
        if action is OpsAction.CANCEL_ORDER:
            return offer_cancellation(self._confirmations, demo_id, reference)
        if action is OpsAction.REDEEM_POINTS:
            return offer_redemption(
                self._confirmations,
                demo_id,
                reference,
                name=str(payload.get("name") or reference),
                point_cost=_whole(payload.get("point_cost")),
            )
        return offer_preferences(self._confirmations, demo_id, payload.get("prefs", {}))

    def _reference(
        self, session_id: str, action: OpsAction, arguments: Mapping[str, Any]
    ) -> tuple[str, Mapping[str, Any]]:
        """What the card is keyed by, and what it should say.

        Two of the three name a row that exists, so the reference is the id the
        model supplied -- which is safe precisely because it is only ever used to
        *find a confirmation*, never to compose a write: the arguments come off
        the claimed record. The third names nothing, so the card's own content is
        its identifier, and
        :func:`~chip_chat.api.confirmations.preferences_reference` is that digest.

        Raises:
            OrderRejectedError: Where the argument is empty. A reference the
                model left blank is a call it should make again with one.
        """
        if action is OpsAction.UPDATE_PREFERENCES:
            prefs = arguments.get("prefs") or {}
            if not prefs:
                raise OrderRejectedError(
                    "NOTHING_TO_UPDATE",
                    "name at least one of display_name, home_store or stated_preferences",
                )
            return preferences_reference(prefs), {"prefs": dict(prefs)}
        field = "order_id" if action is OpsAction.CANCEL_ORDER else "reward_id"
        reference = str(arguments.get(field) or "").strip()
        if not reference:
            raise OrderRejectedError(
                "REFERENCE_REQUIRED", f"{action.value} needs a {field}"
            )
        if action is OpsAction.CANCEL_ORDER:
            return reference, {"order_id": reference}
        return reference, self._reward(session_id, reference)

    def _reward(self, session_id: str, reward_id: str) -> Mapping[str, Any]:
        """What the redemption card says about the reward. See :data:`RewardLookup`."""
        if self._rewards is None:
            return {"name": reward_id, "point_cost": None}
        found = self._rewards(session_id, reward_id)
        if found is None:
            return {"name": reward_id, "point_cost": None}
        name, point_cost = found
        return {"name": name, "point_cost": point_cost}

    def _card(self, record: Confirmation) -> Mapping[str, Any]:
        """Render one confirmation, and say so when ordering is unavailable.

        RFC-001 §10 exactly: the card renders and reports the outage rather than
        being withdrawn, because a visitor who is told nothing assumes the write
        went through. Asked here, while the card is being composed, which is the
        only moment at which that is possible.
        """
        card = record.as_card()
        return card if self.available() else unavailable_card(card)

    def _visitor(self, session_id: str) -> VisitorSession:
        """Resolve the session to a synthetic customer, or refuse the write.

        **The only place in this class where a ``demo_id`` comes into
        existence**, and it comes from the session store rather than from any
        argument. RFC-001 §05: identity is bound by the app and is never a field
        a caller fills in.

        Raises:
            OrderRejectedError: :data:`NO_VISITOR` where nothing is bound.
        """
        visitor = self._visitors.visitor(session_id)
        if visitor is None or not visitor.demo_id:
            raise OrderRejectedError(
                NO_VISITOR,
                "this conversation has no synthetic customer bound to it, so "
                "there is nobody to write for",
            )
        return visitor


class _Card:
    """A rendered card, in the shape the tool bodies expect.

    :class:`chip_chat.agent.desk.Card` asks for ``as_card()`` and what this
    holds is already the rendered mapping -- rendered *by the store*, because
    only the store knows the restaurant row, and rendered *while the availability
    answer was fresh*, because RFC-001 §10 wants the card itself to report an
    outage. Both of those happen at proposal time, so the card is composed once
    and handed on rather than re-derived by whoever asks for it.
    """

    __slots__ = ("_card",)

    def __init__(self, card: Mapping[str, Any]) -> None:
        self._card = card

    def as_card(self) -> Mapping[str, Any]:
        """The card, as the store composed it."""
        return dict(self._card)


class _Receipt:
    """A receipt the ops API composed, in the shape the tool bodies expect.

    :class:`chip_chat.agent.desk.Receipted` is structural and asks for
    ``as_dict()``; what comes back over the wire is already a mapping. This is
    the two lines that reconcile them rather than a second receipt type -- the
    body is passed through verbatim, because a receipt rebuilt in this tier
    would be a second chance to disagree with the rows that were written.
    """

    __slots__ = ("_body",)

    def __init__(self, body: Mapping[str, Any]) -> None:
        self._body = body

    def as_dict(self) -> Mapping[str, Any]:
        """The procedure's own receipt, unchanged."""
        return dict(self._body)


def _describe(catalog: MenuCatalog) -> str:
    """Say what the catalogue can price, in the fewest words a model can use.

    One line per item: the id, the published name, the reference restaurant's
    price, and -- for an item whose menu declares content groups -- what each
    group requires and which modifier ids fill it. Everything is read off the
    catalogue; nothing here is written down.

    **Why the price is the reference restaurant's** rather than the visitor's.
    This string goes in a *tool definition*, which is composed once per turn and
    before any visitor is known to this function, so a per-visitor figure would
    be either wrong or a second read on every turn. It is a rough guide for the
    model's own sentences and the card is the number that counts: the draft is
    priced at the store the card names, in the column its channel selects, and
    that is the total the visitor confirms and the procedure re-derives. A model
    quoting this figure at a visitor whose store prices differently would be
    quoting a real published price at the wrong restaurant, which is why
    ``propose_order``'s own description already tells it to show the card.

    **Why the required groups matter more than the price.** They are the
    difference between a draft that prices and one refused with
    ``REQUIRED_SLOT_EMPTY``: a bowl needs a rice, and the menu says so in
    ``min_quantity`` on every member of ``RiceContentGroup`` rather than in any
    field a model could otherwise reach.
    """
    prices = {
        row.item_id: row
        for row in catalog.item_prices
        if row.restaurant_id == catalog.reference_restaurant_id
    }
    groups: dict[str, dict[str, list[str]]] = {}
    minimums: dict[str, dict[str, int]] = {}
    for modifier in catalog.modifiers:
        if modifier.group_name is None:
            continue
        groups.setdefault(modifier.item_id, {}).setdefault(
            modifier.group_name, []
        ).append(modifier.modifier_item_id)
        if modifier.min_quantity:
            required = minimums.setdefault(modifier.item_id, {})
            required[modifier.group_name] = max(
                required.get(modifier.group_name, 0), modifier.min_quantity
            )
    lines = []
    for item in sorted(catalog.menu_items, key=lambda row: row.item_id):
        price = prices.get(item.item_id)
        money = "" if price is None else f" (${price.unit_price})"
        needed = [
            f"{count} from {group} "
            f"[{', '.join(sorted(set(groups[item.item_id][group])))}]"
            for group, count in sorted(minimums.get(item.item_id, {}).items())
        ]
        requires = f" -- requires {'; '.join(needed)}" if needed else ""
        lines.append(f"{item.item_id} = {item.name}{money}{requires}")
    return " | ".join(lines)


def _order_arguments(draft: Draft) -> Sequence[Any]:
    """``place_order``'s arguments after the retry key, from the claimed draft.

    Deliberately the same three values, in the same order, as
    :func:`chip_chat.api.ops._order_arguments` builds when the ops API claims a
    draft out of its own store -- the store id, the channel and the lines, all
    read off the record and none off the call. It is duplicated rather than
    imported because it is the *app* tier's transcription of the same
    declaration, and the two are checked against each other by
    ``api/tests/test_grants.py`` rather than kept in step by an import that
    would hide a disagreement rather than surface one.
    """
    return (
        draft.restaurant_id,
        "DELIVERY" if draft.order_type.value == "delivery" else "IN_STORE",
        [
            {
                "item_id": line.item_id,
                "qty": line.quantity,
                "modifiers": [
                    selection.modifier_item_id for selection in line.selections
                ],
            }
            for line in draft.lines
        ],
    )


def _action_arguments(action: OpsAction, record: Confirmation) -> Sequence[Any]:
    """The other three procedures' arguments after the retry key.

    From the claimed record and never from the call, which is the property that
    makes the gate worth having: ``redeem_points`` sends the point cost that was
    on the card, and ``update_preferences`` sends the card's copy of the
    preferences rather than the argument that arrived with the tool call.
    """
    if action is OpsAction.CANCEL_ORDER:
        return (record.reference_id,)
    if action is OpsAction.REDEEM_POINTS:
        return (record.reference_id, record.payload.get("point_cost"))
    return (_plain(record.payload.get("prefs", {})),)


def _whole(value: Any) -> int | None:
    """Read a point cost as a whole number, or decline to. See :data:`RewardLookup`."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _plain(value: Any) -> Any:
    """Return a JSON-shaped copy of a frozen payload, for a signature to cover."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, str | bytes):
        return value
    if isinstance(value, Sequence):
        return [_plain(item) for item in value]
    return value
