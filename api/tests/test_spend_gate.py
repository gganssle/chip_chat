"""Tests that fail when the invariant breaks, not when the output changes.

Every assertion in this module is about *reachability*: whether a request path
exists that can call a model without passing the spend cap first. None of them
would be fixed by correcting a message or a status code, and most of them fail
on a change that leaves every happy path green -- which is the point.

The invariant, in one sentence: **a token cannot be spent without a
:class:`~chip_chat.api.turns.FundedTurn`, and a FundedTurn cannot exist for a
turn the guard refused.**
"""

import inspect

import pytest
from fastapi.testclient import TestClient

from chip_chat.agent.testing import ScriptedModel, answer
from chip_chat.api import app as app_module
from chip_chat.api.app import Service, create_app
from chip_chat.api.guard import SpendGuard
from chip_chat.api.killswitch import ManualKillSwitch
from chip_chat.api.limits import SpendLimits
from chip_chat.api.outcome import Stop
from chip_chat.api.turns import FundedTurn, SpendGate, UnfundedTurnError
from chip_chat.otel import chat_turn
from chip_chat.otel.testing import span_recorder

EXPECTED_ROUTES = {
    ("/", frozenset({"GET"})),
    ("/healthz", frozenset({"GET"})),
    ("/healthz/lanes", frozenset({"GET"})),
    ("/robots.txt", frozenset({"GET"})),
    ("/api/chat", frozenset({"POST"})),
    ("/api/entry", frozenset({"POST"})),
    ("/api/switch", frozenset({"POST"})),
    ("/api/draft/revise", frozenset({"POST"})),
    ("/api/photo", frozenset({"POST"})),
}
"""Every route this application has.

Listed rather than counted so that adding one fails here and makes its author
decide, in this file, whether the new route can spend money. That is the whole
job of this constant: it is a speed bump in front of the mistake, not a
description of the app.

``/api/entry`` is #66's name gate, and the decision it was made to charge for is
recorded here rather than in its handler: **it cannot reach a model.** A
:class:`~chip_chat.api.turns.SpendGate` hands a model out only inside a
:class:`~chip_chat.api.turns.FundedTurn`, that route never opens one, and
:func:`test_only_the_chat_route_can_reach_a_model` below is what keeps that true
of the next person's edit. It asks
:meth:`~chip_chat.api.turns.SpendGate.entry_state` anyway, because assigning a
roster slot to a visitor who cannot have a conversation spends a persona on
nobody.

The three routes #67 to #69 added are here on the same terms, and each was
charged the same decision. ``/api/switch`` asks ``entry_state`` and reaches no
model, for exactly the reason ``/api/entry`` does. ``/api/draft/revise``
re-prices a card against a catalogue and an arithmetic -- there is no completion
anywhere in it, which is why an edit is free and instant. ``/api/photo`` spends
real money on Content Safety, so it is the one route that carries its own
ceiling: :class:`~chip_chat.api.uploads.UploadLimiter` runs before a byte is
read off the socket.
"""


@pytest.fixture
def limits() -> SpendLimits:
    return SpendLimits(
        daily_token_ceiling=10_000,
        session_turn_cap=3,
        session_token_cap=6_000,
        source_requests_per_window=5,
        source_window_seconds=60.0,
        turn_token_reservation=1_000,
    )


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel(*[answer("Sure thing.")] * 20)


# --- The constructor is the enforcement -------------------------------------


def test_a_funded_turn_cannot_be_built_for_a_refused_budget(
    limits: SpendLimits, model: ScriptedModel
) -> None:
    """Holding a FundedTurn *is* the proof that the four layers said yes."""
    switch = ManualKillSwitch(thrown=True)
    guard = SpendGuard(limits, kill_switch=switch)
    with span_recorder("api"), chat_turn(session_id="s", turn_index=0):
        budget = guard.reserve(session_id="s", source_address="1.2.3.4")
    assert budget.allowed is False
    with pytest.raises(UnfundedTurnError):
        FundedTurn(budget, model, SpendGate(guard, lambda: model).desk)


def test_the_gate_yields_a_stop_and_a_stop_cannot_run(
    limits: SpendLimits, model: ScriptedModel
) -> None:
    """The refused branch could not call a model if it tried."""
    gate = SpendGate(
        SpendGuard(limits, kill_switch=ManualKillSwitch(thrown=True)), lambda: model
    )
    with (
        span_recorder("api"),
        chat_turn(session_id="s", turn_index=0),
        gate.turn(session_id="s", source_address="1.2.3.4") as funded,
    ):
        assert not hasattr(funded, "run")
        assert isinstance(funded, Stop)
        assert funded.reason.value == "kill_switch"
    assert model.call_count == 0


# --- Nothing can be assembled without a cap ---------------------------------


def test_a_gate_cannot_be_built_without_a_guard(model: ScriptedModel) -> None:
    with pytest.raises(TypeError):
        SpendGate(model_factory=lambda: model)  # type: ignore[call-arg]


def test_a_service_cannot_be_built_without_a_gate() -> None:
    with pytest.raises(TypeError):
        Service()  # type: ignore[call-arg]


