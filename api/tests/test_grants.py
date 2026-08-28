"""The confirmation grant, and the gate holding across a process boundary.

``api/tests/test_ops.py`` drives the gate as it was: one process, one draft
store, and a claim that reads a flag out of a dictionary. That file is untouched
and still passing, because the in-process path is untouched and still the one the
Functions host takes when nobody presents a grant.

What this file establishes is the other topology -- the *deployed* one, where the
chat app holds the confirmation flag and the ops API holds the write role, and
neither can see the other's memory. ``docs/decisions/confirmation-grants.md``
argues why the join is a signature rather than a shared store; these are the
properties that argument depends on, each driven rather than asserted about.

The last two tests are the ones worth reading first. They wire a real
:class:`~chip_chat.api.orderdesk.OpsDesk` to a real
:class:`~chip_chat.api.ops.OpsService` through a client that carries the grant
across the way HTTPS would -- so a draft proposed in one object is placed by
another that never sees the store it came from, and the *procedure arguments*
either side computed are compared. Two tiers transcribing one declaration is
exactly the drift :mod:`chip_chat.snowflake.procedures` exists to catch, and this
is where the app tier's copy is held to it.
"""

import json
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest

from chip_chat.agent.tools import DESK_WRITES, offered_tools
from chip_chat.api.confirmations import ConfirmationLedger
from chip_chat.api.drafts import DraftStore
from chip_chat.api.grants import (
    GRANT_HEADER,
    Grant,
    GrantCode,
    GrantRejectedError,
    GrantSigner,
    signing_key,
)
from chip_chat.api.ops import PRECONDITION_REJECTIONS, OpsRejectedError, OpsService
from chip_chat.api.opsclient import OpsClient
from chip_chat.api.orderdesk import OPS_DECLINED, OpsDesk
from chip_chat.api.testing import FakeClock, RecordingWriteBackend
from chip_chat.api.visitors import PersonaFixture, VisitorDesk, VisitorSessionStore
from chip_chat.catalog import MenuCatalog
from chip_chat.otel import OpsAction, ToolName
from chip_chat.otel.spans import agent_step, chat_turn, tool_call
from chip_chat.otel.testing import SpanRecorder, span_recorder

OPS_KEY = "a-shared-secret-both-tiers-hold"
VISITOR = "dm-000001"
STRANGER = "dm-000002"
SESSION = "sess-visitor"
STRANGER_SESSION = "sess-stranger"

BURRITO = "CMG-2"
WHITE_RICE = "CMG-5001"
BLACK_BEANS = "CMG-5051"


def burrito() -> dict[str, Any]:
    """A composable Steak Burrito line: both required groups filled."""
    return {
        "item_id": BURRITO,
        "quantity": 1,
        "selections": [
            {"modifier_item_id": WHITE_RICE},
            {"modifier_item_id": BLACK_BEANS},
        ],
    }


@pytest.fixture
def signer() -> GrantSigner:
    """The app's side of the shared secret."""
    return GrantSigner(OPS_KEY)


# ---------------------------------------------------------------------------
# The token itself
# ---------------------------------------------------------------------------


def test_a_grant_round_trips_and_carries_the_arguments_it_was_signed_with(
    signer: GrantSigner,
) -> None:
    """The whole of what a grant is for: the procedure's arguments, signed."""
    arguments: Sequence[Any] = [679, "IN_STORE", [{"item_id": BURRITO, "qty": 1}]]

    minted, token = signer.mint(OpsAction.PLACE_ORDER, VISITOR, "draft-1", arguments)
    verified = signer.verify(
        token,
        action=OpsAction.PLACE_ORDER,
        demo_id=VISITOR,
        reference_id="draft-1",
    )

    assert verified.arguments == list(arguments)
    assert verified.grant_id == minted.grant_id


