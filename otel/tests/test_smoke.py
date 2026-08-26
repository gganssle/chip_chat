"""The demo session is the local dev loop's evidence, so its shape is a test.

`make dev` sends this session to Phoenix and a developer reads the tree that
comes out. That only proves anything if the tree is the one RFC-001 section 09
describes, so the trees are asserted here rather than eyeballed there.
"""

import textwrap

import pytest

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
