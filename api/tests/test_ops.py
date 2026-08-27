"""The second launch gate: zero writes without explicit confirmation.

Issue #63's acceptance criteria are the section headings below, and they are
asserted *against the service*, not through the app -- "tested directly against
the API, bypassing the UI" is the criterion's own wording, and it is the only
form of the test that proves the rule is in the code rather than in a template.

The double these lean on is :class:`~chip_chat.api.testing.RecordingWriteBackend`,
and the assertion that matters is nearly always about ``backend.writes`` rather
than about what came back. A refusal that returns the right sentence while still
writing a row is the failure this gate exists to prevent, and only the backend
can tell the two apart.
"""

import inspect
from collections.abc import Iterator, Mapping
from typing import Any, cast

import pytest

from chip_chat.api.confirmations import ConfirmationCode, ConfirmationLedger
from chip_chat.api.drafts import DraftStore, RejectionCode
from chip_chat.api.ops import (
    OPS_UNAVAILABLE_MESSAGE,
    PRECONDITION_REJECTIONS,
    OpsRejectedError,
    OpsService,
    OpsSession,
    OpsUnavailableError,
    offer_cancellation,
    offer_preferences,
    offer_redemption,
    unavailable_card,
)
from chip_chat.api.testing import FakeClock, RecordingWriteBackend
from chip_chat.otel import ConfirmationState, OpsAction, ToolName
from chip_chat.otel.attributes import ChipChatAttributes
from chip_chat.otel.spans import agent_step, chat_turn, tool_call
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.snowflake.procedures import IDENTITY_VOCABULARY, PROCEDURES, procedure

VISITOR = "dm-000001"
STRANGER = "dm-000002"

BURRITO = "CMG-2"
WHITE_RICE = "CMG-5001"
BLACK_BEANS = "CMG-5051"

ORDER = "ord-9000001"
REWARD = "chips"
PREFS: Mapping[str, Any] = {"display_name": "Sam", "home_store": 679}


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
def backend() -> RecordingWriteBackend:
    """The stand-in for the Functions app's Snowflake connection."""
    return RecordingWriteBackend()


@pytest.fixture
def confirmations(clock: FakeClock) -> ConfirmationLedger:
    """The ledger the three writes without a draft claim from."""
    return ConfirmationLedger(clock=clock)


@pytest.fixture
def ops(
    backend: RecordingWriteBackend,
    drafts: DraftStore,
    confirmations: ConfirmationLedger,
) -> OpsService:
    """The service under test, wired to doubles and a driven clock."""
    return OpsService(backend, drafts, confirmations)


@pytest.fixture
def session(ops: OpsService) -> OpsSession:
    """One visitor's write handle."""
    return ops.session(VISITOR)


@pytest.fixture
def spans() -> Iterator[SpanRecorder]:
    """Route the ops spans into memory for the duration of a test."""
    with span_recorder("api") as recorder:
        yield recorder


@pytest.fixture(autouse=True)
def turn(spans: SpanRecorder) -> Iterator[None]:
    """Open the span an ``ops.*`` span is required to nest inside.

    The schema makes ``ops.*`` a child of ``tool.*`` and refuses to open one
    anywhere else, which is not this module's rule to relax: in the deployed
    system the Functions host rejoins the agent's ``tool.<name>`` span from the
    trace context on the request, and in V0 the agent's tool executor is already
    inside one. So every test here runs inside that tree. Which of the four
    tools holds it is immaterial to what is being asserted, so they all use one.
    """
    with (
        chat_turn(session_id="s-1", turn_index=0, message="order it"),
        agent_step(index=0),
        tool_call(ToolName.PLACE_ORDER, arguments={}),
    ):
        yield


def confirmed_draft(drafts: DraftStore, demo_id: str = VISITOR) -> str:
    """Propose and confirm one draft, and return its id."""
    draft = drafts.propose(demo_id, [burrito()])
    drafts.confirm(demo_id, draft.draft_id)
    return draft.draft_id


# ---------------------------------------------------------------------------
# An unconfirmed draft_id is rejected -- tested directly against the API
# ---------------------------------------------------------------------------


