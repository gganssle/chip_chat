"""The ops API: the only path that writes, and the place confirmation is enforced.

RFC-001 section 03 gives writes their own service on Azure Functions, holding the
only credentials in the system with the Snowflake write role, and section 06 says
what that service is *for*: **the ops API rejects any draft that has not been
marked confirmed by a request carrying the visitor's session.** Confirmation is
not a prompt instruction and not a UI convention. It is a precondition checked
here, in code, on every write.

Issue #63 is the second launch gate, and the gate is only real if it is
structural. Four things make it so, and each is a test in ``api/tests/test_ops.py``:

**The record is read before the procedure is called.** Every write in
:class:`OpsSession` begins by claiming something the visitor was shown -- a
:class:`~chip_chat.api.drafts.Draft` for ``place_order``, a
:class:`~chip_chat.api.confirmations.Confirmation` for the other three -- and a
record that is missing, unconfirmed or expired ends the call before a session is
acquired. An agent that decides to skip the confirmation step produces a
rejection and an eval failure, not an order.

**What is written is what was confirmed, not what was asked for.** The
procedure arguments are built from the claimed record. ``place_order`` sends the
draft's own lines, priced at the draft's own restaurant, in the draft's own
channel; ``redeem_points`` sends the point cost that was on the card. Nothing
that arrives with the call reaches a procedure except the identifier of the
record itself. There is therefore no argument through which a model could alter
an order between the card and the write.

**No write takes a visitor identifier.** :class:`OpsService.session` binds the
``demo_id`` the app resolved from the session cookie, once, and every write
method on the returned :class:`OpsSession` is identity-free -- the same absence
:data:`chip_chat.snowflake.procedures.IDENTITY_VOCABULARY` names one tier down,
and ``test_ops.py`` holds these signatures to that same list. Identity arrives as
a session binding and an argument list cannot contradict it.

**No SQL is written here.** The procedure name, its argument order and its
argument count all come from
:data:`chip_chat.snowflake.procedures.PROCEDURES`, which is issue #46's
declaration of the write path. A procedure that grows an argument fails this
module's wiring check rather than being called with the wrong tuple.

Failure behaviour is RFC-001 section 10's row for this service: **blast radius
is writes only**. :class:`OpsUnavailableError` carries
:data:`OPS_UNAVAILABLE_MESSAGE`, :func:`unavailable_card` is the card that says
it, and neither is an exception a read lane ever sees. Nothing is half-written,
because a rejection returns before a transaction opens and the procedures
themselves roll back anything else -- see the header of ``sql/12_procedures.sql``.

**Idempotency is the record's id, and it is never the caller's.** The retry key
handed to every procedure is the draft id or the confirmation id: an identifier
minted by the app, unique to one card, and not reusable, because claiming a
record retires it. Threading it through is what makes a retried *procedure call*
-- the timeout where Snowflake saw one call and the world had two -- replay a
stored receipt instead of writing twice.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final, Protocol

from chip_chat.api.confirmations import (
    Confirmation,
    ConfirmationCode,
    ConfirmationLedger,
    ConfirmationRejectedError,
    preferences_reference,
)
from chip_chat.api.drafts import Draft, DraftRejectedError, DraftStore, OrderType
from chip_chat.api.drafts import RejectionCode as DraftCode
from chip_chat.otel import ConfirmationState, OpsAction, ops_write
from chip_chat.snowflake.procedures import Procedure, procedure

__all__ = [
    "OPS_UNAVAILABLE_MESSAGE",
    "PRECONDITION_REJECTIONS",
    "SESSION_HEADER",
    "OpsRejectedError",
    "OpsService",
    "OpsSession",
    "OpsUnavailableError",
    "Receipt",
    "WriteBackend",
    "WriteSession",
    "offer_cancellation",
    "offer_preferences",
    "offer_redemption",
    "unavailable_card",
]

OPS_UNAVAILABLE_MESSAGE: Final = (
    "Ordering is temporarily unavailable — nothing was placed, and this card is "
    "still here."
)
"""What a visitor is told when the write path cannot be reached.