def test_the_signing_key_is_not_the_ops_key() -> None:
    """The one thing the derivation buys, as a property rather than a claim.

    A proxy log or a mirrored header that captured the bearer secret gives an
    attacker the ability to *call* the ops API. It must not also give them the
    ability to mint a confirmation, and it does not, because the key that signs
    one is never transmitted anywhere.
    """
    assert signing_key(OPS_KEY) != OPS_KEY.encode("utf-8")
    assert signing_key(OPS_KEY) != signing_key(OPS_KEY + "!")


def test_a_grant_signed_with_another_key_does_not_verify(signer: GrantSigner) -> None:
    """Two deployments that disagree about the secret write nothing."""
    _, token = signer.mint(OpsAction.PLACE_ORDER, VISITOR, "draft-1", [])
    somebody_else = GrantSigner("a different shared secret")

    with pytest.raises(GrantRejectedError) as refused:
        somebody_else.verify(
            token,
            action=OpsAction.PLACE_ORDER,
            demo_id=VISITOR,
            reference_id="draft-1",
        )

    assert refused.value.code is GrantCode.INVALID


@pytest.mark.parametrize(
    "mangled",
    ["", "not-a-grant", "a.b", "eyJ2IjogMX0", "eyJ2IjogMX0.notasignature"],
)
def test_anything_that_is_not_a_signed_grant_is_refused(
    signer: GrantSigner, mangled: str
) -> None:
    """No token, a truncated one, an unsigned payload: one answer for all of them."""
    with pytest.raises(GrantRejectedError) as refused:
        signer.verify(
            mangled,
            action=OpsAction.PLACE_ORDER,
            demo_id=VISITOR,
            reference_id="draft-1",
        )

    assert refused.value.code is GrantCode.INVALID


def test_editing_the_payload_breaks_the_signature(signer: GrantSigner) -> None:
    """The property the whole design rests on, attacked directly.

    An attacker who holds a legitimate grant and wants a *different* order has
    to change the arguments inside it, and the arguments are inside the
    signature. There is no field on the wire that reaches a procedure and is not
    covered, which is why the ops API can pass them on unread.
    """
    _, token = signer.mint(OpsAction.PLACE_ORDER, VISITOR, "draft-1", [679, "IN_STORE"])
    body, _, signature = token.partition(".")
    forged = f"{body[:-2]}AA.{signature}"

    with pytest.raises(GrantRejectedError):
        signer.verify(
            forged,
            action=OpsAction.PLACE_ORDER,
            demo_id=VISITOR,
            reference_id="draft-1",
        )


@pytest.mark.parametrize(
    ("action", "demo_id", "reference"),
    [
        (OpsAction.CANCEL_ORDER, VISITOR, "draft-1"),
        (OpsAction.PLACE_ORDER, STRANGER, "draft-1"),
        (OpsAction.PLACE_ORDER, VISITOR, "draft-2"),
    ],
    ids=["another-action", "another-visitor", "another-reference"],
)
def test_a_grant_authorises_exactly_one_write(
    signer: GrantSigner, action: OpsAction, demo_id: str, reference: str
) -> None:
    """Three bindings, three replays closed.

    A grant that verified but was not bound would be a bearer token for *any*
    write, which is precisely what a confirmation must not be. The stranger case
    is the one the live ``another-session`` probe already puts to the deployed
    API through the lookup; this is the same refusal by the other route.
    """
    _, token = signer.mint(OpsAction.PLACE_ORDER, VISITOR, "draft-1", [])

    with pytest.raises(GrantRejectedError) as refused:
        signer.verify(token, action=action, demo_id=demo_id, reference_id=reference)

    assert refused.value.code is GrantCode.INVALID


def test_an_expired_grant_is_refused_and_is_not_an_accusation(
    signer: GrantSigner,
) -> None:
    """The split :data:`chip_chat.api.ops._GATE_VIOLATIONS` draws, at this tier.

    Consent that aged out in flight is a slow request, not an agent skipping a
    step, and the two get different codes so that a launch-gate dashboard is not
    filled with visitors whose warehouse was resuming.
    """
    now = time.time()
    _, token = signer.mint(OpsAction.PLACE_ORDER, VISITOR, "draft-1", [], now=now)

    with pytest.raises(GrantRejectedError) as refused:
        signer.verify(
            token,
            action=OpsAction.PLACE_ORDER,
            demo_id=VISITOR,
            reference_id="draft-1",
            now=now + signer.ttl_seconds + 1.0,
        )

    assert refused.value.code is GrantCode.EXPIRED


