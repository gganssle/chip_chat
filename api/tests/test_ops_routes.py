"""The write path driven through its own routes, which is where the gate is bought.

Issue #63's acceptance criteria say *tested directly against the API, bypassing
the UI*, and the repository had two halves of that and not the whole.
``api/tests/test_ops.py`` drives :class:`~chip_chat.api.ops.OpsService`, which is
one layer inside the edge; ``api/tests/test_ops_host.py`` reads
``api/functions/function_app.py`` as text, which establishes its shape and
nothing about what it does. Between them sat the layer a caller actually meets:
the key check, the body parse, the session header, the trace-context refusal,
the mapping of a rejection onto a 200 and an outage onto a 503.

That layer is where a gate is lost. Not by deleting a rule -- the rules are in
``ops.py`` and they are tested -- but by an edge that never reaches them: a
route that catches the wrong exception, a 500 where a 200 carrying a rejection
belongs, a service resolved before the caller was authenticated. Every one of
those leaves ``test_ops.py`` green.

So these tests call the route functions. ``api/tests/azure_functions_stub.py``
explains why that is possible without the SDK in the lockfile and what the stub
refuses to make easier than the real thing. What is exercised is
``function_app.py`` itself, unmodified, with a real
:class:`~chip_chat.api.ops.OpsService` behind it and
:class:`~chip_chat.api.testing.RecordingWriteBackend` where Snowflake would be.

**Nearly every assertion here is about ``backend.writes``.** A refusal that
returns the right sentence while still writing a row is the failure this gate
exists to prevent, and the response body cannot tell the two apart.

WHAT IS STILL NOT COVERED HERE, SO THAT NOBODY READS MORE INTO IT. The Functions
worker's own dispatch and its ``FUNCTION`` auth level are Azure's code. The
Snowflake driver is exercised nowhere in this workspace, by the same argument
``chip_chat.snowflake.snow`` makes about shelling out to the CLI. And
``func-chip-chat-ops-4cy39i`` holds no deployment yet: :func:`build_ops_service`
refuses to come up without a catalogue, which is #66's loader, so what a live
probe of that host would establish today is the 503 -- which is the one thing
below that also has a Terraform resource behind it rather than only a test.
"""

import importlib.util
import json
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

# `api/tests` is not on mypy's path -- adding it would collide this package's
# `conftest` with `harvest/tests`' -- and pytest puts it on `sys.path` at run
# time, which is the same arrangement every other tests directory here uses.
import azure_functions_stub  # type: ignore[import-not-found]
import pytest

from chip_chat.api.confirmations import ConfirmationLedger
from chip_chat.api.drafts import DraftStore
from chip_chat.api.ops import (
    OPS_UNAVAILABLE_MESSAGE,
    SESSION_HEADER,
    OpsService,
    offer_cancellation,
    offer_preferences,
    offer_redemption,
)
from chip_chat.api.testing import FakeClock, RecordingWriteBackend
from chip_chat.otel import ConfirmationState, OpsAction, ToolName, turn_context_headers
from chip_chat.otel.attributes import ChipChatAttributes
from chip_chat.otel.spans import agent_step, chat_turn, tool_call
from chip_chat.otel.testing import SpanRecorder, span_recorder

VISITOR = "dm-000001"
STRANGER = "dm-000002"

BURRITO = "CMG-2"
WHITE_RICE = "CMG-5001"
BLACK_BEANS = "CMG-5051"

ORDER = "ord-9000001"
REWARD = "chips"
PREFS: Mapping[str, Any] = {"display_name": "Sam", "home_store": 679}

OPS_KEY = "a-shared-secret-the-app-presents"
OPS_KEY_HEADER = "x-cilantro-ops-key"


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


# ---------------------------------------------------------------------------
# The host, imported the way a worker would load it
# ---------------------------------------------------------------------------


