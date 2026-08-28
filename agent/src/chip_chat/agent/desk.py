"""What the action lane looks like from the agent's side, and nothing about how.

The five read tools reach their services through :class:`chip_chat.agent.lanes.Lanes`,
which is a value the app hands in rather than a client this package builds --
``lanes.py``'s own docstring gives the argument, and it is about a deployment
name, an endpoint or a credential having exactly one place it is resolved. The
five *action* tools had no such seam. They reached
:class:`chip_chat.agent.orders.OrderDesk` directly, which is an in-process draft
store over three hardcoded items, and that is why ``docs/ops-api.md`` could say
the ops API was deployed, credentialled and refusing correctly while the chat app
did not call it: there was nowhere for a caller to put a different desk.

:class:`Desk` is that seam. It is a Protocol and not a base class for the same
reason :class:`chip_chat.snowflake.reads.SessionCheckout` is one: the two
implementations live in different packages and neither may import the other.
``agent/`` must not import ``api/`` -- that direction is load-bearing and
``make imports`` enforces it -- so the desk the deployed app uses is an object
from :mod:`chip_chat.api.orderdesk` that satisfies this shape, and the desk a
week-one slice uses is :class:`~chip_chat.agent.orders.OrderDesk`.

**Two properties are on the protocol rather than inferred, and both are about
honesty rather than dispatch.**

:attr:`Desk.writes_here` says whether the write happens in *this* process. It
decides whether :func:`chip_chat.agent.tools.dispatch` opens ``ops.<action>``
itself. A local desk is the write, so the agent emits the span; a remote desk is
an HTTP call to a service that opens its own ``ops.<action>`` as a child of this
``tool.<name>`` from the trace context on the request, and an agent that opened
one too would produce two spans for one write and a trace that says the gate ran
twice. Worse than untidy: ``continue_turn(..., parent=SpanName.TOOL)`` on the ops
API's edge *refuses* a write whose parent span is not a tool span, so an ops span
opened here would make every remote write fail with ``TRACE_CONTEXT_REQUIRED``.

:meth:`Desk.orderable_item_ids` is the vocabulary ``propose_order``'s schema is
narrowed to. RFC-001 D3 says the model may describe food and may never name a
SKU; the enforcement is the deterministic matcher and the ops API's catalogue
check, and the *schema* narrowing is a third, cheap layer that makes an
unorderable id unexpressible rather than merely rejected. It has to come from the
desk because only the desk knows what it can price -- ``tools.py`` used to pin it
to the three hardcoded items in a function that said, in as many words, *"when
the real catalogue reaches the desk, the enum is generated from it and this is
the function that does it"*. This is that.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from chip_chat.otel import OpsAction

__all__ = ["ActionOutcome", "Card", "Desk", "Receipted"]


@runtime_checkable
class Card(Protocol):
    """Something that renders as a confirmation card.

    Structural rather than nominal because the two things that satisfy it are
    :class:`chip_chat.agent.orders.Draft` and
    :class:`chip_chat.api.drafts.Draft`, which are different classes in
    different packages holding the same idea at two levels of fidelity.
    """

    def as_card(self) -> Mapping[str, Any]:
        """The card the widget renders and the visitor presses Confirm on."""
        ...


@runtime_checkable
class Receipted(Protocol):
    """Something that renders as a receipt for a write that happened."""

    def as_dict(self) -> Mapping[str, Any]:
        """The receipt, as the model is handed it and the widget renders it."""
        ...


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """What one of the three writes without a draft produced.

    Exactly one of the two fields is set, and which one is the whole of the
    confirmation flow for ``cancel_order``, ``redeem_points`` and
    ``update_preferences``. ``propose_order`` and ``place_order`` already had
    this shape spelled as two separate tools; the other three name a row rather
    than compose one, so there is nothing for a ``propose_`` half to do and the
    two halves are two *calls of the same tool* instead.

    The model is told which it got, in words, by
    :mod:`chip_chat.agent.tools`. It is never told to fabricate the second call:
    the first call returns a card and the second one only succeeds because a
    request carrying the visitor's session arrived in between.

    Attributes:
        card: The confirmation card to render, when nobody has confirmed yet.
        receipt: What the write returned, when somebody had.
    """

    card: Mapping[str, Any] | None = None
    receipt: Mapping[str, Any] | None = None

    @property
    def confirmed(self) -> bool:
        """Whether this outcome is a write that happened."""
        return self.receipt is not None


class Desk(Protocol):
    """The action lane, as the five action tools need it.

    Every method takes ``session_id`` and no method takes a visitor identifier.
    That is the same absence :mod:`chip_chat.agent.surface` is built on, carried
    into the one protocol that reaches a write: the session is supplied by the
    request handler, resolved to a ``demo_id`` by whatever implements this, and
    never read out of a tool argument.
    """

    @property
    def writes_here(self) -> bool:
        """Whether the write happens in this process. See the module docstring."""
        ...

    def offers_every_write(self) -> bool:
        """Whether this desk can answer all four write tools or only ``place_order``.

        The week-one desk answers one, because ``cancel_order``,
        ``redeem_points`` and ``update_preferences`` name rows in Snowflake that
        an in-process dictionary of three hardcoded items does not have. A tool
        definition the model can see and nothing can answer is worse than an
        absent one -- ``lanes.py`` makes that argument for the read lanes and it
        is the same argument here -- so
        :func:`chip_chat.agent.tools.offered_tools` asks this.
        """
        ...

    def orderable_item_ids(self) -> tuple[str, ...] | None:
        """The item ids ``propose_order``'s schema is narrowed to, or ``None``.

        ``None`` leaves the schema open, which is right for a desk whose
        catalogue is too large to enumerate in a tool definition. Neither desk
        in this repository is that, and the shape is here so that a future one
        can decline rather than truncate.
        """
        ...

    def propose(self, session_id: str, items: Sequence[Mapping[str, Any]]) -> Card:
        """Price a set of lines and mint an unconfirmed draft.

        Raises:
            Exception: The desk's own typed rejection, which
                :func:`chip_chat.agent.tools.dispatch` turns into a result the
                model can read. A draft is not repaired into validity.
        """
        ...

    def confirm(self, session_id: str, reference: str) -> object | None:
        """Mark a card confirmed on behalf of the visitor whose request this is.

        **The launch gate**, and the one method here no tool reaches. It is
        called by the request handler when a request carrying the session cookie
        says Confirm was pressed, and by nothing else.

        Returns:
            The confirmed record, or ``None`` when there is no such live card
            for this session.
        """
        ...

    def place(self, session_id: str, draft_id: str) -> Receipted:
        """Place a confirmed draft and return its receipt.

        Raises:
            Exception: The desk's own typed rejection where the draft is
                unknown, expired, or was never confirmed.
        """
        ...

    def act(
        self, session_id: str, action: OpsAction, arguments: Mapping[str, Any]
    ) -> ActionOutcome:
        """Offer or perform one of the three writes that name a row.

        Called once by the model and answered twice: a card the first time, and
        the receipt after a request carrying the visitor's session confirmed it.

        Args:
            session_id: The bound conversation.
            action: Which of ``cancel_order``, ``redeem_points`` or
                ``update_preferences``.
            arguments: The tool's own arguments, validated by
                :mod:`chip_chat.agent.surface` before they arrive here.

        Raises:
            Exception: The desk's own typed rejection.
        """
        ...

    def available(self) -> bool:
        """Whether a card composed now should say ordering is available.

        Asked while a card is being composed rather than when Confirm is
        pressed, because RFC-001 §10 wants the card to *render* and report the
        outage -- which is only possible if somebody asked in advance.
        """
        ...