RFC-001 section 10: *ops API unavailable → confirmation card renders but reports
that ordering is temporarily unavailable; nothing is half-written*. Two halves,
both load-bearing. It says ordering rather than Cilantro, because the knowledge,
account and personalization lanes are unaffected and telling a visitor the whole
assistant is down would be false. And it says nothing was placed, because the
one question somebody who has just pressed Confirm actually has is whether they
are about to be charged twice.

Distinct from :data:`chip_chat.api.outcome.STOP_STATE_MESSAGE`, which is the
budget's designed stop. This one is a failure and says so; that one is not.
"""

SESSION_HEADER: Final = "x-cilantro-session"
"""The header the app's server-side session token travels in.

Identity reaches the ops API the same way it reaches everything else: as a
session the server resolves, never as a field in a body a model can compose. The
Functions host maps this to a ``demo_id`` and hands it to
:meth:`OpsService.session`; nothing downstream of that point takes an identifier
at all.
"""

_ATTEMPTS: Final = 2
"""How many times one procedure call is made before the write path is called down.

Two, not more. The retry exists for the single failure the retry key was
introduced for -- a connection that dies after the procedure committed -- and the
second attempt replays the stored receipt rather than writing again. Anything
beyond that is a visitor waiting on a service that is already gone, which
RFC-001 section 10 would rather tell them about.
"""

_CHANNELS: Final[Mapping[OrderType, str]] = MappingProxyType(
    {
        OrderType.PICKUP: "IN_STORE",
        OrderType.DELIVERY: "DELIVERY",
    }
)
"""The draft's order type, in the two words ``orders.channel`` is written in.

The catalogue publishes two price columns and they differ by about thirty
percent, so the channel is not a label on the order -- it is which list priced
it. ``sql/12_procedures.sql`` rejects anything that is not one of these two.
"""

PRECONDITION_REJECTIONS: Final = (
    DraftCode.DRAFT_NOT_FOUND.value,
    DraftCode.DRAFT_NOT_CONFIRMED.value,
    DraftCode.DRAFT_EXPIRED.value,
    ConfirmationCode.NOT_FOUND.value,
    ConfirmationCode.NOT_CONFIRMED.value,
    ConfirmationCode.EXPIRED.value,
)
"""The six codes this service can return *before* a procedure is reached.

None of them is in :data:`chip_chat.snowflake.procedures.SHARED_REJECTIONS` and
none is in any procedure's own list, deliberately: the database is never asked
whether the visitor confirmed, because the flag lives where the model cannot
reach it and the database is not that place. A trace showing one of these is a
write that was refused by the gate rather than by the catalogue.
"""

_GATE_VIOLATIONS: Final = frozenset(
    {
        DraftCode.DRAFT_NOT_FOUND.value,
        DraftCode.DRAFT_NOT_CONFIRMED.value,
        ConfirmationCode.NOT_FOUND.value,
        ConfirmationCode.NOT_CONFIRMED.value,
    }
)
"""Which of the six are launch-gate violations, and therefore span failures.

Four of them are: the record does not exist for this visitor, or it exists and
they never pressed Confirm. Both mean a write was attempted against something
nobody agreed to, which is what
:attr:`~chip_chat.otel.attributes.ConfirmationState.REJECTED` is for and what an
eval counts.