def _load_host() -> ModuleType:
    """Import ``api/functions/function_app.py`` under its own name.

    Loaded from its path rather than as a workspace module because it is not
    one: ``api/functions/`` is a deployment package with its own
    ``requirements.txt``, and making it importable as ``chip_chat.api.functions``
    would put the Functions SDK on the workspace's import path -- which is the
    thing ``api/functions/requirements.txt`` exists to avoid.
    """
    azure_functions_stub.install()
    path = Path(__file__).resolve().parents[1] / "functions" / "function_app.py"
    spec = importlib.util.spec_from_file_location("chip_chat_ops_function_app", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HOST = _load_host()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend() -> RecordingWriteBackend:
    """The stand-in for the Functions app's Snowflake connection."""
    return RecordingWriteBackend()


@pytest.fixture
def confirmations(clock: FakeClock) -> ConfirmationLedger:
    """The ledger the three writes without a draft claim from."""
    return ConfirmationLedger(clock=clock)


@pytest.fixture
def service(
    backend: RecordingWriteBackend,
    drafts: DraftStore,
    confirmations: ConfirmationLedger,
) -> OpsService:
    """The service the host serves, wired to doubles and a driven clock."""
    return OpsService(backend, drafts, confirmations)


@pytest.fixture(autouse=True)
def installed(
    service: OpsService, monkeypatch: pytest.MonkeyPatch
) -> Iterator[OpsService]:
    """Install the service and the ops key, and take both away afterwards.

    ``configure`` writes a module global, which is how a deployment hands the
    host an assembled service. Resetting it between tests is what keeps the
    outage test from being ordering-dependent: it has to be able to observe a
    host with nothing installed.
    """
    monkeypatch.setenv(HOST.OPS_KEY_VARIABLE, OPS_KEY)
    HOST.configure(service)
    yield service
    HOST.configure(None)


@pytest.fixture
def spans() -> Iterator[SpanRecorder]:
    """Route the ops spans into memory for the duration of a test."""
    with span_recorder("api") as recorder:
        yield recorder


@pytest.fixture
def turn(spans: SpanRecorder) -> Iterator[Mapping[str, str]]:
    """The app's half of the turn, and the headers it would send.

    The host rejoins ``tool.<name>`` from the trace context on the request, so
    the app's side has to be open while the carrier is built. What comes back is
    what a real call would carry: ``traceparent``, and the turn's identity as
    baggage.
    """
    with (
        chat_turn(session_id="s-1", turn_index=0, message="place it"),
        agent_step(index=0),
        tool_call(ToolName.PLACE_ORDER, arguments={}),
    ):
        yield turn_context_headers()


def request(
    body: Mapping[str, Any],
    *,
    trace: Mapping[str, str],
    demo_id: str | None = VISITOR,
    key: str | None = OPS_KEY,
) -> Any:
    """Compose one inbound request the way the chat app composes it.

    Args:
        body: The JSON body.
        trace: The W3C carrier from :func:`turn_context_headers`.
        demo_id: The visitor the app resolved from its session cookie, or
            ``None`` to send no session header at all.
        key: The ops key, or ``None`` to present none.
    """
    headers: dict[str, str] = dict(trace)
    if key is not None:
        headers[OPS_KEY_HEADER] = key
    if demo_id is not None:
        headers[SESSION_HEADER] = demo_id
    return azure_functions_stub.HttpRequest(
        "POST",
        "https://func-chip-chat-ops.example/api/place_order",
        body=json.dumps(body).encode("utf-8"),
        headers=headers,
    )


def answered(response: Any) -> Mapping[str, Any]:
    """Decode a response body, as anything reading this API would."""
    decoded = json.loads(response.get_body().decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def confirmed_draft(drafts: DraftStore, demo_id: str = VISITOR) -> str:
    """Propose and confirm one draft, and return its id."""
    draft = drafts.propose(demo_id, [burrito()])
    drafts.confirm(demo_id, draft.draft_id)
    return draft.draft_id


# ---------------------------------------------------------------------------
# The routes exist, and the worker would find them
# ---------------------------------------------------------------------------


def test_the_host_registers_one_route_per_write_action() -> None:
    """Registration, observed rather than read out of the decorator source."""
    assert set(HOST.app.routes) == {action.value for action in OpsAction}


# ---------------------------------------------------------------------------
# An unconfirmed draft_id is rejected -- through the route, not the service
# ---------------------------------------------------------------------------


def test_an_unconfirmed_draft_is_rejected_at_the_route(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """Launch gate 2, met at the edge a caller actually reaches.

    The draft exists and belongs to this visitor. The only thing missing is the
    press of Confirm, which is the whole of the rule.
    """
    draft = drafts.propose(VISITOR, [burrito()])

    response = HOST.place_order(request({"draft_id": draft.draft_id}, trace=turn))

    body = answered(response)
    assert response.status_code == 200
    assert body["ok"] is False
    assert body["error"] == "DRAFT_NOT_CONFIRMED"
    assert backend.writes == []


def test_an_unconfirmed_draft_reaches_no_procedure_at_all(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """Not merely no write: no call. The database is never asked."""
    draft = drafts.propose(VISITOR, [burrito()])

    HOST.place_order(request({"draft_id": draft.draft_id}, trace=turn))

    assert backend.calls == []


def test_a_draft_nobody_minted_is_rejected_at_the_route(
    backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """A model that invents a draft id gets a rejection, not an order."""
    response = HOST.place_order(request({"draft_id": "drf-invented"}, trace=turn))

    assert answered(response)["error"] == "DRAFT_NOT_FOUND"
    assert backend.writes == []


def test_a_confirmed_draft_is_placed_through_the_route(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """The permitted case, so the refusals above are refusals and not an outage."""
    draft_id = confirmed_draft(drafts)

    response = HOST.place_order(request({"draft_id": draft_id}, trace=turn))

    body = answered(response)
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["receipt"]["reference_id"] == draft_id
    assert len(backend.writes) == 1


@pytest.mark.parametrize(
    ("route", "field", "reference"),
    [
        ("cancel_order", "order_id", ORDER),
        ("redeem_points", "reward_id", REWARD),
        ("update_preferences", "prefs", PREFS),
    ],
)
def test_the_three_writes_without_a_draft_are_gated_the_same_way(
    route: str,
    field: str,
    reference: Any,
    confirmations: ConfirmationLedger,
    backend: RecordingWriteBackend,
    turn: Mapping[str, str],
) -> None:
    """A card was offered and never confirmed, on each of the other three routes.

    The gate is not a property of ``place_order``. Each of these names something
    the visitor has already been shown, and each is refused until they said yes
    to it.
    """
    offer_cancellation(confirmations, VISITOR, ORDER)
    offer_redemption(confirmations, VISITOR, REWARD, name="Chips", point_cost=250)
    offer_preferences(confirmations, VISITOR, PREFS)

    response = getattr(HOST, route)(request({field: reference}, trace=turn))

    assert answered(response)["error"] == "CONFIRMATION_NOT_CONFIRMED"
    assert backend.writes == []


# ---------------------------------------------------------------------------
# A confirmed draft from another session is rejected
# ---------------------------------------------------------------------------


def test_another_visitors_confirmed_draft_is_rejected_at_the_route(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """A real, confirmed, live draft -- belonging to somebody else.

    The header is the only thing that differs from the call that succeeds, which
    is the point: the identifier in the body is not what decides whose draft it
    is. ``DRAFT_NOT_FOUND`` rather than a permission error, because a store that
    distinguished "not yours" from "does not exist" would be a store that
    confirms other people's draft ids exist.
    """
    draft_id = confirmed_draft(drafts, VISITOR)

    response = HOST.place_order(
        request({"draft_id": draft_id}, trace=turn, demo_id=STRANGER)
    )

    assert answered(response)["error"] == "DRAFT_NOT_FOUND"
    assert backend.writes == []


def test_the_stranger_can_still_place_their_own_draft(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """The refusal above is about whose draft it is, not about the stranger."""
    theirs = confirmed_draft(drafts, STRANGER)

    response = HOST.place_order(
        request({"draft_id": theirs}, trace=turn, demo_id=STRANGER)
    )

    assert answered(response)["ok"] is True
    assert [call.demo_id for call in backend.writes] == [STRANGER]


def test_a_write_with_no_session_header_is_refused(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """No visitor, no write. There is no anonymous write and no default one."""
    draft_id = confirmed_draft(drafts)

    response = HOST.place_order(request({"draft_id": draft_id}, trace=turn, demo_id=None))

    assert response.status_code == 401
    assert answered(response)["error"] == "SESSION_REQUIRED"
    assert backend.writes == []


# ---------------------------------------------------------------------------
# Retrying with the same idempotency key produces one write
# ---------------------------------------------------------------------------


def test_a_dropped_connection_after_the_commit_still_writes_once(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """The failure the retry key exists for, driven through the route.

    The procedure commits and spends its key; the connection dies before the
    caller hears about it. The second attempt carries the same key -- the
    draft's own id -- finds the receipt and replays it. Two calls, one write,
    and the visitor is told their order was placed because it was.
    """
    draft_id = confirmed_draft(drafts)
    backend.commit_then_fail()

    response = HOST.place_order(request({"draft_id": draft_id}, trace=turn))

    body = answered(response)
    assert body["ok"] is True
    assert body["receipt"]["replayed"] is True
    assert len(backend.calls) == 2
    assert len(backend.writes) == 1


def test_the_two_attempts_carry_the_same_retry_key(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """Which is what makes the replay a replay rather than a coincidence."""
    draft_id = confirmed_draft(drafts)
    backend.commit_then_fail()

    HOST.place_order(request({"draft_id": draft_id}, trace=turn))

    assert {call.retry_key for call in backend.calls} == {draft_id}


def test_posting_the_same_draft_twice_writes_once(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """The retry a *caller* makes, rather than the one the service makes.

    A client that did not hear the first response and sent the whole request
    again. Claiming retires the draft, so the second request finds nothing to
    place -- a rejection, and emphatically not a second order.
    """
    draft_id = confirmed_draft(drafts)

    first = HOST.place_order(request({"draft_id": draft_id}, trace=turn))
    second = HOST.place_order(request({"draft_id": draft_id}, trace=turn))

    assert answered(first)["ok"] is True
    assert answered(second)["ok"] is False
    assert len(backend.writes) == 1


# ---------------------------------------------------------------------------
# Taking the Functions app down produces the specified message
# ---------------------------------------------------------------------------


def test_a_write_path_that_cannot_be_reached_answers_with_the_specified_message(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """RFC-001 section 10's row for this service, at the edge that renders it.

    503, the sentence a visitor is shown, and ``ordering_available`` false so the
    card can say so without the app having to parse prose.
    """
    draft_id = confirmed_draft(drafts)
    backend.take_down()

    response = HOST.place_order(request({"draft_id": draft_id}, trace=turn))

    body = answered(response)
    assert response.status_code == 503
    assert body["message"] == OPS_UNAVAILABLE_MESSAGE
    assert body["ordering_available"] is False
    assert backend.writes == []


def test_a_host_with_no_service_installed_is_an_outage_and_not_a_crash(
    turn: Mapping[str, str],
) -> None:
    """The state ``func-chip-chat-ops-4cy39i`` is actually in today.

    :func:`build_ops_service` refuses to come up without a catalogue -- #66's
    loader -- so a call to the deployed host resolves nothing. What that must
    produce is the row RFC-001 section 10 already has copy for, not a 500 with a
    stack trace in it.
    """
    HOST.configure(None)

    response = HOST.place_order(request({"draft_id": "drf-anything"}, trace=turn))

    assert response.status_code == 503
    assert answered(response)["message"] == OPS_UNAVAILABLE_MESSAGE


def test_the_five_hundred_and_three_body_is_the_one_the_host_names() -> None:
    """``UNAVAILABLE_BODY`` is what a caller may assert against, so it must match."""
    assert HOST.UNAVAILABLE_BODY["message"] == OPS_UNAVAILABLE_MESSAGE
    assert HOST.UNAVAILABLE_BODY["ordering_available"] is False


# ---------------------------------------------------------------------------
# Every write emits an ops.<action> span with draft id and confirmation state
# ---------------------------------------------------------------------------


def test_a_confirmed_write_emits_its_span_through_the_route(
    drafts: DraftStore, spans: SpanRecorder, turn: Mapping[str, str]
) -> None:
    """Gate 2 is auditable in traces, which means the span survives the edge."""
    draft_id = confirmed_draft(drafts)

    HOST.place_order(request({"draft_id": draft_id}, trace=turn))

    attributes = spans.attributes_of("ops.place_order")
    assert attributes[ChipChatAttributes.OPS_REFERENCE_ID] == draft_id
    assert (
        attributes[ChipChatAttributes.OPS_CONFIRMATION_STATE]
        == ConfirmationState.CONFIRMED
    )


def test_a_refused_write_emits_its_span_too(
    drafts: DraftStore, spans: SpanRecorder, turn: Mapping[str, str]
) -> None:
    """A gate violation nobody can find in a trace is a gate nobody can audit."""
    draft = drafts.propose(VISITOR, [burrito()])

    HOST.place_order(request({"draft_id": draft.draft_id}, trace=turn))

    attributes = spans.attributes_of("ops.place_order")
    assert attributes[ChipChatAttributes.OPS_REFERENCE_ID] == draft.draft_id
    assert (
        attributes[ChipChatAttributes.OPS_CONFIRMATION_STATE]
        == ConfirmationState.REJECTED
    )


def test_the_write_span_rejoins_the_app_side_of_the_turn(
    drafts: DraftStore, spans: SpanRecorder, turn: Mapping[str, str]
) -> None:
    """One trace across the process boundary, which is what makes it findable.

    The host extracts the carrier and opens the write under the app's
    ``tool.place_order``. If it opened a trace of its own instead, every
    assertion above would still pass and no operator would ever see a write
    beside the turn that made it.

    Compared against the ``traceparent`` on the wire rather than against the
    app's span object, because the app's span is still open while the host runs
    -- which is exactly the situation in the deployed system, and an exported
    span is not available until it closes. The header is also the only thing
    that actually crossed, so it is the honest thing to compare to.
    """
    draft_id = confirmed_draft(drafts)
    sent = turn["traceparent"].split("-")[1]

    HOST.place_order(request({"draft_id": draft_id}, trace=turn))

    ops = spans.span_named("ops.place_order")
    assert ops.context is not None
    assert format(ops.context.trace_id, "032x") == sent
    assert ops.parent is not None
    assert format(ops.parent.span_id, "016x") == turn["traceparent"].split("-")[2]


@pytest.mark.parametrize(
    ("route", "field", "reference", "action"),
    [
        ("cancel_order", "order_id", ORDER, OpsAction.CANCEL_ORDER),
        ("redeem_points", "reward_id", REWARD, OpsAction.REDEEM_POINTS),
        ("update_preferences", "prefs", PREFS, OpsAction.UPDATE_PREFERENCES),
    ],
)
def test_every_route_emits_its_own_span_name(
    route: str,
    field: str,
    reference: Any,
    action: OpsAction,
    confirmations: ConfirmationLedger,
    spans: SpanRecorder,
    turn: Mapping[str, str],
) -> None:
    """Four actions, four span names. A shared name is an unauditable rollup."""
    card = {
        OpsAction.CANCEL_ORDER: lambda: offer_cancellation(confirmations, VISITOR, ORDER),
        OpsAction.REDEEM_POINTS: lambda: offer_redemption(
            confirmations, VISITOR, REWARD, name="Chips", point_cost=250
        ),
        OpsAction.UPDATE_PREFERENCES: lambda: offer_preferences(
            confirmations, VISITOR, PREFS
        ),
    }[action]()
    confirmations.confirm(VISITOR, card.confirmation_id)

    response = getattr(HOST, route)(request({field: reference}, trace=turn))

    assert answered(response)["ok"] is True
    attributes = spans.attributes_of(f"ops.{action.value}")
    assert (
        attributes[ChipChatAttributes.OPS_CONFIRMATION_STATE]
        == ConfirmationState.CONFIRMED
    )


def test_a_write_with_no_trace_context_is_refused_before_it_is_made(
    drafts: DraftStore, backend: RecordingWriteBackend
) -> None:
    """Auditability is a precondition here, not a best effort.

    A write whose span lands in a trace nobody can find is a write gate 2 cannot
    be checked against, so the host declines to make it. Refused *before* the
    write, which is the half worth asserting.
    """
    draft_id = confirmed_draft(drafts)

    response = HOST.place_order(request({"draft_id": draft_id}, trace={}))

    assert response.status_code == 400
    assert answered(response)["error"] == "TRACE_CONTEXT_REQUIRED"
    assert backend.writes == []


# ---------------------------------------------------------------------------
# The caller has to be the app
# ---------------------------------------------------------------------------


def test_a_caller_with_no_ops_key_may_not_write(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """This service is the only path that writes, so an open one writes as anybody."""
    draft_id = confirmed_draft(drafts)

    response = HOST.place_order(request({"draft_id": draft_id}, trace=turn, key=None))

    assert response.status_code == 401
    assert backend.writes == []


def test_a_caller_with_the_wrong_ops_key_may_not_write(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """The comparison is constant-time; that it compares at all is asserted here."""
    draft_id = confirmed_draft(drafts)

    response = HOST.place_order(
        request({"draft_id": draft_id}, trace=turn, key="not-the-key")
    )

    assert response.status_code == 401
    assert backend.writes == []


def test_an_unset_ops_key_refuses_every_caller(
    drafts: DraftStore,
    backend: RecordingWriteBackend,
    turn: Mapping[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key configured means no writes, never every write.

    The other way round is how a write path ends up open, and it fails in
    exactly the environment nobody tests.
    """
    monkeypatch.delenv(HOST.OPS_KEY_VARIABLE, raising=False)
    draft_id = confirmed_draft(drafts)

    response = HOST.place_order(request({"draft_id": draft_id}, trace=turn))

    assert response.status_code == 401
    assert backend.writes == []


def test_the_key_is_checked_before_anything_else_is_read(
    backend: RecordingWriteBackend,
) -> None:
    """An unauthenticated caller learns nothing about the body or the trace.

    Sent with no key, no trace context and a body that is not JSON: three
    reasons to refuse, and the one that comes back is the key. Otherwise a
    caller could map the service's preconditions without holding it.
    """
    malformed = azure_functions_stub.HttpRequest(
        "POST",
        "https://func-chip-chat-ops.example/api/place_order",
        body=b"not json at all",
        headers={},
    )

    response = HOST.place_order(malformed)

    assert answered(response)["error"] == "OPS_KEY_INVALID"
    assert backend.calls == []


# ---------------------------------------------------------------------------
# Bodies this service will not answer at all
# ---------------------------------------------------------------------------


def test_a_body_that_is_not_json_is_a_four_hundred(
    backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    body = azure_functions_stub.HttpRequest(
        "POST",
        "https://func-chip-chat-ops.example/api/place_order",
        body=b"{{{",
        headers={**turn, OPS_KEY_HEADER: OPS_KEY, SESSION_HEADER: VISITOR},
    )

    response = HOST.place_order(body)

    assert response.status_code == 400
    assert answered(response)["error"] == "BODY_NOT_JSON"
    assert backend.calls == []


@pytest.mark.parametrize(
    ("route", "body"),
    [
        ("place_order", {}),
        ("place_order", {"draft_id": ""}),
        ("cancel_order", {"order_id": None}),
        ("redeem_points", {"reward_id": 17}),
        ("update_preferences", {"prefs": "not a mapping"}),
    ],
)
def test_a_missing_or_misshapen_reference_is_a_four_hundred(
    route: str,
    body: Mapping[str, Any],
    backend: RecordingWriteBackend,
    turn: Mapping[str, str],
) -> None:
    """Every write names something the visitor was shown, and it has to be one."""
    response = getattr(HOST, route)(request(body, trace=turn))

    assert response.status_code == 400
    assert answered(response)["error"] == "REFERENCE_REQUIRED"
    assert backend.calls == []


def test_the_headers_are_read_case_insensitively(
    drafts: DraftStore, turn: Mapping[str, str]
) -> None:
    """HTTP header names are case-insensitive and no client is obliged to agree.

    Asserted because the stub is where a case-sensitive host could hide, and a
    test that only ever sends this repository's own casing would never find it.
    """
    draft_id = confirmed_draft(drafts)
    headers = {
        **{name.upper(): value for name, value in turn.items()},
        OPS_KEY_HEADER.upper(): OPS_KEY,
        SESSION_HEADER.title(): VISITOR,
    }
    inbound = azure_functions_stub.HttpRequest(
        "POST",
        "https://func-chip-chat-ops.example/api/place_order",
        body=json.dumps({"draft_id": draft_id}).encode("utf-8"),
        headers=headers,
    )

    assert answered(HOST.place_order(inbound))["ok"] is True


def test_a_visitor_identifier_the_pattern_refuses_never_reaches_a_session(
    drafts: DraftStore, backend: RecordingWriteBackend, turn: Mapping[str, str]
) -> None:
    """``SET`` takes no bound parameter, so the allowlist is the defence.

    Driven rather than read: a session header carrying a quote is refused, and
    the refusal happens before any connection is opened.
    """
    draft_id = confirmed_draft(drafts)

    response = HOST.place_order(
        request({"draft_id": draft_id}, trace=turn, demo_id="dm-1'; DROP TABLE orders--")
    )

    assert answered(response)["ok"] is False
    assert backend.writes == []
