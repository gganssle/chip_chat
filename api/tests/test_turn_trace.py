"""Issue #64's first and third criteria, asserted on a real turn.

    A complete real turn renders as one readable trace, matching the contract
    test from #14.

    Token counts present on every LLM span, verified to sum to the provider's
    reported usage.

``otel/tests/test_span_tree.py`` is the contract test #14 asked for, and it
emits the tree by hand: it proves the *schema* is coherent, and it would go on
passing if the application stopped emitting half of it. This module drives the
same assertions through :func:`~chip_chat.api.app.create_app` -- a real request,
the real guard, the real agent loop, the real photo lane -- so what is asserted
is that the application emits the tree, not that the tree can be emitted.

The models are doubles, and only the models. Every span in these recordings was
opened by production code.

**On ``guard.content_safety``.** RFC-001 section 09 puts it under ``chat.turn``,
and ``docs/local-tracing.md`` says what it is: *image moderation, before
inference*. A text-only turn therefore does not emit it and is not missing it.
Inbound text screening and prompt shields are Phase 8's hardening work
(``docs/system-design.md``), and a span emitted here to make the tree look
complete would be a trace claiming a check that never ran.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chip_chat.agent.testing import ScriptedModel, answer, calls_tool
from chip_chat.agent.tools import TOOLS
from chip_chat.api.app import Service, create_app
from chip_chat.api.guard import SpendGuard
from chip_chat.api.limits import SpendLimits
from chip_chat.api.turns import SpendGate
from chip_chat.otel import ChipChatAttributes, TokenUsage, ToolName
from chip_chat.otel.attributes import SpanAttributes
from chip_chat.otel.schema import SPAN_NAMES
from chip_chat.otel.testing import SpanRecorder, span_recorder
from chip_chat.vision.lane import PhotoLane
from chip_chat.vision.testing import STUB_PHOTO_REF, STUB_VISION_USAGE, photo_lane

PROMPT_TOKENS = 812
COMPLETION_TOKENS = 64
"""What the scripted model reports per round trip. Distinct, and neither round,
so a test that read the wrong field could not accidentally pass."""


@pytest.fixture
def limits() -> SpendLimits:
    """Ceilings wide enough that nothing here trips them; that is other tests' job."""
    return SpendLimits(
        daily_token_ceiling=1_000_000,
        session_turn_cap=100,
        session_token_cap=1_000_000,
        source_requests_per_window=100,
        source_window_seconds=60.0,
        turn_token_reservation=1_000,
    )


@pytest.fixture
def lane() -> PhotoLane:
    """A real photo lane over doubles, with a photograph already stored."""
    return photo_lane()[0]


def serving(
    model: ScriptedModel, limits: SpendLimits, lane: PhotoLane | None = None
) -> Iterator[TestClient]:
    service = Service(SpendGate(SpendGuard(limits), lambda: model, lane=lane))
    with TestClient(create_app(service)) as client:
        yield client


def say(client: TestClient, message: str, **extra: Any) -> Any:
    return client.post("/api/chat", json={"message": message, **extra}).json()


def scripted(*replies: Any) -> ScriptedModel:
    return ScriptedModel(*replies)


def turn_reply(**kwargs: Any) -> Any:
    return answer("Here you go.", **kwargs)


# --- one readable trace -----------------------------------------------------


def test_a_full_turn_renders_as_the_tree_the_rfc_draws(limits: SpendLimits) -> None:
    """Two lanes and a write in one turn, and every node in its place."""
    model = scripted(
        calls_tool(
            ToolName.SEARCH_MENU_KNOWLEDGE,
            {"query": "barbacoa"},
            prompt_tokens=PROMPT_TOKENS,
            completion_tokens=COMPLETION_TOKENS,
        ),
        calls_tool(
            ToolName.PROPOSE_ORDER,
            {"items": [{"item_id": "BOWL-CHICKEN"}]},
            call_id="call-2",
            prompt_tokens=PROMPT_TOKENS,
            completion_tokens=COMPLETION_TOKENS,
        ),
        turn_reply(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS),
    )
    with span_recorder("api") as spans:
        for client in serving(model, limits):
            say(client, "what's in the barbacoa? and draft me a chicken bowl")

    assert spans.tree_text() == (
        "chat.turn\n"
        "  guard.budget_check\n"
        "  agent.step\n"
        "    llm.completion\n"
        "    tool.search_menu_knowledge\n"
        "      retriever.search\n"
        "  agent.step\n"
        "    llm.completion\n"
        "    tool.propose_order\n"
        "  agent.step\n"
        "    llm.completion\n"
        "  render.response"
    )