The two expiry codes are not. Consent that has aged out is consent that was
plausibly given, and marking a fifteen-minute-old card as an agent violation
would fill a launch-gate dashboard with visitors who went to make a cup of tea.
Those record :attr:`~chip_chat.otel.attributes.ConfirmationState.UNCONFIRMED`
instead: not in force, not an accusation.
"""


class OpsUnavailableError(RuntimeError):
    """The write path could not be reached, and nothing was written.

    Attributes:
        message: :data:`OPS_UNAVAILABLE_MESSAGE`, for the visitor.
        detail: What actually happened, for the operator.
    """

    __slots__ = ("detail", "message")

    def __init__(self, detail: str) -> None:
        super().__init__(f"{OPS_UNAVAILABLE_MESSAGE} ({detail})")
        self.message = OPS_UNAVAILABLE_MESSAGE
        self.detail = detail


class OpsRejectedError(Exception):
    """A refused write, naming the rule it broke.

    Attributes:
        action: Which write was attempted.
        code: The rejection code -- one of :data:`PRECONDITION_REJECTIONS`, or
            one the procedure returned.
        detail: What happened, in a sentence.
        subject: The item, modifier or store the procedure named, where it named
            one. ``None`` for a refusal that is not about a particular row.
    """

    __slots__ = ("action", "code", "detail", "subject")

    def __init__(
        self,
        action: OpsAction,
        code: str,
        detail: str,
        subject: str | None = None,
    ) -> None:
        super().__init__(f"{action.value}: {code} -- {detail}")
        self.action = action
        self.code = code
        self.detail = detail
        self.subject = subject

    def as_result(self) -> Mapping[str, Any]:
        """Render the refusal as the tool result the model is handed back."""
        result: dict[str, Any] = {
            "ok": False,
            "action": self.action.value,
            "error": self.code,
            "detail": self.detail,
        }
        if self.subject is not None:
            result["subject"] = self.subject
        return result


@dataclass(frozen=True, slots=True)
class Receipt:
    """What a write returns: the procedure's own object, and what it acted on.

    Attributes:
        action: Which write produced it.
        reference_id: The draft, order, reward or preference card it acted on --
            always an identifier for something the visitor had already been
            shown, which is what makes a receipt referable to later in the
            conversation.
        body: What the stored procedure returned, verbatim. Not re-derived and
            not re-formatted: a receipt rebuilt in this tier is a second chance
            to disagree with the rows that were written.
        replayed: Whether the procedure recognised a spent retry key and
            returned a stored receipt rather than writing again. True means a
            retry found the first attempt had landed after all.
    """

    action: OpsAction
    reference_id: str
    body: Mapping[str, Any]
    replayed: bool = False

    def as_dict(self) -> Mapping[str, Any]:
        """The receipt as the app renders it and the model is handed it back."""
        return {
            **dict(self.body),
            "reference_id": self.reference_id,
            "replayed": self.replayed,
        }


class WriteSession(Protocol):
    """One Snowflake session with ``DEMO_ID`` bound, and the write role assumed.

    Obtained from :meth:`WriteBackend.session` and identity-free by construction:
    the visitor is bound on the connection, so there is no argument here through
    which a compromised caller could name somebody else. Row access policies
    filter what a session may *see*; an INSERT naming another visitor is a write,
    and only the absence of the field stops it.
    """

    def call(self, procedure_name: str, arguments: Sequence[object]) -> Mapping[str, Any]:
        """Call one stored procedure and return the ``VARIANT`` it returned.

        Args:
            procedure_name: Fully qualified, as
                :meth:`chip_chat.snowflake.procedures.Procedure.qualified`
                spells it.
            arguments: Positional, in declaration order.

        Returns:
            The decoded object. Every procedure returns one, and every one of
            them carries ``ok``.

        Raises:
            OpsUnavailableError: If the call could not be completed. Whether it
                was attempted is deliberately not claimed -- that is what the
                retry key is for.
        """
        ...


class WriteBackend(Protocol):
    """Where write sessions come from. The only holder of the write role."""

    def session(self, demo_id: str) -> WriteSession:
        """Acquire a session with ``DEMO_ID`` bound to ``demo_id``.

        Raises:
            OpsUnavailableError: If no session can be had. A visitor who has
                already pressed Confirm is told so and loses their card; a
                visitor who has not is never offered one, because
                :meth:`available` is asked while the card is being composed.
        """
        ...

    def available(self) -> bool:
        """Whether the write path is reachable right now.

        Called when a confirmation card is composed, not when it is pressed:
        RFC-001 section 10 wants the card to *render* and say ordering is
        unavailable, which is only possible if somebody asked in advance.
        """
        ...


class OpsService:
    """The four write actions, and the two ledgers that gate them.

    Construction is the wiring: a service cannot be built without somewhere to
    read confirmations from, so there is no configuration in which the gate is
    absent. The same shape :class:`chip_chat.api.turns.SpendGate` uses, and for
    the same reason -- "is there a caller?" is the wrong question, because a
    caller can be forgotten by the next route somebody adds.
    """

    __slots__ = ("_attempts", "_backend", "_confirmations", "_drafts")

    def __init__(
        self,
        backend: WriteBackend,
        drafts: DraftStore,
        confirmations: ConfirmationLedger,
        *,
        attempts: int = _ATTEMPTS,
    ) -> None:
        """Assemble the service.

        Args:
            backend: Where write sessions come from. Required and without a
                default.
            drafts: The store ``place_order`` claims from. Required: a service
                that could be built without one would be a service with no gate
                on its most consequential write.
            confirmations: The ledger the other three claim from. Required for
                the same reason.
            attempts: How many times one procedure call is made before the write
                path is called down. See :data:`_ATTEMPTS`.

        Raises:
            ValueError: If ``attempts`` is less than one.
        """
        if attempts < 1:
            raise ValueError("a write needs at least one attempt")
        self._backend = backend
        self._drafts = drafts
        self._confirmations = confirmations
        self._attempts = attempts

    def session(self, demo_id: str) -> "OpsSession":
        """Bind the service to one visitor, resolved from the session by the app.

        Args:
            demo_id: The visitor the request resolved to. This is the last place
                in the write path an identifier appears at all.

        Returns:
            A handle whose four write methods take no visitor identifier.

        Raises:
            ValueError: If ``demo_id`` is empty. A write for nobody is a wiring
                bug, not a rejection a visitor is told about.
        """
        if not demo_id:
            raise ValueError("a write has to be made on behalf of a visitor")
        return OpsSession(self, demo_id)

    def available(self) -> bool:
        """Whether ordering is available, for a card about to be rendered."""
        return self._backend.available()

    @property
    def drafts(self) -> DraftStore:
        """The store ``place_order`` claims from, for the app to propose into."""
        return self._drafts

    @property
    def confirmations(self) -> ConfirmationLedger:
        """The ledger the other three claim from, for the app to offer into."""
        return self._confirmations

    # --- the write path, once, for all four --------------------------------

    def _write(
        self,
        demo_id: str,
        action: OpsAction,
        reference_id: str,
        claim: Callable[[], tuple[str, Sequence[object]]],
    ) -> Receipt:
        """Gate, then write, then receipt. The body of every method below.

        The order is the argument. The record is claimed first, so a write
        nobody confirmed is refused without a session being acquired and without
        the database being asked a question whose answer it does not hold. The
        procedure is called second, with a retry key that is the record's own id.

        Args:
            demo_id: The bound visitor.
            action: Which write, which is also the span name.
            reference_id: What the visitor was shown.
            claim: Consumes the record and returns the retry key and the
                procedure's arguments after it. Raises the typed rejection.

        Returns:
            The receipt.

        Raises:
            OpsRejectedError: The gate refused, or the procedure did.
            OpsUnavailableError: The write path could not be reached.
        """
        with ops_write(action, reference_id=reference_id) as ops:
            try:
                retry_key, rest = claim()
            except (DraftRejectedError, ConfirmationRejectedError) as rejection:
                code = rejection.code.value
                ops.record_confirmation(
                    ConfirmationState.REJECTED
                    if code in _GATE_VIOLATIONS
                    else ConfirmationState.UNCONFIRMED
                )
                raise OpsRejectedError(action, code, rejection.message) from rejection
            ops.record_confirmation(ConfirmationState.CONFIRMED)

            declaration = procedure(action.value)
            arguments = _arguments(declaration, retry_key, rest)
            body = self._call(demo_id, declaration, arguments)
            if not body.get("ok", False):
                ops.record_failure(str(body.get("rejection", "WRITE_FAILED")))
                ops.set_metadata(rejection=body.get("rejection"))
                raise OpsRejectedError(
                    action,
                    str(body.get("rejection", "WRITE_FAILED")),
                    str(body.get("detail", "the write was refused")),
                    _optional(body.get("subject")),
                )

            receipt = Receipt(
                action=action,
                reference_id=reference_id,
                body=MappingProxyType(dict(body)),
                replayed=bool(body.get("replayed", False)),
            )
            ops.record_receipt(receipt.as_dict())
            return receipt

    def _call(
        self, demo_id: str, declaration: Procedure, arguments: Sequence[object]
    ) -> Mapping[str, Any]:
        """Call the procedure, retrying the same retry key on a transport failure.

        The retry is not optimism. The key is spent inside the procedure's own
        transaction, so a second attempt with the same key either finds the
        first attempt's receipt and replays it or discovers the first attempt
        never landed -- and in both cases exactly one write exists afterwards.
        """
        last: OpsUnavailableError | None = None
        for _ in range(self._attempts):
            try:
                return self._backend.session(demo_id).call(
                    declaration.qualified(), arguments
                )
            except OpsUnavailableError as unavailable:
                last = unavailable
        raise last if last is not None else OpsUnavailableError("no attempt was made")


class OpsSession:
    """One visitor's write handle. **No method here takes an identifier.**

    That absence is the mechanism rather than a convention:
    :data:`chip_chat.snowflake.procedures.IDENTITY_VOCABULARY` is the list of
    words no procedure argument may be spelled with, and ``test_ops.py`` holds
    these four signatures to the same list. A tier that grew a ``demo_id``
    parameter would be a field for a compromised caller to fill in with somebody
    else's identifier, one tier above the one that already refuses to have it.
    """

    __slots__ = ("_demo_id", "_service")

    def __init__(self, service: OpsService, demo_id: str) -> None:
        self._service = service
        self._demo_id = demo_id

    def place_order(self, draft_id: str) -> Receipt:
        """Place a confirmed draft, and return its receipt.

        The draft is claimed from :class:`~chip_chat.api.drafts.DraftStore`,
        which retires it as it hands it over -- so one draft becomes at most one
        order, and the lines that reach the procedure are the lines the card
        showed rather than anything that arrived with this call.

        Args:
            draft_id: The draft the visitor was shown and confirmed.

        Returns:
            The receipt: order id, store, lines, totals, points and the
            simulation notice, as ``sql/12_procedures.sql`` composed it.

        Raises:
            OpsRejectedError: ``DRAFT_NOT_CONFIRMED`` if the visitor never
                pressed Confirm -- the launch gate -- ``DRAFT_NOT_FOUND`` for an
                id that is not theirs, ``DRAFT_EXPIRED`` for one that aged out,
                or whichever catalogue rule the procedure refused.
            OpsUnavailableError: The write path could not be reached.
        """

        def claim() -> tuple[str, Sequence[object]]:
            draft = self._service.drafts.claim(self._demo_id, draft_id)
            return draft.draft_id, _order_arguments(draft)

        return self._service._write(self._demo_id, OpsAction.PLACE_ORDER, draft_id, claim)

    def cancel_order(self, order_id: str) -> Receipt:
        """Cancel an order the visitor placed, and return its receipt.

        Models an affordance the real product refuses -- see
        docs/action-surface.md section 3 and the header of
        ``sql/13_cancel_order.sql`` -- so the receipt says so out loud, in a
        sentence the procedure composes rather than this tier.

        Args:
            order_id: The order the visitor was shown and confirmed cancelling.

        Returns:
            The receipt: new status, points reversed, resulting balance, and the
            sentence about the real product's window.

        Raises:
            OpsRejectedError: ``CONFIRMATION_NOT_CONFIRMED`` for the launch gate,
                or ``ORDER_NOT_FOUND``, ``ORDER_NOT_CANCELLABLE`` or
                ``CANCELLATION_WINDOW_CLOSED`` from the procedure.
            OpsUnavailableError: The write path could not be reached.
        """

        def claim() -> tuple[str, Sequence[object]]:
            record = self._claim(OpsAction.CANCEL_ORDER, order_id)
            return record.confirmation_id, (order_id,)

        return self._service._write(
            self._demo_id, OpsAction.CANCEL_ORDER, order_id, claim
        )

    def redeem_points(self, reward_id: str) -> Receipt:
        """Redeem a published reward, and return the receipt and new balance.

        The point cost sent to the procedure is the one that was **on the card**,
        carried on the confirmation by :func:`offer_redemption`. The rewards
        terms let Chipotle change a cost at any time, so a cost read when the
        card was rendered is a quote; a procedure that finds it has moved
        rejects with ``REWARD_COST_CHANGED`` rather than silently charging the
        new price.

        Args:
            reward_id: The reward the visitor was shown and confirmed.

        Returns:
            The receipt: the reward's published name, points deducted, new
            balance, expiry, and the three sentences the terms require.

        Raises:
            OpsRejectedError: ``CONFIRMATION_NOT_CONFIRMED`` for the launch gate,
                or ``REWARD_UNAVAILABLE``, ``REWARD_COST_CHANGED`` or
                ``INSUFFICIENT_POINTS`` from the procedure.
            OpsUnavailableError: The write path could not be reached.
        """

        def claim() -> tuple[str, Sequence[object]]:
            record = self._claim(OpsAction.REDEEM_POINTS, reward_id)
            return record.confirmation_id, (reward_id, record.payload.get("point_cost"))

        return self._service._write(
            self._demo_id, OpsAction.REDEEM_POINTS, reward_id, claim
        )

    def update_preferences(self, prefs: Mapping[str, Any]) -> Receipt:
        """Store a preference edit, and return the acknowledgement.

        ``prefs`` names no row, so what identifies it is its own content:
        :func:`~chip_chat.api.confirmations.preferences_reference` digests
        exactly what was shown, and a call whose preferences differ by one
        character from the card's finds no confirmation. What reaches the
        procedure is the card's copy, not this argument.

        Args:
            prefs: A partial object over ``display_name``, ``home_store`` and
                ``stated_preferences``. Absent keys are unchanged; an explicit
                null clears.

        Returns:
            The acknowledgement, including the procedure's statement that a
            preference is not an allergen answer.

        Raises:
            OpsRejectedError: ``CONFIRMATION_NOT_CONFIRMED`` for the launch gate,
                or ``NAME_TOO_LONG``, ``STORE_NOT_FOUND``,
                ``MODIFIER_NOT_RECOGNISED``, ``STANCE_NOT_AVAILABLE_FOR_MODIFIER``,
                ``TOO_MANY_PREFERENCES`` or ``NOTHING_TO_UPDATE`` from the
                procedure.
            OpsUnavailableError: The write path could not be reached.
        """
        reference_id = preferences_reference(prefs)

        def claim() -> tuple[str, Sequence[object]]:
            record = self._claim(OpsAction.UPDATE_PREFERENCES, reference_id)
            shown = record.payload.get("prefs", {})
            return record.confirmation_id, (_plain(shown),)

        return self._service._write(
            self._demo_id, OpsAction.UPDATE_PREFERENCES, reference_id, claim
        )

    def _claim(self, action: OpsAction, reference_id: str) -> Confirmation:
        """Claim the confirmation for one of the three writes without a draft."""
        return self._service.confirmations.claim(self._demo_id, action, reference_id)


# ---------------------------------------------------------------------------
# Composing the cards, so that the two sides of a confirmation cannot drift.
# ---------------------------------------------------------------------------


def offer_cancellation(
    ledger: ConfirmationLedger,
    demo_id: str,
    order_id: str,
    *,
    placed_at: str | None = None,
    total: str | None = None,
) -> Confirmation:
    """Offer to cancel an order, and return the card to render.

    Args:
        ledger: Where the record is minted.
        demo_id: The visitor, resolved from the session by the app.
        order_id: The order being cancelled.
        placed_at: When it was placed, for the card. The cancellation window is
            checked by the procedure and not here.
        total: What it came to, for the card.

    Returns:
        The unconfirmed record. Pressing Confirm is a separate request.
    """
    return ledger.offer(
        demo_id,
        OpsAction.CANCEL_ORDER,
        order_id,
        {"order_id": order_id, "placed_at": placed_at, "total": total},
    )


def offer_redemption(
    ledger: ConfirmationLedger,
    demo_id: str,
    reward_id: str,
    *,
    name: str,
    point_cost: int,
) -> Confirmation:
    """Offer to redeem a reward, and return the card to render.

    The point cost goes on the record because the write must send what the
    visitor was shown; see :meth:`OpsSession.redeem_points`.

    Args:
        ledger: Where the record is minted.
        demo_id: The visitor, resolved from the session by the app.
        reward_id: The published reward's slug.
        name: Its published name, for the card.
        point_cost: What it costs, as read when the card was composed.

    Returns:
        The unconfirmed record.
    """
    return ledger.offer(
        demo_id,
        OpsAction.REDEEM_POINTS,
        reward_id,
        {"reward_id": reward_id, "name": name, "point_cost": point_cost},
    )


def offer_preferences(
    ledger: ConfirmationLedger, demo_id: str, prefs: Mapping[str, Any]
) -> Confirmation:
    """Offer a preference edit, and return the card to render.

    Args:
        ledger: Where the record is minted.
        demo_id: The visitor, resolved from the session by the app.
        prefs: Exactly what the card will show.

    Returns:
        The unconfirmed record, referenced by a digest of ``prefs``.
    """
    return ledger.offer(
        demo_id,
        OpsAction.UPDATE_PREFERENCES,
        preferences_reference(prefs),
        {"prefs": dict(prefs)},
    )


def unavailable_card(card: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return ``card`` as it renders while the write path is unreachable.

    RFC-001 section 10, exactly: the confirmation card still renders, and it
    reports that ordering is temporarily unavailable. The card is not withdrawn
    and the Confirm button is not silently disabled -- a visitor who is told
    nothing assumes the order went through.

    Args:
        card: A draft or confirmation card, as ``as_card()`` composed it.

    Returns:
        A copy carrying ``ordering_available`` and the message. The original is
        not modified.
    """
    return {
        **dict(card),
        "ordering_available": False,
        "unavailable_message": OPS_UNAVAILABLE_MESSAGE,
    }


