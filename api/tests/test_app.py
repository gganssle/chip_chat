"""The request path, and the one property it exists to hold.

``api/README.md`` and :mod:`chip_chat.api.guard` both say the same thing in
different words: the refusal happens *before* a model is called, in the request
path, synchronously. Until this module existed the guard was a library nothing
called, so that claim was true of the code and untestable of the system.

Every refusal test here therefore asserts on
:attr:`~chip_chat.agent.testing.ScriptedModel.call_count` rather than on the
response text. The copy would still read correctly if the check regressed into
something asynchronous; the call count would not.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chip_chat.agent.testing import ScriptedModel, answer, calls_tool
from chip_chat.api.app import SESSION_COOKIE, Service, create_app
from chip_chat.api.guard import SpendGuard
from chip_chat.api.killswitch import ManualKillSwitch
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import STOP_STATE_MESSAGE
from chip_chat.api.testing import FakeClock
from chip_chat.api.turns import SpendGate
from chip_chat.api.visitors import PersonaFixture, StaticRoster, VisitorDesk
from chip_chat.otel import ChipChatAttributes, ToolName
from chip_chat.otel.testing import span_recorder
from chip_chat.web import BANNER


@pytest.fixture
def limits() -> SpendLimits:
    """Ceilings small enough that a test can trip them for real."""
    return SpendLimits(
        daily_token_ceiling=10_000,
        session_turn_cap=3,
        session_token_cap=6_000,
        source_requests_per_window=5,
        source_window_seconds=60.0,
        turn_token_reservation=1_000,
    )


@pytest.fixture
def kill_switch() -> ManualKillSwitch:
    return ManualKillSwitch()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def model() -> ScriptedModel:
    """A model that answers immediately, and records that it was asked."""
    return ScriptedModel(*[answer("Sure thing.")] * 40)


@pytest.fixture
def service(
    limits: SpendLimits,
    kill_switch: ManualKillSwitch,
    clock: FakeClock,
    model: ScriptedModel,
) -> Service:
    return Service(
        SpendGate(SpendGuard(limits, kill_switch=kill_switch, clock=clock), lambda: model)
    )


@pytest.fixture
def client(service: Service) -> Iterator[TestClient]:
    with TestClient(create_app(service)) as running:
        yield running


@pytest.fixture
def populated(
    limits: SpendLimits,
    kill_switch: ManualKillSwitch,
    clock: FakeClock,
    model: ScriptedModel,
) -> Iterator[TestClient]:
    """A client whose deployment has a synthetic population behind it.

    One fixture, so the persona a test asserts about is the one it wrote down.
    """
    service = Service(
        SpendGate(
            SpendGuard(limits, kill_switch=kill_switch, clock=clock), lambda: model
        ),
        visitors=VisitorDesk(
            StaticRoster(
                [
                    PersonaFixture(
                        demo_id="demo-0001",
                        persona_id="regular",
                        label="The Weekly Regular",
                        home_store=679,
                        home_store_name="Ballard",
                        points_balance=1_340,
                        usual_item_id="CMG-1",
                        order_count=79,
                        narrative=(
                            "a regular at Ballard, 1,340 points on the card, and "
                            "99% of 79 orders the same Chicken Bowl."
                        ),
                    )
                ]
            ),
            clock=clock,
        ),
    )
    with TestClient(create_app(service)) as running:
        yield running


def say(client: TestClient, message: str, **extra: Any) -> Any:
    return client.post("/api/chat", json={"message": message, **extra})


# --- The entry page ---------------------------------------------------------


def test_the_entry_page_carries_the_unaffiliated_banner(client: TestClient) -> None:
    body = client.get("/").text
    assert BANNER in body
    assert "simulated" in body


def test_the_demo_is_kept_out_of_search_results(client: TestClient) -> None:
    """Two halves: the meta tag, and the header for anything that only fetches."""
    response = client.get("/")
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    assert 'name="robots" content="noindex,nofollow"' in response.text
    assert "Disallow: /" in client.get("/robots.txt").text


def test_the_opening_message_names_the_persona(populated: TestClient) -> None:
    """A visitor with an empty account has nothing to ask; this says they aren't.

    The message is on the *entry response* rather than in the served HTML: the
    page is the same document for everybody and the persona is not, so the
    sentence is composed once the app has decided who the visitor became.
    """
    opening = populated.post("/api/entry", json={"name": "Sam"}).json()["opening"]

    assert opening.startswith("Hi Sam.")
    assert "1,340 points on the card" in opening
    assert "Ballard" in opening


def test_the_entry_page_mints_a_session(client: TestClient) -> None:
    assert client.get("/").cookies.get(SESSION_COOKIE)


def test_the_entry_page_serves_the_stop_state_when_the_switch_is_thrown(
    client: TestClient, kill_switch: ManualKillSwitch, model: ScriptedModel
) -> None:
    kill_switch.throw()
    response = client.get("/")
    assert response.status_code == 200  # A designed state, never an error.
    assert STOP_STATE_MESSAGE in response.text
    assert "quota" not in response.text.lower()
    assert model.call_count == 0


def test_health_answers_even_when_the_app_is_stopped(
    client: TestClient, kill_switch: ManualKillSwitch
) -> None:
    """A probe refused for spending money it never spends would take the app down."""
    kill_switch.throw()
    assert client.get("/healthz").json() == {"status": "ok"}


# --- One turn ---------------------------------------------------------------


def test_a_turn_answers_and_calls_the_model(
    client: TestClient, model: ScriptedModel
) -> None:
    body = say(client, "hello").json()
    assert body == {
        "reply": "Sure thing.",
        "card": None,
        "receipt": False,
        "stopped": False,
    }
    assert model.call_count == 1


def test_a_turn_emits_one_readable_span_tree(client: TestClient) -> None:
    """The acceptance criterion, asserted rather than described."""
    with span_recorder("api") as spans:
        say(client, "hello")
    assert spans.tree_text() == (
        "chat.turn\n  guard.budget_check\n  agent.step\n    llm.completion"
        "\n  render.response"
    )


def test_turns_in_one_session_are_numbered(client: TestClient) -> None:
    with span_recorder("api") as spans:
        say(client, "one")
        say(client, "two")
    indexes = [
        span.attributes[ChipChatAttributes.TURN_INDEX]
        for span in spans.finished_spans()
        if span.name == "chat.turn" and span.attributes
    ]
    assert indexes == [0, 1]


def test_a_client_cannot_choose_its_own_session_id(client: TestClient) -> None:
    """Session ids are minted here, so the per-session cap cannot be re-rolled."""
    client.cookies.set(SESSION_COOKIE, "../../etc/passwd")
    response = say(client, "hello")
    assert response.cookies.get(SESSION_COOKIE) != "../../etc/passwd"


# --- The cap, which is the reason this module exists ------------------------


def test_the_kill_switch_stops_the_turn_before_the_model(
    client: TestClient, kill_switch: ManualKillSwitch, model: ScriptedModel
) -> None:
    kill_switch.throw()
    body = say(client, "hello").json()
    assert body["stopped"] is True
    assert body["reply"] == STOP_STATE_MESSAGE
    assert model.call_count == 0


def test_the_stop_state_is_never_an_error_status(
    client: TestClient, kill_switch: ManualKillSwitch
) -> None:
    kill_switch.throw()
    assert say(client, "hello").status_code == 200


def test_the_session_turn_cap_stops_the_model_being_called(
    client: TestClient, limits: SpendLimits, model: ScriptedModel
) -> None:
    for _ in range(limits.session_turn_cap):
        assert say(client, "hello").json()["stopped"] is False
    before = model.call_count
    assert say(client, "hello").json()["stopped"] is True
    assert model.call_count == before


def test_the_source_rate_limit_stops_the_model_being_called(
    limits: SpendLimits, kill_switch: ManualKillSwitch, model: ScriptedModel
) -> None:
    """Sessions are free to mint; the address a request came from is not."""
    tight = SpendLimits(
        daily_token_ceiling=limits.daily_token_ceiling,
        session_turn_cap=100,
        session_token_cap=1_000_000,
        source_requests_per_window=2,
        source_window_seconds=60.0,
        turn_token_reservation=10,
    )
    service = Service(
        SpendGate(SpendGuard(tight, kill_switch=kill_switch), lambda: model)
    )
    with TestClient(create_app(service)) as client:
        for _ in range(2):
            assert say(client, "hello").json()["stopped"] is False
        before = model.call_count
        assert say(client, "hello").json()["stopped"] is True
    assert model.call_count == before


def test_the_daily_ceiling_stops_the_model_being_called(
    client: TestClient, service: Service, model: ScriptedModel
) -> None:
    service.guard.ledger.commit(service.guard.ledger.reserve("warm-up"), 10_000)
    before = model.call_count
    body = say(client, "hello").json()
    assert body["stopped"] is True
    assert model.call_count == before


def test_a_refusal_is_recorded_on_the_turn_and_the_guard_span(
    client: TestClient, kill_switch: ManualKillSwitch
) -> None:
    kill_switch.throw()
    with span_recorder("api") as spans:
        say(client, "hello")
    assert (
        spans.attributes_of("guard.budget_check")[ChipChatAttributes.GUARD_REASON]
        == "kill_switch"
    )
    assert (
        spans.attributes_of("chat.turn")[ChipChatAttributes.GUARD_REASON] == "kill_switch"
    )
    # The visitor saw something, so render.response is still part of the turn.
    assert "render.response" in spans.names()
    assert "agent.step" not in spans.names()


def test_real_token_counts_reach_the_ledger(
    kill_switch: ManualKillSwitch, limits: SpendLimits
) -> None:
    """Settling the reservation with the real number is what makes the ceiling
    mean tokens rather than turns."""
    model = ScriptedModel(answer("Sure.", prompt_tokens=120, completion_tokens=30))
    service = Service(
        SpendGate(SpendGuard(limits, kill_switch=kill_switch), lambda: model)
    )
    with TestClient(create_app(service)) as client:
        say(client, "hello")
    assert service.guard.ledger.global_usage().used == 150


def test_the_rate_limit_counts_the_forwarded_client_and_not_the_proxy(
    client: TestClient, model: ScriptedModel, service: Service
) -> None:
    """One trusted proxy appends, so the last entry is the address it saw.

    Taking the first would let a caller re-roll its bucket on every request by
    inventing a header, which is the opposite of what the limiter is for.
    """
    say(client, "hello", **{})
    client.post(
        "/api/chat",
        json={"message": "hello"},
        headers={"X-Forwarded-For": "10.0.0.1, 203.0.113.9"},
    )
    assert service.guard.rate_limiter.usage("203.0.113.9").used == 1
    assert service.guard.rate_limiter.usage("10.0.0.1").used == 0


# --- Confirmation, which the agent cannot grant itself ----------------------


def order_client(model: ScriptedModel, limits: SpendLimits) -> TestClient:
    return TestClient(create_app(Service(SpendGate(SpendGuard(limits), lambda: model))))


def test_an_order_needs_the_button_and_not_the_prompt(limits: SpendLimits) -> None:
    """The launch gate, end to end through the request path."""
    model = ScriptedModel(
        calls_tool(ToolName.PROPOSE_ORDER, {"items": [{"item_id": "BOWL-CHICKEN"}]}),
        answer("That is $10.70 -- press Confirm."),
    )
    with order_client(model, limits) as client:
        first = say(client, "a chicken bowl please").json()
        draft_id = first["card"]["draft_id"]
        assert first["card"]["requires_confirmation"] is True
        assert first["receipt"] is False

        # The model tries to place it without the visitor pressing anything.
        model._replies.extend(
            [
                calls_tool(ToolName.PLACE_ORDER, {"draft_id": draft_id}),
                answer("I could not place that."),
            ]
        )
        with span_recorder("api") as spans:
            refused = say(client, "just do it").json()
        assert refused["card"] is None
        assert (
            spans.attributes_of("ops.place_order")[
                ChipChatAttributes.OPS_CONFIRMATION_STATE
            ]
            == "rejected"
        )

        # Now the visitor presses the button, and the same call succeeds.
        model._replies.extend(
            [
                calls_tool(ToolName.PLACE_ORDER, {"draft_id": draft_id}),
                answer("Ordered. Simulated, of course."),
            ]
        )
        placed = say(client, "yes", confirm_draft_id=draft_id).json()
    assert placed["receipt"] is True
    assert str(placed["card"]["order_id"]).startswith("CC-")


def test_confirming_someone_elses_draft_does_nothing(limits: SpendLimits) -> None:
    model = ScriptedModel(
        calls_tool(ToolName.PROPOSE_ORDER, {"items": [{"item_id": "BOWL-CHICKEN"}]}),
        answer("Press Confirm."),
    )
    with order_client(model, limits) as owner:
        draft_id = say(owner, "a chicken bowl").json()["card"]["draft_id"]
        service_desk_before = draft_id
    # A second client is a second session, and the draft is not its to confirm.
    with order_client(model, limits) as stranger:
        model._replies.extend(
            [
                calls_tool(ToolName.PLACE_ORDER, {"draft_id": service_desk_before}),
                answer("No such draft."),
            ]
        )
        body = say(stranger, "yes", confirm_draft_id=service_desk_before).json()
    assert body["receipt"] is False


# --- When something goes wrong ---------------------------------------------


class ExplodingModel:
    """A model that fails the way a real one does: at the worst moment."""

    deployment = "gpt-test-mini"

    def complete(self, messages: Any, *, tools: Any = ()) -> Any:
        raise RuntimeError("the deployment is having a day")


def test_a_failing_model_does_not_fail_the_conversation(limits: SpendLimits) -> None:
    """RFC-001 section 10: a lane may fail, the conversation may not fail with it."""
    service = Service(SpendGate(SpendGuard(limits), ExplodingModel))
    with TestClient(create_app(service)) as client, span_recorder("api") as spans:
        response = say(client, "hello")
    assert response.status_code == 200
    assert response.json()["stopped"] is False
    assert "went wrong" in response.json()["reply"]
    # The failure is on the trace rather than only in a log.
    assert spans.span_named("chat.turn").status.is_ok is False
    assert "render.response" in spans.names()


def test_a_failed_turn_is_charged_the_pessimistic_reservation(
    limits: SpendLimits,
) -> None:
    """The tokens it bought before falling over are unknown; over-counting by
    less than one turn is the safe direction to be wrong in."""
    service = Service(SpendGate(SpendGuard(limits), ExplodingModel))
    with TestClient(create_app(service)) as client:
        say(client, "hello")
    assert service.guard.ledger.global_usage().used == limits.turn_token_reservation


def test_an_empty_message_is_refused_before_anything_else(
    client: TestClient, model: ScriptedModel
) -> None:
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    assert model.call_count == 0


def test_an_enormous_message_is_refused_before_anything_else(
    client: TestClient, model: ScriptedModel
) -> None:
    """The cheapest possible bound on prompt tokens: never reach the model."""
    assert client.post("/api/chat", json={"message": "x" * 5_000}).status_code == 422
    assert model.call_count == 0


def test_a_bogus_confirmation_announces_nothing_and_places_nothing(
    limits: SpendLimits,
) -> None:
    """The note is written only if the desk really confirmed something.

    A visitor who posts an id that is not theirs gets no note, and place_order
    refuses them anyway -- the desk is the gate, the note is a hint.
    """
    model = ScriptedModel(answer("There is no such draft."))
    service = Service(SpendGate(SpendGuard(limits), lambda: model))
    with TestClient(create_app(service)) as client:
        body = say(client, "yes", confirm_draft_id="draft-not-mine").json()
    assert body["receipt"] is False
    assert not any(
        "pressed Confirm" in str(message.get("content", ""))
        for message in model.requests[0]
    )


# ---------------------------------------------------------------------------
# The name gate (#66). Behaviour tests only -- the *absence* of a demo_id in
# every request is `api/tests/test_identity_binding.py`, because a criterion
# about what an endpoint accepts is a criterion about the schema.
# ---------------------------------------------------------------------------


def personas() -> VisitorDesk:
    """A desk with two archetypes loaded, which is what a live deployment has."""
    return VisitorDesk(
        StaticRoster(
            PersonaFixture(
                demo_id=f"dm-{index:06d}",
                persona_id=persona,
                label=persona.title(),
                home_store=679,
                home_store_name="Ballard",
                points_balance=1_340,
                usual_item_id="CMG-1",
                order_count=42,
                narrative="Same bowl, same store, nearly every week.",
            )
            for index, persona in enumerate(("regular", "explorer"), start=1)
        ),
        clock=FakeClock(),
    )


def test_the_name_gate_returns_a_populated_account(limits: SpendLimits) -> None:
    """One screen, one name, and an account with something in it to ask about."""
    model = ScriptedModel(answer("Sure thing."))
    service = Service(SpendGate(SpendGuard(limits), lambda: model), visitors=personas())
    with TestClient(create_app(service)) as client:
        body = client.post("/api/entry", json={"name": "Sam"}).json()

    assert body["stopped"] is False
    assert body["visitor"]["display_name"] == "Sam"
    assert body["visitor"]["points_balance"] == 1_340
    assert body["visitor"]["order_count"] == 42
    assert body["visitor"]["home_store_name"] == "Ballard"


def test_a_visitor_who_skips_the_name_gate_still_gets_an_account(
    limits: SpendLimits,
) -> None:
    """The cold start is the product risk, so the assignment is not on the polite path."""
    model = ScriptedModel(answer("Sure thing."))
    service = Service(SpendGate(SpendGuard(limits), lambda: model), visitors=personas())
    with TestClient(create_app(service)) as client:
        say(client, "what do I usually order?")
        session_id = client.cookies[SESSION_COOKIE]

    assert service.visitors.store.demo_id_for(session_id) is not None


def test_the_assigned_persona_reaches_the_turn_span(limits: SpendLimits) -> None:
    """``chat.turn`` carries the visitor it was actually served as."""
    model = ScriptedModel(answer("Sure thing."))
    service = Service(SpendGate(SpendGuard(limits), lambda: model), visitors=personas())
    with TestClient(create_app(service)) as client, span_recorder("api") as spans:
        client.post("/api/entry", json={"name": "Sam"})
        say(client, "hello")
        session_id = client.cookies[SESSION_COOKIE]

    visitor = service.visitors.visitor(session_id)
    assert visitor is not None
    turn = spans.attributes_of("chat.turn")
    assert turn[ChipChatAttributes.PERSONA_ID] == visitor.persona_id
    assert turn[ChipChatAttributes.DEMO_ID] == visitor.demo_id


def test_the_name_gate_serves_the_stop_state_and_assigns_nobody(
    limits: SpendLimits, kill_switch: ManualKillSwitch
) -> None:
    """A roster slot spent on a visitor who cannot converse is a slot wasted."""
    model = ScriptedModel(answer("Sure thing."))
    desk = personas()
    service = Service(
        SpendGate(SpendGuard(limits, kill_switch=kill_switch), lambda: model),
        visitors=desk,
    )
    kill_switch.throw()
    with TestClient(create_app(service)) as client:
        response = client.post("/api/entry", json={"name": "Sam"})

    assert response.status_code == 200, "the stop state is a designed state"
    body = response.json()
    assert body["stopped"] is True
    assert body["message"] == STOP_STATE_MESSAGE
    assert body["visitor"] is None
    assert len(desk.store) == 0


def test_a_deployment_with_no_population_loaded_serves_the_demo_unbound(
    client: TestClient,
) -> None:
    """The default service has an empty roster, and says so rather than inventing one."""
    body = client.post("/api/entry", json={"name": "Sam"}).json()

    assert body["stopped"] is False
    assert body["visitor"] is None
    assert say(client, "hello").json()["reply"] == "Sure thing."