def test_the_wire_format_carries_no_algorithm_field(signer: GrantSigner) -> None:
    """Two segments, not three. ``alg: none`` needs a header to live in."""
    _, token = signer.mint(OpsAction.PLACE_ORDER, VISITOR, "draft-1", [])

    assert token.count(".") == 1


# ---------------------------------------------------------------------------
# The gate, through the service, with a grant instead of a lookup
# ---------------------------------------------------------------------------


@pytest.fixture
def spans() -> Iterator[SpanRecorder]:
    """Route the ops spans into memory for the duration of a test."""
    with span_recorder("api") as recorder:
        yield recorder


@pytest.fixture(autouse=True)
def turn(spans: SpanRecorder) -> Iterator[None]:
    """Open the ``tool.*`` span an ``ops.*`` span is required to nest inside."""
    with (
        chat_turn(session_id=SESSION, turn_index=0, message="order it"),
        agent_step(index=0),
        tool_call(ToolName.PLACE_ORDER, arguments={}),
    ):
        yield


@pytest.fixture
def backend() -> RecordingWriteBackend:
    """The stand-in for the Functions app's Snowflake connection."""
    return RecordingWriteBackend()


@pytest.fixture
def ops(
    backend: RecordingWriteBackend, catalog: MenuCatalog, clock: FakeClock
) -> OpsService:
    """The ops API's own service: its *own* stores, and a grant verifier.

    The stores are deliberately fresh and empty, which is the deployed topology
    made literal -- nothing the app minted is in them, so a write that got
    through by looking a record up here would be a write that did not happen.
    """
    return OpsService(
        backend,
        DraftStore(catalog, clock=clock),
        ConfirmationLedger(clock=clock),
        grants=GrantSigner(OPS_KEY),
    )


def test_a_write_with_no_grant_is_refused_by_the_empty_store(
    ops: OpsService, backend: RecordingWriteBackend
) -> None:
    """The unchanged path, stated as the thing the deployed host still does.

    Every probe ``infra/scripts/verify-ops-api.sh`` puts arrives without a
    grant, and this is the answer it gets: the in-process claim, an empty store,
    and a refusal before a Snowflake session is acquired.
    """
    with pytest.raises(OpsRejectedError) as refused:
        ops.session(VISITOR).place_order("draft-nobody-minted")

    assert refused.value.code == "DRAFT_NOT_FOUND"
    assert backend.calls == []


def test_a_forged_grant_is_refused_and_reaches_no_procedure(
    ops: OpsService, backend: RecordingWriteBackend
) -> None:
    """A compromised model that invented a confirmation writes nothing.

    The rejection code is the draft vocabulary's rather than a seventh one --
    see :data:`chip_chat.api.ops.PRECONDITION_REJECTIONS` for why -- and the
    grant's own sentence is what tells an operator which of the two things went
    wrong.
    """
    with pytest.raises(OpsRejectedError) as refused:
        ops.session(VISITOR).place_order("draft-1", "forged.token")

    assert refused.value.code == "DRAFT_NOT_CONFIRMED"
    assert refused.value.code in PRECONDITION_REJECTIONS
    assert "confirmation" in refused.value.detail
    assert backend.calls == []


def test_a_grant_from_another_visitor_is_refused_at_the_service(
    ops: OpsService, backend: RecordingWriteBackend
) -> None:
    """A grant is not a bearer token. Bound at the tier that binds the visitor."""
    _, token = GrantSigner(OPS_KEY).mint(
        OpsAction.PLACE_ORDER, STRANGER, "draft-1", [679, "IN_STORE", []]
    )

    with pytest.raises(OpsRejectedError):
        ops.session(VISITOR).place_order("draft-1", token)

    assert backend.calls == []