def test_nothing_but_the_gate_holds_a_model(
    limits: SpendLimits, model: ScriptedModel
) -> None:
    """A second public route to a model would be a second way to skip the check."""
    gate = SpendGate(SpendGuard(limits), lambda: model)
    service = Service(gate)
    for holder in (service, gate):
        public = [name for name in dir(holder) if not name.startswith("_")]
        assert not any("model" in name for name in public), public


def test_running_a_turn_is_the_only_public_method_that_reaches_the_model(
    limits: SpendLimits, model: ScriptedModel
) -> None:
    """If this grows a second entry, the claim above needs re-checking."""
    reaching = [
        name
        for name, member in inspect.getmembers(FundedTurn, inspect.isfunction)
        if not name.startswith("_") and "_model" in inspect.getsource(member)
    ]
    assert reaching == ["run"]


# --- Ordering, which is what a careless refactor breaks ---------------------


def test_the_budget_check_finishes_before_any_model_call_starts(
    limits: SpendLimits, model: ScriptedModel
) -> None:
    """Fails if the check is moved beside or after the model rather than before.

    Asserted on span timestamps rather than on call order in the source,
    because it is the runtime ordering that decides whether a refusal costs
    anything.
    """
    service = Service(SpendGate(SpendGuard(limits), lambda: model))
    with TestClient(create_app(service)) as client, span_recorder("api") as spans:
        client.post("/api/chat", json={"message": "hello"})
    guard_span = spans.span_named("guard.budget_check")
    completion = spans.span_named("llm.completion")
    assert guard_span.end_time is not None
    assert completion.start_time is not None
    assert guard_span.end_time <= completion.start_time


def test_a_refused_turn_reaches_no_agent_span_at_all(
    limits: SpendLimits, model: ScriptedModel
) -> None:
    """Not "the reply says stopped" -- no work happened under the turn."""
    service = Service(
        SpendGate(
            SpendGuard(limits, kill_switch=ManualKillSwitch(thrown=True)),
            lambda: model,
        )
    )
    with TestClient(create_app(service)) as client, span_recorder("api") as spans:
        client.post("/api/chat", json={"message": "hello"})
    assert spans.names() == ("guard.budget_check", "render.response", "chat.turn")
    assert model.call_count == 0


# --- The application as a whole ---------------------------------------------


def test_the_application_has_exactly_the_routes_this_file_knows_about(
    limits: SpendLimits, model: ScriptedModel
) -> None:
    """Adding a route fails here, so its author has to think about the cap.

    A new route that cannot spend money costs one line in EXPECTED_ROUTES. A new
    route that can costs a decision, which is the price this test exists to
    charge.
    """
    application = create_app(Service(SpendGate(SpendGuard(limits), lambda: model)))
    found = {
        (route.path, frozenset(route.methods))  # type: ignore[attr-defined]
        for route in application.routes
        if getattr(route, "methods", None) is not None
    }
    assert found == EXPECTED_ROUTES


def test_only_the_chat_route_can_reach_a_model(
    limits: SpendLimits, model: ScriptedModel
) -> None:
    """The route added by #66 buys nothing, and the assertion is on the mock.

    Same shape as every refusal test in this package: the copy would still read
    correctly if the entry route started calling a model, and the call count
    would not.
    """
    service = Service(SpendGate(SpendGuard(limits), lambda: model))
    with TestClient(create_app(service)) as client:
        for _ in range(20):
            client.post("/api/entry", json={"name": "Sam"})

    assert model.call_count == 0


def test_every_route_that_can_spend_goes_through_the_gate(
    limits: SpendLimits, model: ScriptedModel
) -> None:
    """The reachability claim, checked against the module rather than the docs.

    ``chip_chat.agent.loop.run_turn`` is what calls a model. The request path is
    not allowed to have it in scope at all -- only
    :meth:`chip_chat.api.turns.FundedTurn.run` may -- so importing it into
    ``app`` fails here even if the new caller happens to check the budget first.
    """
    assert not hasattr(app_module, "run_turn")
    assert "service.gate.turn(" in inspect.getsource(app_module)


def test_the_health_probe_never_touches_the_cap(
    limits: SpendLimits, model: ScriptedModel
) -> None:
    """Deliberately outside, so a reached ceiling does not restart the container."""
    service = Service(
        SpendGate(
            SpendGuard(limits, kill_switch=ManualKillSwitch(thrown=True)),
            lambda: model,
        )
    )
    with TestClient(create_app(service)) as client, span_recorder("api") as spans:
        assert client.get("/healthz").status_code == 200
    assert spans.names() == ()
    assert service.guard.rate_limiter.usage("testclient").used == 0


# --- Settlement, which cannot be forgotten ----------------------------------


def test_a_turn_settles_its_own_tokens(limits: SpendLimits) -> None:
    """The caller does not get the chance to forget, so the ceiling counts tokens."""
    model = ScriptedModel(answer("Sure.", prompt_tokens=90, completion_tokens=11))
    service = Service(SpendGate(SpendGuard(limits), lambda: model))
    with TestClient(create_app(service)) as client:
        client.post("/api/chat", json={"message": "hello"})
    assert service.guard.ledger.global_usage().used == 101