# ---------------------------------------------------------------------------
# Procedure arguments. Built from the declaration, never typed out here.
# ---------------------------------------------------------------------------


def _arguments(
    declaration: Procedure, retry_key: str, rest: Sequence[object]
) -> Sequence[object]:
    """Assemble one procedure's positional arguments, retry key first.

    Raises:
        RuntimeError: If the tuple does not match the declared arity. That is a
            wiring bug -- issue #46 added an argument and this file was not
            changed -- and it is louder as a failure here than as a procedure
            called with a value in the wrong slot.
    """
    arguments = (retry_key, *rest)
    if len(arguments) != len(declaration.arguments):
        expected = ", ".join(argument.name for argument in declaration.arguments)
        raise RuntimeError(
            f"{declaration.qualified()} declares ({expected}) but the ops API "
            f"assembled {len(arguments)} arguments"
        )
    return arguments


def _order_arguments(draft: Draft) -> Sequence[object]:
    """Return ``place_order``'s three arguments after the retry key.

    ``STORE_ID``, ``CHANNEL`` and ``ORDER_LINES``, all read off the draft. The
    order is priced at the store it is placed at and in the column its channel
    selects, which is the whole reason those are arguments rather than something
    the database looks up.
    """
    return (
        draft.restaurant_id,
        _CHANNELS[draft.order_type],
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


def _plain(value: Any) -> Any:
    """Return a JSON-shaped copy of a frozen payload, for a driver to bind."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, str | bytes):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Sequence):
        return [_plain(item) for item in value]
    return value


def _optional(value: Any) -> str | None:
    """Return ``value`` as a string, or ``None`` where the procedure sent none."""
    return None if value is None else str(value)