def test_a_valid_grant_writes_exactly_what_it_carries(
    ops: OpsService, backend: RecordingWriteBackend
) -> None:
    """The arguments come off the signature and nothing else reaches a procedure."""
    arguments: Sequence[Any] = [679, "IN_STORE", [{"item_id": BURRITO, "qty": 1}]]
    granted, token = GrantSigner(OPS_KEY).mint(
        OpsAction.PLACE_ORDER, VISITOR, "draft-1", arguments
    )

    receipt = ops.session(VISITOR).place_order("draft-1", token)

    assert receipt.reference_id == "draft-1"
    assert len(backend.writes) == 1
    call = backend.writes[0]
    assert call.arguments[0] == granted.grant_id
    assert list(call.arguments[1:]) == list(arguments)


def test_replaying_a_grant_replays_the_receipt_rather_than_writing_twice(
    ops: OpsService, backend: RecordingWriteBackend
) -> None:
    """Why replay needs no shared state, driven.

    The grant's single-use id *is* the retry key, and every procedure spends its
    retry key inside its own transaction. So a captured grant re-presented is
    two calls and one row -- which is the mechanism ``docs/ops-api.md`` already
    describes for a connection that died after committing, doing a second job it
    was already the right shape for.
    """
    _, token = GrantSigner(OPS_KEY).mint(
        OpsAction.PLACE_ORDER, VISITOR, "draft-1", [679, "IN_STORE", []]
    )

    first = ops.session(VISITOR).place_order("draft-1", token)
    second = ops.session(VISITOR).place_order("draft-1", token)

    assert len(backend.calls) == 2
    assert len(backend.writes) == 1
    assert not first.replayed
    assert second.replayed


def test_a_service_with_no_verifier_refuses_a_grant_rather_than_ignoring_it(
    backend: RecordingWriteBackend, catalog: MenuCatalog, clock: FakeClock
) -> None:
    """A deployment missing its shared secret says so instead of saying not-found.

    Ignoring the grant would fall through to the empty in-process store and
    report ``DRAFT_NOT_FOUND`` for a request whose real problem was a missing
    setting, which is the confusing hour ``docs/ops-api.md`` records one version
    of already.
    """
    unkeyed = OpsService(
        backend, DraftStore(catalog, clock=clock), ConfirmationLedger(clock=clock)
    )
    _, token = GrantSigner(OPS_KEY).mint(OpsAction.PLACE_ORDER, VISITOR, "draft-1", [])

    with pytest.raises(OpsRejectedError) as refused:
        unkeyed.session(VISITOR).place_order("draft-1", token)

    assert "CHIP_CHAT_OPS_KEY" in refused.value.detail
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Both tiers at once: the app's desk and the ops API's service, joined
# ---------------------------------------------------------------------------


class _LoopbackClient(OpsClient):
    """The ops API on the other end of the wire, minus the wire.

    Everything :class:`~chip_chat.api.opsclient.OpsClient` would put on the
    request is instead handed to a real :class:`~chip_chat.api.ops.OpsService`
    whose stores are empty -- so the only thing that can make a write succeed is
    the grant. It records what it was given, which is how the test below can
    compare the arguments the app computed with the arguments the procedure was
    called with.
    """

    def __init__(self, ops: OpsService) -> None:
        super().__init__("https://ops.invalid", OPS_KEY, "function-key")
        self._ops = ops
        self.headers: list[Mapping[str, str]] = []

    def available(self) -> bool:
        """Up, and asked without a network."""
        return True

    def write(
        self,
        action: OpsAction,
        *,
        demo_id: str,
        reference: Any,
        confirmation: str,
    ) -> Mapping[str, Any]:
        """Hand the call to the far side exactly as the four routes would."""
        self.headers.append({GRANT_HEADER: confirmation})
        session = self._ops.session(demo_id)
        if action is OpsAction.PLACE_ORDER:
            return session.place_order(reference, confirmation).as_dict()
        if action is OpsAction.CANCEL_ORDER:
            return session.cancel_order(reference, confirmation).as_dict()
        if action is OpsAction.REDEEM_POINTS:
            return session.redeem_points(reference, confirmation).as_dict()
        return session.update_preferences(reference, confirmation).as_dict()