def test_every_span_a_real_turn_emits_is_on_the_schema(limits: SpendLimits) -> None:
    """The property the whole package exists for, asserted against the real path."""
    model = scripted(
        calls_tool(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "steak"}),
        turn_reply(),
    )
    with span_recorder("api") as spans:
        for client in serving(model, limits):
            say(client, "tell me about the steak")

    assert set(spans.names()) <= SPAN_NAMES


def test_every_span_carries_the_identity_a_bug_report_arrives_with(
    limits: SpendLimits,
) -> None:
    """Session, persona and turn index, on every node -- not only on the root.

    Application Insights searches attributes far more comfortably than it walks
    trace trees, and "it did something weird" arrives with a session id at best.
    """
    model = scripted(
        calls_tool(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "steak"}),
        turn_reply(),
    )
    with span_recorder("api") as spans:
        for client in serving(model, limits):
            say(client, "tell me about the steak")

    for span in spans.finished_spans():
        attributes = dict(span.attributes or {})
        assert SpanAttributes.SESSION_ID in attributes, span.name
        assert ChipChatAttributes.TURN_INDEX in attributes, span.name
        assert ChipChatAttributes.PERSONA_ID in attributes, span.name


# --- tokens, and that they add up -------------------------------------------


def test_the_llm_spans_sum_to_what_the_provider_reported(limits: SpendLimits) -> None:
    """Criterion three, in the form the helper was written to express."""
    rounds = 3
    model = scripted(
        calls_tool(
            ToolName.SEARCH_MENU_KNOWLEDGE,
            {"query": "barbacoa"},
            prompt_tokens=PROMPT_TOKENS,
            completion_tokens=COMPLETION_TOKENS,
        ),
        calls_tool(
            ToolName.GET_POINTS_BALANCE,
            call_id="call-2",
            prompt_tokens=PROMPT_TOKENS,
            completion_tokens=COMPLETION_TOKENS,
        ),
        turn_reply(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS),
    )
    with span_recorder("api") as spans:
        for client in serving(model, limits):
            say(client, "barbacoa, and my points please")

    spans.assert_token_counts_sum(
        TokenUsage(
            prompt_tokens=PROMPT_TOKENS * rounds,
            completion_tokens=COMPLETION_TOKENS * rounds,
        )
    )


def test_the_turn_root_rolls_the_whole_turn_up(limits: SpendLimits) -> None:
    """So "what did this conversation cost" is a lookup, not a tree walk."""
    model = scripted(
        turn_reply(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS)
    )
    with span_recorder("api") as spans:
        for client in serving(model, limits):
            say(client, "hello")

    turn = spans.attributes_of("chat.turn")
    assert turn[ChipChatAttributes.TOKENS_PROMPT] == PROMPT_TOKENS
    assert turn[ChipChatAttributes.TOKENS_COMPLETION] == COMPLETION_TOKENS
    assert turn[ChipChatAttributes.TOKENS_TOTAL] == PROMPT_TOKENS + COMPLETION_TOKENS
    # The rollup is ours; the OpenInference keys belong to the calls themselves,
    # which is what keeps summing them across a trace meaningful.
    assert SpanAttributes.LLM_TOKEN_COUNT_TOTAL not in turn


def test_each_step_carries_what_that_round_trip_cost(limits: SpendLimits) -> None:
    """The per-step breakdown a runaway loop shows up in."""
    model = scripted(
        calls_tool(
            ToolName.GET_POINTS_BALANCE,
            prompt_tokens=100,
            completion_tokens=10,
        ),
        turn_reply(prompt_tokens=300, completion_tokens=30),
    )
    with span_recorder("api") as spans:
        for client in serving(model, limits):
            say(client, "points please")

    steps = [span for span in spans.finished_spans() if span.name == "agent.step"]
    totals = [
        dict(span.attributes or {})[ChipChatAttributes.TOKENS_TOTAL] for span in steps
    ]
    assert totals == [110, 330]


# --- the photo turn ---------------------------------------------------------


