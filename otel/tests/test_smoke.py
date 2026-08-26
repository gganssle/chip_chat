"""The demo session is the local dev loop's evidence, so its shape is a test.

`make dev` sends this session to Phoenix and a developer reads the tree that
comes out. That only proves anything if the tree is the one RFC-001 section 09
describes, so the trees are asserted here rather than eyeballed there.
"""

import textwrap

import pytest

from chip_chat.otel.attributes import ChipChatAttributes, SpanAttributes
from chip_chat.otel.config import TelemetryConfig
from chip_chat.otel.schema import OPS_SPAN_PREFIX, TOOL_SPAN_PREFIX, SpanName
from chip_chat.otel.smoke import (
    account_turn,
    emit_demo_session,
    knowledge_turn,
    main,
    new_session_id,
    vision_order_turn,
)
from chip_chat.otel.spans import TokenUsage
from chip_chat.otel.testing import span_recorder

SESSION = "test-session"


def test_the_knowledge_turn_emits_a_retrieval_under_its_tool() -> None:
    with span_recorder() as recorder:
        knowledge_turn(SESSION)

    assert (
        recorder.tree_text()
        == textwrap.dedent(
            """
        chat.turn
          guard.budget_check
          guard.content_safety
          agent.step
            llm.completion
            tool.search_menu_knowledge
              retriever.search
          agent.step
            llm.completion
          render.response
        """
        ).strip()
    )


def test_the_account_turn_emits_cortex_analyst_and_a_childless_tool() -> None:
    with span_recorder() as recorder:
        account_turn(SESSION)

    assert (
        recorder.tree_text()
        == textwrap.dedent(
            """
        chat.turn
          guard.budget_check
          agent.step
            llm.completion
            tool.ask_account_question
              db.cortex_analyst
            tool.get_usual_order
          agent.step
            llm.completion
          render.response
        """
        ).strip()
    )


def test_the_vision_turn_emits_the_write_under_its_tool() -> None:
    with span_recorder() as recorder:
        vision_order_turn(SESSION)

    assert (
        recorder.tree_text()
        == textwrap.dedent(
            """
        chat.turn
          guard.budget_check
          guard.content_safety
          agent.step
            llm.completion
            tool.match_meal_from_photo
              vision.describe
              matcher.resolve
          agent.step
            llm.completion
            tool.propose_order
          agent.step
            llm.completion
            tool.place_order
              ops.place_order
          render.response
        """
        ).strip()
    )


def _node(name: str) -> SpanName:
    """Map an emitted span name back to its node in the schema."""
    if name.startswith(OPS_SPAN_PREFIX):
        return SpanName.OPS
    if name.startswith(TOOL_SPAN_PREFIX):
        return SpanName.TOOL
    return SpanName(name)


def test_the_demo_session_reaches_every_node_of_the_schema() -> None:
    # The point of the demo: a developer wiring up a backend should see every
    # span shape it will ever have to render, not a subset that happens to work.
    with span_recorder() as recorder:
        emit_demo_session(SESSION)

    assert {_node(name) for name in recorder.names()} == set(SpanName)


EXPECTED_TURN_TOKENS = {
    "knowledge_turn": TokenUsage(prompt_tokens=2_052, completion_tokens=112),
    "account_turn": TokenUsage(prompt_tokens=2_292, completion_tokens=128),
    # 1_020 + 1_402 + 1_540 for the three round trips, plus the vision call's
    # 274/91 -- which is the number the fixture would lose if the photo lane's
    # tokens ever stopped reaching the turn.
    "vision_order_turn": TokenUsage(prompt_tokens=4_236, completion_tokens=251),
}
"""What each demo turn costs, written out rather than measured.

Spelled out on purpose: passing ``llm_token_usage()`` back into
``assert_token_counts_sum`` would compare the measurement to itself and exercise
only half the check. These are the figures in ``smoke.py``, added up by hand, so
a call whose counts moved fails here rather than agreeing with itself.
"""


def test_every_turn_reconciles_its_rollup_against_its_model_calls() -> None:
    """The demo is what a consumer runs to check the schema, so it must add up.

    ``assert_token_counts_sum`` is the check this package asks its consumers to
    run over their own turns. A fixture that failed it -- or that quietly left
    ``vision.describe`` uncounted -- would be teaching the wrong thing to every
    reader of the trace it produces.
    """
    for turn in (knowledge_turn, account_turn, vision_order_turn):
        with span_recorder() as recorder:
            turn(SESSION)
        recorder.assert_token_counts_sum(EXPECTED_TURN_TOKENS[turn.__name__])


def test_the_vision_span_is_counted_like_any_other_model_call() -> None:
    """It is an LLM span, and the photo lane is the expensive lane."""
    with span_recorder() as recorder:
        vision_order_turn(SESSION)

    vision = recorder.attributes_of("vision.describe")
    assert vision[SpanAttributes.LLM_TOKEN_COUNT_TOTAL] == 365
    # And the tool span above it carries the lane's rollup, under our own key.
    tool = recorder.attributes_of("tool.match_meal_from_photo")
    assert tool[ChipChatAttributes.TOKENS_TOTAL] == 365
    assert SpanAttributes.LLM_TOKEN_COUNT_TOTAL not in tool


def test_every_turn_carries_the_session_it_was_given() -> None:
    with span_recorder() as recorder:
        emit_demo_session(SESSION)

    attributes = [dict(span.attributes or {}) for span in recorder.finished_spans()]
    assert {each["session.id"] for each in attributes} == {SESSION}
    assert {each["chip_chat.turn.index"] for each in attributes} == {0, 1, 2}


def test_session_ids_do_not_collide_between_runs() -> None:
    assert new_session_id() != new_session_id()


def test_main_refuses_to_export_nowhere(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Exporting into the void is the failure this tool exists to detect, so it
    # is the one thing `main` must not do quietly.
    monkeypatch.setattr(
        TelemetryConfig, "from_env", classmethod(lambda cls, component: cls(component))
    )

    assert main() == 1
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" in capsys.readouterr().err