@pytest.fixture
def visitors() -> VisitorDesk:
    """A desk with one bound visitor, so a session resolves to a ``demo_id``."""
    desk = VisitorDesk(_roster(), store=VisitorSessionStore())
    desk.admit(SESSION)
    return desk


def _roster() -> Any:
    """Two populated synthetic customers, both at the fixture catalogue's store.

    ``populated`` is a real precondition rather than a formality --
    :class:`~chip_chat.api.visitors.StaticRoster` discards a fixture with no
    order history, no home store or no points, because an unpopulated one is the
    empty account PRD §06 says loses the demo. So these carry all three.
    """
    from chip_chat.api.visitors import StaticRoster

    return StaticRoster(
        [
            PersonaFixture(
                demo_id=VISITOR,
                persona_id="regular",
                label="The Regular",
                home_store=679,
                points_balance=1_200,
                order_count=14,
            ),
            PersonaFixture(
                demo_id=STRANGER,
                persona_id="lapsed",
                label="The Lapsed Regular",
                home_store=679,
                points_balance=90,
                order_count=3,
            ),
        ]
    )


@pytest.fixture
def desk(
    visitors: VisitorDesk, catalog: MenuCatalog, clock: FakeClock, ops: OpsService
) -> OpsDesk:
    """The app's action lane, with the ops API's service on the far side."""
    return OpsDesk(
        DraftStore(catalog, clock=clock),
        ConfirmationLedger(clock=clock),
        _LoopbackClient(ops),
        GrantSigner(OPS_KEY),
        visitors,
    )


def test_the_action_lane_offers_all_four_writes(desk: OpsDesk) -> None:
    """PRD T1, as the tool list the model is actually shown.

    The week-one desk offers ``place_order`` alone, which is why the live write
    gate could not put its two redemption probes: the suite refuses to read a
    missing lane as a guard.
    """
    offered = offered_tools(desk=desk)

    assert set(DESK_WRITES) <= set(offered)
    assert ToolName.PLACE_ORDER in offered


def test_placing_a_draft_nobody_confirmed_never_reaches_the_far_side(
    desk: OpsDesk, backend: RecordingWriteBackend
) -> None:
    """**The launch gate**, across the boundary.

    The claim happens in this process, where the flag lives, and it raises --
    so no grant is minted, no request is composed, and the write role is never
    asked for anything. The ops API is not consulted and does not have to be.
    """
    from chip_chat.agent.orders import OrderRejectedError

    card = desk.propose(SESSION, [burrito()]).as_card()

    with pytest.raises(OrderRejectedError) as refused:
        desk.place(SESSION, str(card["draft_id"]))

    assert refused.value.code == "DRAFT_NOT_CONFIRMED"
    assert backend.calls == []


def test_a_confirmed_draft_crosses_the_boundary_and_writes_what_was_on_the_card(
    desk: OpsDesk, backend: RecordingWriteBackend
) -> None:
    """The whole path, and the check the two tiers' transcriptions agree.

    The draft is proposed and confirmed in the app's store; the ops API's store
    is empty throughout, so the only thing that could have authorised this write
    is the signature. The final assertion is the one that earns its keep: the
    arguments the *app* built from the claimed record are the arguments the
    procedure was called with, which is what ``_order_arguments`` being written
    twice -- once in each tier -- has to keep true.
    """
    card = desk.propose(SESSION, [burrito()]).as_card()
    draft_id = str(card["draft_id"])
    assert desk.confirm(SESSION, draft_id) is not None

    receipt = desk.place(SESSION, draft_id).as_dict()

    assert len(backend.writes) == 1
    call = backend.writes[0]
    assert call.procedure.endswith("place_order")
    # The store, the channel, and the lines -- read off the card the visitor
    # confirmed rather than off anything that arrived with the call.
    assert call.arguments[1] == card["pricing"]["restaurant_id"]
    assert call.arguments[2] == "IN_STORE"
    assert call.arguments[3] == [
        {"item_id": BURRITO, "qty": 1, "modifiers": [WHITE_RICE, BLACK_BEANS]}
    ]
    assert receipt["reference_id"] == draft_id