def test_an_unconfirmed_draft_is_rejected(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    draft = drafts.propose(VISITOR, [burrito()])

    with pytest.raises(OpsRejectedError) as refused:
        session.place_order(draft.draft_id)

    assert refused.value.code == RejectionCode.DRAFT_NOT_CONFIRMED.value
    assert backend.writes == []


def test_an_unconfirmed_draft_reaches_no_procedure_at_all(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    """Not merely no write: the database is never asked.

    The confirmation flag lives in the app tier, so a database consulted about
    it would be a database that could be persuaded. ``calls`` being empty is the
    stronger statement, and it is the one the gate needs.
    """
    draft = drafts.propose(VISITOR, [burrito()])
    with pytest.raises(OpsRejectedError):
        session.place_order(draft.draft_id)

    assert backend.calls == []


def test_a_draft_id_nobody_minted_is_rejected(
    session: OpsSession, backend: RecordingWriteBackend
) -> None:
    with pytest.raises(OpsRejectedError) as refused:
        session.place_order("draft-invented-by-the-model")

    assert refused.value.code == RejectionCode.DRAFT_NOT_FOUND.value
    assert backend.writes == []


def test_an_expired_draft_is_rejected(
    session: OpsSession, drafts: DraftStore, clock: FakeClock
) -> None:
    draft_id = confirmed_draft(drafts)
    clock.advance(901.0)

    with pytest.raises(OpsRejectedError) as refused:
        session.place_order(draft_id)

    assert refused.value.code == RejectionCode.DRAFT_EXPIRED.value


def test_a_confirmed_draft_is_placed(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    receipt = session.place_order(confirmed_draft(drafts))

    assert receipt.action is OpsAction.PLACE_ORDER
    assert len(backend.writes) == 1
    assert backend.writes[0].name == "place_order"


def test_the_three_writes_without_a_draft_are_gated_the_same_way(
    session: OpsSession, backend: RecordingWriteBackend
) -> None:
    """The rule is not special to orders; the record it reads is."""
    for write in (
        lambda: session.cancel_order(ORDER),
        lambda: session.redeem_points(REWARD),
        lambda: session.update_preferences(PREFS),
    ):
        with pytest.raises(OpsRejectedError) as refused:
            write()
        assert refused.value.code == ConfirmationCode.NOT_FOUND.value

    assert backend.writes == []


def test_an_offered_but_unconfirmed_card_is_the_launch_gate(
    session: OpsSession, confirmations: ConfirmationLedger, backend: RecordingWriteBackend
) -> None:
    offer_cancellation(confirmations, VISITOR, ORDER)

    with pytest.raises(OpsRejectedError) as refused:
        session.cancel_order(ORDER)

    assert refused.value.code == ConfirmationCode.NOT_CONFIRMED.value
    assert backend.calls == []


def test_confirming_the_card_lets_the_write_through(
    session: OpsSession, confirmations: ConfirmationLedger, backend: RecordingWriteBackend
) -> None:
    record = offer_cancellation(confirmations, VISITOR, ORDER)
    confirmations.confirm(VISITOR, record.confirmation_id)

    receipt = session.cancel_order(ORDER)

    assert receipt.reference_id == ORDER
    assert [call.name for call in backend.writes] == ["cancel_order"]


# ---------------------------------------------------------------------------
# A confirmed draft from another session is rejected
# ---------------------------------------------------------------------------


def test_another_visitors_confirmed_draft_is_rejected(
    ops: OpsService, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    """And is a not-found rather than a forbidden: section 7, first bullet."""
    draft_id = confirmed_draft(drafts, STRANGER)

    with pytest.raises(OpsRejectedError) as refused:
        ops.session(VISITOR).place_order(draft_id)

    assert refused.value.code == RejectionCode.DRAFT_NOT_FOUND.value
    assert backend.writes == []


def test_another_visitors_confirmed_card_is_rejected(
    ops: OpsService, confirmations: ConfirmationLedger, backend: RecordingWriteBackend
) -> None:
    record = offer_redemption(
        confirmations, STRANGER, REWARD, name="Chips", point_cost=1_000
    )
    confirmations.confirm(STRANGER, record.confirmation_id)

    with pytest.raises(OpsRejectedError) as refused:
        ops.session(VISITOR).redeem_points(REWARD)

    assert refused.value.code == ConfirmationCode.NOT_FOUND.value
    assert backend.writes == []


def test_the_stranger_can_still_place_their_own_draft(
    ops: OpsService, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    """The refusal is about whose draft it is, not about the draft."""
    draft_id = confirmed_draft(drafts, STRANGER)
    ops.session(STRANGER).place_order(draft_id)

    assert [call.demo_id for call in backend.writes] == [STRANGER]


# ---------------------------------------------------------------------------
# Retrying with the same idempotency key produces one write
# ---------------------------------------------------------------------------


def test_the_retry_key_is_the_record_the_visitor_was_shown(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    """Never the caller's: a key a caller mints is a key a caller can vary."""
    draft_id = confirmed_draft(drafts)
    session.place_order(draft_id)

    assert backend.writes[0].retry_key == draft_id


def test_a_dropped_connection_after_the_commit_still_writes_once(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    """The case the retry key exists for, per ``sql/12_procedures.sql``.

    The procedure commits and spends its key, then the connection dies before
    the caller hears anything. The retry carries the same key, finds the stored
    receipt and replays it -- one order, and the visitor is told about it.
    """
    backend.commit_then_fail()
    receipt = session.place_order(confirmed_draft(drafts))

    assert len(backend.calls) == 2
    assert len(backend.writes) == 1
    assert receipt.replayed is True


def test_both_attempts_carry_the_same_key(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    backend.commit_then_fail()
    session.place_order(confirmed_draft(drafts))

    assert len({call.retry_key for call in backend.calls}) == 1


def test_replaying_a_receipt_is_not_a_second_write(
    session: OpsSession, confirmations: ConfirmationLedger, backend: RecordingWriteBackend
) -> None:
    record = offer_preferences(confirmations, VISITOR, PREFS)
    confirmations.confirm(VISITOR, record.confirmation_id)
    backend.commit_then_fail()

    session.update_preferences(PREFS)

    assert len(backend.writes) == 1


def test_a_claimed_draft_cannot_be_placed_a_second_time(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    """At most one order per draft, whatever the caller does afterwards."""
    draft_id = confirmed_draft(drafts)
    session.place_order(draft_id)

    with pytest.raises(OpsRejectedError) as refused:
        session.place_order(draft_id)

    assert refused.value.code == RejectionCode.DRAFT_NOT_FOUND.value
    assert len(backend.writes) == 1


def test_a_claimed_card_cannot_be_spent_a_second_time(
    session: OpsSession, confirmations: ConfirmationLedger, backend: RecordingWriteBackend
) -> None:
    record = offer_cancellation(confirmations, VISITOR, ORDER)
    confirmations.confirm(VISITOR, record.confirmation_id)
    session.cancel_order(ORDER)

    with pytest.raises(OpsRejectedError):
        session.cancel_order(ORDER)

    assert len(backend.writes) == 1


# ---------------------------------------------------------------------------
# Taking the Functions app down leaves the read lanes working
# ---------------------------------------------------------------------------


def test_an_unavailable_write_path_says_so(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    draft_id = confirmed_draft(drafts)
    backend.take_down()

    with pytest.raises(OpsUnavailableError) as down:
        session.place_order(draft_id)

    assert down.value.message == OPS_UNAVAILABLE_MESSAGE


def test_an_unavailable_write_path_writes_nothing(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    """Nothing is half-written: RFC-001 section 10, the ops API's row."""
    draft_id = confirmed_draft(drafts)
    backend.take_down()

    with pytest.raises(OpsUnavailableError):
        session.place_order(draft_id)

    assert backend.writes == []


def test_the_service_reports_availability_before_a_card_is_rendered(
    ops: OpsService, backend: RecordingWriteBackend
) -> None:
    """Which is what lets the card render *and* report that ordering is off."""
    assert ops.available() is True
    backend.take_down()
    assert ops.available() is False


def test_the_card_still_renders_and_reports_the_outage(drafts: DraftStore) -> None:
    draft = drafts.propose(VISITOR, [burrito()])
    card = unavailable_card(drafts.card(draft))

    assert card["ordering_available"] is False
    assert card["unavailable_message"] == OPS_UNAVAILABLE_MESSAGE
    assert card["draft_id"] == draft.draft_id
    assert card["total"] == drafts.card(draft)["total"]


def test_the_outage_message_is_not_the_budget_stop_state() -> None:
    """Two different states. One is a failure and one is a designed stop."""
    from chip_chat.api.outcome import STOP_STATE_MESSAGE

    assert OPS_UNAVAILABLE_MESSAGE != STOP_STATE_MESSAGE
    assert "ordering" in OPS_UNAVAILABLE_MESSAGE.lower()


def test_a_transport_failure_is_retried_before_the_path_is_called_down(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    backend.fail_calls(1)
    receipt = session.place_order(confirmed_draft(drafts))

    assert len(backend.calls) == 2
    assert receipt.replayed is False


def test_a_write_path_that_stays_down_is_called_down(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    backend.fail_calls(5)
    with pytest.raises(OpsUnavailableError):
        session.place_order(confirmed_draft(drafts))

    assert len(backend.calls) == 2


# ---------------------------------------------------------------------------
# Every write emits ops.<action> with its confirmation state
# ---------------------------------------------------------------------------


def test_a_confirmed_write_emits_its_span(
    session: OpsSession, drafts: DraftStore, spans: SpanRecorder
) -> None:
    draft_id = confirmed_draft(drafts)
    session.place_order(draft_id)

    attributes = spans.attributes_of("ops.place_order")
    assert attributes[ChipChatAttributes.OPS_ACTION] == OpsAction.PLACE_ORDER.value
    assert attributes[ChipChatAttributes.OPS_REFERENCE_ID] == draft_id
    assert (
        attributes[ChipChatAttributes.OPS_CONFIRMATION_STATE]
        == ConfirmationState.CONFIRMED
    )


def test_a_refused_write_emits_its_span_too(
    session: OpsSession, drafts: DraftStore, spans: SpanRecorder
) -> None:
    """A turn that quietly emitted no span would hide the launch-gate failure."""
    draft = drafts.propose(VISITOR, [burrito()])
    with pytest.raises(OpsRejectedError):
        session.place_order(draft.draft_id)

    attributes = spans.attributes_of("ops.place_order")
    assert (
        attributes[ChipChatAttributes.OPS_CONFIRMATION_STATE]
        == ConfirmationState.REJECTED
    )


def test_a_rejected_confirmation_marks_the_span_failed(
    session: OpsSession, drafts: DraftStore, spans: SpanRecorder
) -> None:
    draft = drafts.propose(VISITOR, [burrito()])
    with pytest.raises(OpsRejectedError):
        session.place_order(draft.draft_id)

    assert spans.span_named("ops.place_order").status.is_ok is False


def test_an_expired_card_is_not_recorded_as_an_agent_violation(
    session: OpsSession, drafts: DraftStore, clock: FakeClock, spans: SpanRecorder
) -> None:
    """Consent that aged out is not an accusation. See ``_GATE_VIOLATIONS``."""
    draft_id = confirmed_draft(drafts)
    clock.advance(901.0)

    with pytest.raises(OpsRejectedError):
        session.place_order(draft_id)

    assert (
        spans.attributes_of("ops.place_order")[ChipChatAttributes.OPS_CONFIRMATION_STATE]
        == ConfirmationState.UNCONFIRMED
    )


def test_every_action_emits_its_own_span_name(
    session: OpsSession,
    drafts: DraftStore,
    confirmations: ConfirmationLedger,
    spans: SpanRecorder,
) -> None:
    draft_id = confirmed_draft(drafts)
    for record in (
        offer_cancellation(confirmations, VISITOR, ORDER),
        offer_redemption(confirmations, VISITOR, REWARD, name="Chips", point_cost=1_000),
        offer_preferences(confirmations, VISITOR, PREFS),
    ):
        confirmations.confirm(VISITOR, record.confirmation_id)

    session.place_order(draft_id)
    session.cancel_order(ORDER)
    session.redeem_points(REWARD)
    session.update_preferences(PREFS)

    assert {name for name in spans.names() if name.startswith("ops.")} == {
        f"ops.{action.value}" for action in OpsAction
    }


# ---------------------------------------------------------------------------
# What reaches the procedure is what the visitor confirmed
# ---------------------------------------------------------------------------


def test_the_lines_written_are_the_lines_on_the_card(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    draft = drafts.propose(VISITOR, [burrito()])
    drafts.confirm(VISITOR, draft.draft_id)
    session.place_order(draft.draft_id)

    lines = cast(list[dict[str, Any]], backend.writes[0].arguments[3])
    assert [line["item_id"] for line in lines] == [BURRITO]
    modifiers = lines[0]["modifiers"]
    assert WHITE_RICE in modifiers
    assert BLACK_BEANS in modifiers


def test_the_order_is_priced_at_the_store_it_was_drafted_at(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    draft = drafts.propose(VISITOR, [burrito()])
    drafts.confirm(VISITOR, draft.draft_id)
    session.place_order(draft.draft_id)

    store_id, channel, _ = backend.writes[0].arguments[1:]
    assert store_id == draft.restaurant_id
    assert channel == "IN_STORE"


def test_a_delivery_draft_is_written_in_the_delivery_channel(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    """The channel is not a label; it is which price list priced the order."""
    draft = drafts.propose(VISITOR, [burrito()], order_type="delivery")
    drafts.confirm(VISITOR, draft.draft_id)
    session.place_order(draft.draft_id)

    assert backend.writes[0].arguments[2] == "DELIVERY"


def test_the_point_cost_written_is_the_one_that_was_on_the_card(
    session: OpsSession, confirmations: ConfirmationLedger, backend: RecordingWriteBackend
) -> None:
    """A cost read when the card was rendered is a quote; section 7.3 rule 3."""
    record = offer_redemption(
        confirmations, VISITOR, REWARD, name="Chips", point_cost=1_000
    )
    confirmations.confirm(VISITOR, record.confirmation_id)
    session.redeem_points(REWARD)

    assert backend.writes[0].arguments[1:] == (REWARD, 1_000)


def test_editing_the_preferences_after_confirmation_finds_no_card(
    session: OpsSession, confirmations: ConfirmationLedger, backend: RecordingWriteBackend
) -> None:
    """The card's content is its identifier, so a changed field is a new card."""
    record = offer_preferences(confirmations, VISITOR, PREFS)
    confirmations.confirm(VISITOR, record.confirmation_id)

    with pytest.raises(OpsRejectedError) as refused:
        session.update_preferences({**PREFS, "home_store": 999})

    assert refused.value.code == ConfirmationCode.NOT_FOUND.value
    assert backend.writes == []


def test_the_preferences_written_are_the_cards_copy(
    session: OpsSession, confirmations: ConfirmationLedger, backend: RecordingWriteBackend
) -> None:
    record = offer_preferences(confirmations, VISITOR, PREFS)
    confirmations.confirm(VISITOR, record.confirmation_id)
    session.update_preferences(PREFS)

    assert backend.writes[0].arguments[1] == dict(PREFS)


# ---------------------------------------------------------------------------
# No write takes a visitor identifier, and no SQL is written here
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method",
    ["place_order", "cancel_order", "redeem_points", "update_preferences"],
)
def test_no_write_method_names_a_visitor(method: str) -> None:
    """The same absence ``IDENTITY_VOCABULARY`` names one tier down."""
    signature = inspect.signature(getattr(OpsSession, method))
    for name in signature.parameters:
        if name == "self":
            continue
        assert name.lower() not in IDENTITY_VOCABULARY


def test_the_identity_is_bound_once_and_never_after(ops: OpsService) -> None:
    """``session`` is the last place in the write path an identifier appears."""
    assert "demo_id" in inspect.signature(OpsService.session).parameters


def test_a_write_for_nobody_is_a_wiring_bug(ops: OpsService) -> None:
    with pytest.raises(ValueError, match="visitor"):
        ops.session("")


@pytest.mark.parametrize("declaration", PROCEDURES, ids=lambda d: d.name)
def test_every_declared_procedure_is_reachable(declaration: Any) -> None:
    """Four actions, four procedures, and the ops API knows no fifth."""
    assert declaration.name in {action.value for action in OpsAction}


@pytest.mark.parametrize(
    "action",
    list(OpsAction),
    ids=lambda action: action.value,
)
def test_each_action_calls_its_own_procedure(
    action: OpsAction,
    session: OpsSession,
    drafts: DraftStore,
    confirmations: ConfirmationLedger,
    backend: RecordingWriteBackend,
) -> None:
    if action is OpsAction.PLACE_ORDER:
        session.place_order(confirmed_draft(drafts))
    else:
        reference = {
            OpsAction.CANCEL_ORDER: ORDER,
            OpsAction.REDEEM_POINTS: REWARD,
        }.get(action)
        if reference is None:
            record = offer_preferences(confirmations, VISITOR, PREFS)
            confirmations.confirm(VISITOR, record.confirmation_id)
            session.update_preferences(PREFS)
        else:
            record = confirmations.offer(VISITOR, action, reference, {"point_cost": 1})
            confirmations.confirm(VISITOR, record.confirmation_id)
            getattr(session, action.value)(reference)

    assert backend.writes[0].procedure == procedure(action.value).qualified()


@pytest.mark.parametrize(
    "action",
    list(OpsAction),
    ids=lambda action: action.value,
)
def test_the_argument_count_matches_the_declaration(
    action: OpsAction,
    session: OpsSession,
    drafts: DraftStore,
    confirmations: ConfirmationLedger,
    backend: RecordingWriteBackend,
) -> None:
    """A procedure that grows an argument fails here rather than in Snowflake."""
    if action is OpsAction.PLACE_ORDER:
        session.place_order(confirmed_draft(drafts))
    elif action is OpsAction.UPDATE_PREFERENCES:
        record = offer_preferences(confirmations, VISITOR, PREFS)
        confirmations.confirm(VISITOR, record.confirmation_id)
        session.update_preferences(PREFS)
    else:
        reference = ORDER if action is OpsAction.CANCEL_ORDER else REWARD
        record = confirmations.offer(VISITOR, action, reference, {"point_cost": 1})
        confirmations.confirm(VISITOR, record.confirmation_id)
        getattr(session, action.value)(reference)

    declared = procedure(action.value).arguments
    assert len(backend.writes[0].arguments) == len(declared)
    assert declared[0].name == "RETRY_KEY"


def test_the_precondition_codes_are_this_tiers_and_not_the_databases() -> None:
    """The database is never asked whether the visitor confirmed."""
    declared: set[str] = set()
    for declaration in PROCEDURES:
        declared.update(declaration.all_rejections())

    assert declared.isdisjoint(PRECONDITION_REJECTIONS)


# ---------------------------------------------------------------------------
# A rejection from the procedure is a refusal, never a repaired call
# ---------------------------------------------------------------------------


def test_a_procedure_rejection_becomes_a_typed_refusal(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    backend.reject_next("ITEM_UNAVAILABLE_AT_STORE", "we do not have that here")

    with pytest.raises(OpsRejectedError) as refused:
        session.place_order(confirmed_draft(drafts))

    assert refused.value.code == "ITEM_UNAVAILABLE_AT_STORE"
    assert refused.value.as_result()["ok"] is False
    assert backend.writes == []


def test_a_procedure_rejection_is_not_retried(
    session: OpsSession, drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    """A refusal is an answer. Asking again is how a rejection becomes a write."""
    backend.reject_next("QUANTITY_EXCEEDS_MAX")

    with pytest.raises(OpsRejectedError):
        session.place_order(confirmed_draft(drafts))

    assert len(backend.calls) == 1


def test_the_receipt_is_the_procedures_own_object(
    session: OpsSession, drafts: DraftStore
) -> None:
    """Not re-derived here: a receipt rebuilt in this tier can disagree."""
    receipt = session.place_order(confirmed_draft(drafts))
    rendered = receipt.as_dict()

    assert rendered["simulation"].startswith("Simulated")
    assert rendered["reference_id"] == receipt.reference_id


def test_a_service_cannot_be_built_without_somewhere_to_read_consent_from(
    backend: RecordingWriteBackend,
) -> None:
    """Both ledgers are required positionally: there is no ungated wiring."""
    parameters = inspect.signature(OpsService.__init__).parameters
    for required in ("drafts", "confirmations"):
        assert parameters[required].default is inspect.Parameter.empty


def test_a_service_needs_at_least_one_attempt(
    backend: RecordingWriteBackend,
    drafts: DraftStore,
    confirmations: ConfirmationLedger,
) -> None:
    with pytest.raises(ValueError, match="attempt"):
        OpsService(backend, drafts, confirmations, attempts=0)