def test_a_photo_turn_holds_image_description_and_skus_in_one_trace(
    limits: SpendLimits, lane: PhotoLane
) -> None:
    """Criterion two, through the request path rather than through the lane."""
    model = scripted(
        calls_tool(
            ToolName.MATCH_MEAL_FROM_PHOTO,
            {"blob_ref": str(STUB_PHOTO_REF)},
            prompt_tokens=PROMPT_TOKENS,
            completion_tokens=COMPLETION_TOKENS,
        ),
        turn_reply(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS),
    )
    with span_recorder("api") as spans:
        for client in serving(model, limits, lane):
            say(client, "order me what's in this photo")

    assert spans.tree_text() == (
        "chat.turn\n"
        "  guard.budget_check\n"
        "  agent.step\n"
        "    llm.completion\n"
        "    tool.match_meal_from_photo\n"
        "      vision.describe\n"
        "      matcher.resolve\n"
        "  agent.step\n"
        "    llm.completion\n"
        "  render.response"
    )
    assert spans.attributes_of("vision.describe")[
        ChipChatAttributes.VISION_IMAGE_REF
    ] == str(STUB_PHOTO_REF)
    assert spans.attributes_of("matcher.resolve")[
        ChipChatAttributes.MATCHER_RESOLVED_SKUS
    ]


def test_a_photo_turn_counts_the_vision_tokens_too(
    limits: SpendLimits, lane: PhotoLane
) -> None:
    """The photo lane is the expensive one, so omitting it is the costly error."""
    model = scripted(
        calls_tool(
            ToolName.MATCH_MEAL_FROM_PHOTO,
            {"blob_ref": str(STUB_PHOTO_REF)},
            prompt_tokens=PROMPT_TOKENS,
            completion_tokens=COMPLETION_TOKENS,
        ),
        turn_reply(prompt_tokens=PROMPT_TOKENS, completion_tokens=COMPLETION_TOKENS),
    )
    with span_recorder("api") as spans:
        for client in serving(model, limits, lane):
            say(client, "order me what's in this photo")

    reported = TokenUsage(
        prompt_tokens=PROMPT_TOKENS * 2 + STUB_VISION_USAGE.prompt_tokens,
        completion_tokens=COMPLETION_TOKENS * 2 + STUB_VISION_USAGE.completion_tokens,
    )
    # The whole criterion, not half of it. Reading llm_token_usage() alone would
    # pass while the turn's rollup and the spend ledger both stopped at the tool
    # boundary -- which is exactly what they did before the lane's tokens were
    # given a way back to the loop.
    spans.assert_token_counts_sum(reported)

    turn = spans.attributes_of("chat.turn")
    assert turn[ChipChatAttributes.TOKENS_TOTAL] == reported.total
    # And the step that made the tool call owns its whole subtree's cost.
    steps = [span for span in spans.finished_spans() if span.name == "agent.step"]
    assert dict(steps[0].attributes or {})[ChipChatAttributes.TOKENS_TOTAL] == (
        PROMPT_TOKENS + COMPLETION_TOKENS + STUB_VISION_USAGE.total
    )


def test_the_model_is_offered_the_photo_tool_only_when_a_lane_is_wired(
    limits: SpendLimits, lane: PhotoLane
) -> None:
    """An unanswerable tool definition reads as a lane outage. Do not offer one."""
    without = scripted(turn_reply())
    with span_recorder("api") as spans:
        for client in serving(without, limits):
            say(client, "hello")
    assert _offered(spans) == {tool.value for tool in TOOLS}
    assert ToolName.MATCH_MEAL_FROM_PHOTO.value not in _offered(spans)

    withal = scripted(turn_reply())
    with span_recorder("api") as spans:
        for client in serving(withal, limits, lane):
            say(client, "hello")
    assert "match_meal_from_photo" in _offered(spans)


def _offered(spans: SpanRecorder) -> set[str]:
    """The tool names ``llm.completion`` says the model was shown."""
    attributes = spans.attributes_of("llm.completion")
    names = set()
    for key, value in attributes.items():
        if key.startswith(SpanAttributes.LLM_TOOLS) and key.endswith("json_schema"):
            names.add(json.loads(str(value))["function"]["name"])
    return names


# --- both backends, one instrumentation --------------------------------------