def test_a_confirmed_draft_is_placed_once_however_often_it_is_asked_for(
    desk: OpsDesk, backend: RecordingWriteBackend
) -> None:
    """One card, one order. The claim retires the draft as it hands it over."""
    from chip_chat.agent.orders import OrderRejectedError

    card = desk.propose(SESSION, [burrito()]).as_card()
    draft_id = str(card["draft_id"])
    desk.confirm(SESSION, draft_id)
    desk.place(SESSION, draft_id)

    with pytest.raises(OrderRejectedError) as refused:
        desk.place(SESSION, draft_id)

    assert refused.value.code == "DRAFT_NOT_FOUND"
    assert len(backend.writes) == 1


def test_another_sessions_confirm_does_not_confirm_this_visitors_card(
    desk: OpsDesk, visitors: VisitorDesk, backend: RecordingWriteBackend
) -> None:
    """``confirm-a-draft-from-another-session``, at the tier that resolves both.

    The stranger's request carries their own cookie, so it resolves to their own
    ``demo_id``, and the draft store answers *not found* for a well-formed id
    belonging to somebody else -- the same answer a forged id gets, so this is
    not an oracle either.
    """
    from chip_chat.agent.orders import OrderRejectedError

    visitors.admit(STRANGER_SESSION)
    card = desk.propose(SESSION, [burrito()]).as_card()
    draft_id = str(card["draft_id"])

    assert desk.confirm(STRANGER_SESSION, draft_id) is None
    with pytest.raises(OrderRejectedError) as refused:
        desk.place(SESSION, draft_id)

    assert refused.value.code == "DRAFT_NOT_CONFIRMED"
    assert backend.calls == []


def test_the_three_writes_that_name_a_row_offer_a_card_before_they_write(
    desk: OpsDesk, backend: RecordingWriteBackend
) -> None:
    """``cancel_order``, ``redeem_points`` and ``update_preferences``, gated alike.

    The first call finds no confirmation and returns a card; nothing is written.
    This is what makes the live write gate's two redemption probes *questions*
    for the first time: the lane exists, so a refusal is the gate rather than a
    missing door.
    """
    outcomes = [
        desk.act(SESSION, OpsAction.CANCEL_ORDER, {"order_id": "ord-9000001"}),
        desk.act(SESSION, OpsAction.REDEEM_POINTS, {"reward_id": "chips"}),
        desk.act(
            SESSION, OpsAction.UPDATE_PREFERENCES, {"prefs": {"display_name": "Sam"}}
        ),
    ]

    assert [outcome.confirmed for outcome in outcomes] == [False, False, False]
    assert all(outcome.card is not None for outcome in outcomes)
    assert backend.calls == []


def test_confirming_one_of_the_three_lets_the_write_across(
    desk: OpsDesk, backend: RecordingWriteBackend
) -> None:
    """The second half, and the point cost that reaches the procedure.

    ``redeem_points`` sends what was *on the card* rather than what arrived with
    the call, because the rewards terms let a cost change at any time. Here the
    card quotes nothing -- no account lane is wired in this test -- and null is
    the procedure's *skip the check*, which is the honest answer rather than an
    invented figure.
    """
    offered = desk.act(SESSION, OpsAction.REDEEM_POINTS, {"reward_id": "chips"})
    assert offered.card is not None
    confirmation_id = str(offered.card["confirmation_id"])
    assert desk.confirm(SESSION, confirmation_id) is not None

    written = desk.act(SESSION, OpsAction.REDEEM_POINTS, {"reward_id": "chips"})

    assert written.confirmed
    assert len(backend.writes) == 1
    assert backend.writes[0].arguments[1] == "chips"


def test_an_ops_api_that_is_down_refuses_the_write_and_says_the_specified_thing(
    desk: OpsDesk, visitors: VisitorDesk, catalog: MenuCatalog, clock: FakeClock
) -> None:
    """RFC-001 §10's row for this service, at the tier a visitor meets.

    Blast radius is writes only, nothing is half-written, and the sentence the
    model is handed is the one the specification gives rather than one it
    composes.
    """
    from chip_chat.agent.orders import OrderRejectedError
    from chip_chat.api.ops import OPS_UNAVAILABLE_MESSAGE, OpsUnavailableError

    class _Down(_LoopbackClient):
        def available(self) -> bool:
            return False

        def write(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
            raise OpsUnavailableError("the recording backend is down")

    drafts = DraftStore(catalog, clock=clock)
    down = OpsDesk(
        drafts,
        ConfirmationLedger(clock=clock),
        _Down(
            OpsService(
                RecordingWriteBackend(),
                DraftStore(catalog, clock=clock),
                ConfirmationLedger(clock=clock),
                grants=GrantSigner(OPS_KEY),
            )
        ),
        GrantSigner(OPS_KEY),
        visitors,
    )

    card = down.propose(SESSION, [burrito()]).as_card()
    assert card["ordering_available"] is False
    assert card["unavailable_message"] == OPS_UNAVAILABLE_MESSAGE

    down.confirm(SESSION, str(card["draft_id"]))
    with pytest.raises(OrderRejectedError) as refused:
        down.place(SESSION, str(card["draft_id"]))

    assert refused.value.code == OPS_DECLINED
    assert refused.value.message == OPS_UNAVAILABLE_MESSAGE


def test_a_grant_is_the_only_new_thing_on_the_wire(signer: GrantSigner) -> None:
    """No visitor identifier became a field a model can reach.

    Invariant 1, checked at the one tier this change added. The grant is a
    header composed server-to-server, and the ``demo_id`` inside it is signed
    rather than supplied -- there is no argument on any of the four routes, and
    none on any of the eleven tools, through which a caller could name somebody.
    """
    _, token = signer.mint(OpsAction.PLACE_ORDER, VISITOR, "draft-1", [])
    verified: Grant = signer.verify(
        token,
        action=OpsAction.PLACE_ORDER,
        demo_id=VISITOR,
        reference_id="draft-1",
    )

    assert verified.demo_id == VISITOR
    assert GRANT_HEADER.startswith("x-cilantro-")


# ---------------------------------------------------------------------------
# The request the client actually composes
# ---------------------------------------------------------------------------


class _RecordingClient(OpsClient):
    """An ops client that records the body and headers instead of sending them.

    Subclassed at :meth:`OpsClient._post` rather than at
    :meth:`OpsClient.write`, so everything above it -- the body field each route
    reads, the two keys, the visitor header, the grant header -- is the real
    code path. That is the point: the bug this class was written for was in the
    body, and a double that stubbed ``write`` would have had nothing to say
    about it.
    """

    def __init__(self) -> None:
        super().__init__("https://ops.invalid", OPS_KEY, "function-key")
        self.sent: list[tuple[str, Mapping[str, Any], Mapping[str, str]]] = []

    def available(self) -> bool:
        return True

    def _post(
        self,
        route: str,
        body: Mapping[str, Any],
        demo_id: str,
        confirmation: str,
        *,
        traced: bool = True,
    ) -> tuple[int, Mapping[str, Any]]:
        self.sent.append(
            (route, dict(body), self._headers(demo_id, confirmation, traced=False))
        )
        return 200, {"ok": True, "receipt": {"ok": True, "route": route}}


@pytest.fixture
def recorder(
    visitors: VisitorDesk, catalog: MenuCatalog, clock: FakeClock
) -> tuple[OpsDesk, _RecordingClient]:
    """A desk over a client that records what it would have sent."""
    client = _RecordingClient()
    desk = OpsDesk(
        DraftStore(catalog, clock=clock),
        ConfirmationLedger(clock=clock),
        client,
        GrantSigner(OPS_KEY),
        visitors,
    )
    return desk, client


def test_every_route_is_sent_the_field_it_reads(
    recorder: tuple[OpsDesk, _RecordingClient],
) -> None:
    """Four routes, four body fields, and the fourth is not like the others.

    ``update_preferences`` is keyed by a *digest* of what was shown and its body
    must be the preferences **object**, because the ops API recomputes that
    digest from the body and checks the grant against the result. Sending the
    digest would be sending a string to a route that requires an object, and the
    write would be refused as malformed one layer away from anything explaining
    why -- which is exactly what it did before this test existed.
    """
    desk, client = recorder
    prefs = {"display_name": "Sam"}
    for action, arguments in (
        (OpsAction.CANCEL_ORDER, {"order_id": "ord-9000001"}),
        (OpsAction.REDEEM_POINTS, {"reward_id": "chips"}),
        (OpsAction.UPDATE_PREFERENCES, {"prefs": prefs}),
    ):
        offered = desk.act(SESSION, action, arguments)
        assert offered.card is not None
        desk.confirm(SESSION, str(offered.card["confirmation_id"]))
        assert desk.act(SESSION, action, arguments).confirmed

    sent = {route: body for route, body, _ in client.sent}
    assert sent["cancel_order"] == {"order_id": "ord-9000001"}
    assert sent["redeem_points"] == {"reward_id": "chips"}
    assert sent["update_preferences"] == {"prefs": prefs}


def test_the_order_route_is_sent_the_draft_id_and_the_grant(
    recorder: tuple[OpsDesk, _RecordingClient], visitors: VisitorDesk
) -> None:
    """And the four headers that make the write answerable at all.

    The visitor is read back off the desk rather than written down, because the
    roster assigns one of two archetypes at random -- which is
    :meth:`VisitorDesk.admit` doing its job, and a test that pinned the answer
    would be pinning the shuffle.
    """
    desk, client = recorder
    bound = visitors.visitor(SESSION)
    assert bound is not None
    card = desk.propose(SESSION, [burrito()]).as_card()
    draft_id = str(card["draft_id"])
    desk.confirm(SESSION, draft_id)

    desk.place(SESSION, draft_id)

    route, body, headers = client.sent[-1]
    assert route == "place_order"
    assert body == {"draft_id": draft_id}
    assert headers["x-cilantro-session"] == bound.demo_id
    assert headers["x-functions-key"] == "function-key"
    assert headers["x-cilantro-ops-key"] == OPS_KEY
    # And the grant verifies against exactly the write it accompanies.
    granted = GrantSigner(OPS_KEY).verify(
        headers[GRANT_HEADER],
        action=OpsAction.PLACE_ORDER,
        demo_id=bound.demo_id,
        reference_id=draft_id,
    )
    assert granted.arguments[0] == card["pricing"]["restaurant_id"]


def test_the_visitor_never_reaches_the_body(
    recorder: tuple[OpsDesk, _RecordingClient], visitors: VisitorDesk
) -> None:
    """Invariant 1, at the last tier where an identifier exists at all.

    The ``demo_id`` travels on a header the app composes server-to-server. It is
    never a body field, because a body field is the shape a model-named value
    has, and the four routes' bodies carry only what the visitor was shown.
    """
    desk, client = recorder
    bound = visitors.visitor(SESSION)
    assert bound is not None
    card = desk.propose(SESSION, [burrito()]).as_card()
    desk.confirm(SESSION, str(card["draft_id"]))
    desk.place(SESSION, str(card["draft_id"]))

    for _, body, _ in client.sent:
        assert bound.demo_id not in json.dumps(body)