def test_a_real_turn_reaches_both_backends_identically(limits: SpendLimits) -> None:
    """Criterion four, on the request path rather than on a synthetic turn.

    ``otel/tests/test_export_configuration.py`` proves the fan-out is one
    emission to N exporters. What this adds is that the application is emitting
    through that fan-out rather than through a tracer of its own -- the failure
    mode being a second provider built somewhere in the request path, which
    would look fine in Phoenix and be missing entirely from Application
    Insights.
    """
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from chip_chat.otel import TelemetryConfig, build_tracer_provider
    from chip_chat.otel.tracing import use_tracer_provider

    infrastructure_backend = InMemorySpanExporter()
    agent_backend = InMemorySpanExporter()
    provider = build_tracer_provider(
        TelemetryConfig(component="api"),
        span_processors=(
            SimpleSpanProcessor(infrastructure_backend),
            SimpleSpanProcessor(agent_backend),
        ),
    )
    model = scripted(
        calls_tool(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "barbacoa"}),
        turn_reply(),
    )
    try:
        with use_tracer_provider(provider):
            for client in serving(model, limits):
                say(client, "what's in the barbacoa?")
    finally:
        provider.shutdown()

    def identity(exporter: InMemorySpanExporter) -> list[tuple[str, int]]:
        return sorted(
            (span.name, span.context.span_id)
            for span in exporter.get_finished_spans()
            if span.context is not None
        )

    assert identity(infrastructure_backend) == identity(agent_backend)
    assert [name for name, _ in identity(agent_backend)] == sorted(
        [
            "chat.turn",
            "guard.budget_check",
            "agent.step",
            "llm.completion",
            "tool.search_menu_knowledge",
            "retriever.search",
            "agent.step",
            "llm.completion",
            "render.response",
        ]
    )


# --- the Phase 7 demo criterion ---------------------------------------------


def test_reorder_my_usual_with_extra_guac(limits: SpendLimits) -> None:
    """The demo criterion, in three parts, asserted rather than demonstrated.

        "reorder my usual with extra guac" works, shows what it is about to do
        first, and the whole turn is one readable trace.

    *Works*: the reorder becomes a draft over real item ids, and confirming it
    produces a receipt. *Shows what it is about to do first*: the write is
    refused until the visitor presses the button, so the card comes before the
    order and that ordering is structural rather than prompted -- see
    :mod:`chip_chat.agent.orders`. *One readable trace*: both turns render as
    the tree, below.

    The model is scripted, so what this proves is the path and the trace and not
    that a deployment picks the right tools. That is issue #74's, measured
    against the real deployment.
    """
    model = scripted(
        calls_tool(ToolName.GET_USUAL_ORDER),
        calls_tool(
            ToolName.PROPOSE_ORDER,
            {
                "items": [
                    {"item_id": "BOWL-CHICKEN", "quantity": 1},
                    {"item_id": "SIDE-GUACAMOLE", "quantity": 2},
                ]
            },
            call_id="call-2",
        ),
        answer("That is your usual with an extra guac. Press Confirm."),
    )
    with span_recorder("api") as spans:
        for client in serving(model, limits):
            drafted = say(client, "reorder my usual with extra guac")

            # It showed what it was about to do, and placed nothing.
            card = drafted["card"]
            assert card["requires_confirmation"] is True
            assert drafted["receipt"] is False
            assert [line["item_id"] for line in card["lines"]] == [
                "BOWL-CHICKEN",
                "SIDE-GUACAMOLE",
            ]
            # "extra guac" is a second unit of the guacamole line rather than a
            # new item, which is what `get_usual_order` tells the model to do and
            # what the draft holds. The model is scripted here, so what this
            # asserts is the path and the draft; that a deployment picks the
            # quantity is #74's, measured against the real thing.
            assert card["lines"][1]["quantity"] == 2

            drafting = spans.tree_text()
            spans.clear()

            model.queue(
                calls_tool(
                    ToolName.PLACE_ORDER,
                    {"draft_id": card["draft_id"]},
                    call_id="call-3",
                ),
                answer("Ordered -- simulated, of course."),
            )
            placed = say(client, "yes", confirm_draft_id=card["draft_id"])

    assert placed["receipt"] is True
    assert str(placed["card"]["order_id"]).startswith("CC-")

    assert drafting == (
        "chat.turn\n"
        "  guard.budget_check\n"
        "  agent.step\n"
        "    llm.completion\n"
        "    tool.get_usual_order\n"
        "  agent.step\n"
        "    llm.completion\n"
        "    tool.propose_order\n"
        "  agent.step\n"
        "    llm.completion\n"
        "  render.response"
    )
    assert spans.tree_text() == (
        "chat.turn\n"
        "  guard.budget_check\n"
        "  agent.step\n"
        "    llm.completion\n"
        "    tool.place_order\n"
        "      ops.place_order\n"
        "  agent.step\n"
        "    llm.completion\n"
        "  render.response"
    )
    assert (
        spans.attributes_of("ops.place_order")[ChipChatAttributes.OPS_CONFIRMATION_STATE]
        == "confirmed"
    )
