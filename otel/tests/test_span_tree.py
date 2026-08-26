"""The contract test: a synthetic turn must emit exactly the RFC-001 tree."""

import textwrap

import pytest
from openinference.semconv.trace import (
    OpenInferenceSpanKindValues,
    SpanAttributes,
)

from chip_chat.otel.attributes import (
    ChipChatAttributes,
    ConfirmationState,
    DbAttributes,
    GuardOutcome,
)
from chip_chat.otel.schema import OpsAction, ToolName
from chip_chat.otel.spans import (
    Document,
    Message,
    agent_step,
    budget_check,
    chat_turn,
    content_safety,
    cortex_analyst_query,
    current_turn,
    llm_completion,
    matcher_resolve,
    ops_write,
    render_response,
    retriever_search,
    tool_call,
    vision_describe,
)
from chip_chat.otel.testing import span_recorder


def emit_full_turn() -> None:
    """One turn that exercises every node of the schema exactly once."""
    with chat_turn(
        session_id="sess-1",
        turn_index=0,
        message="what's in the barbacoa?",
        persona_id="persona-7",
        demo_id="demo-42",
    ) as turn:
        with budget_check() as budget:
            budget.record_budget(scope="session", tokens_used=120, tokens_limit=8000)
            budget.allow()
        with content_safety() as safety:
            safety.allow()
        with agent_step(index=0) as step:
            with llm_completion(model="gpt-4o", provider="azure", system="openai") as llm:
                llm.record_input_messages([Message(role="user", content="hello")])
                llm.record_output_messages([Message(role="assistant", content="hi")])
                llm.record_tools([{"name": "search_menu_knowledge"}])
                llm.record_usage(prompt_tokens=812, completion_tokens=64)
                llm.record_finish_reason("tool_calls")
            with tool_call(
                ToolName.SEARCH_MENU_KNOWLEDGE, arguments={"query": "barbacoa"}
            ) as tool:
                with retriever_search(query="barbacoa", index="menu-v3") as search:
                    search.record_documents(
                        [
                            Document(
                                id="doc-1",
                                content="Barbacoa is beef.",
                                score=0.91,
                                metadata={"source_url": "https://example.test/menu"},
                            )
                        ]
                    )
                tool.record_result({"passages": 1})
            with tool_call(
                ToolName.ASK_ACCOUNT_QUESTION, arguments={"question": "spend"}
            ) as tool:
                with cortex_analyst_query(question="how much did I spend?") as analyst:
                    analyst.record_query(sql="select 1", row_count=1)
                tool.record_result({"rows": 1})
            with tool_call(
                ToolName.MATCH_MEAL_FROM_PHOTO, arguments={"blob_ref": "b/1"}
            ) as tool:
                with vision_describe(image_ref="b/1", model="gpt-4o-vision") as vision:
                    vision.record_description({"vessel": "bowl", "meals_visible": 1})
                with matcher_resolve() as matcher:
                    matcher.record_slots({"vessel": ("bowl", 0.97)})
                    matcher.record_resolved_skus(["SKU-1"])
                tool.record_result({"draft_id": "draft-9"})
            with tool_call(
                ToolName.PLACE_ORDER, arguments={"draft_id": "draft-9"}
            ) as tool:
                with ops_write(OpsAction.PLACE_ORDER, reference_id="draft-9") as ops:
                    ops.record_confirmation(ConfirmationState.CONFIRMED)
                    ops.record_receipt({"order_id": "ord-3"})
                tool.record_result({"order_id": "ord-3"})
            step.record_output("ordered")
        with render_response() as render:
            render.record_output("Done — barbacoa bowl on its way.")
        turn.record_output("Done — barbacoa bowl on its way.")


def test_a_full_turn_emits_exactly_the_rfc_tree() -> None:
    with span_recorder() as recorder:
        emit_full_turn()

    # Renaming any span in chip_chat.otel.schema fails right here.
    assert recorder.tree_text() == textwrap.dedent(
        """\
        chat.turn
          guard.budget_check
          guard.content_safety
          agent.step
            llm.completion
            tool.search_menu_knowledge
              retriever.search
            tool.ask_account_question
              db.cortex_analyst
            tool.match_meal_from_photo
              vision.describe
              matcher.resolve
            tool.place_order
              ops.place_order
          render.response"""
    )


def test_every_span_carries_an_openinference_kind() -> None:
    with span_recorder() as recorder:
        emit_full_turn()
    for span in recorder.finished_spans():
        attributes = dict(span.attributes or {})
        assert SpanAttributes.OPENINFERENCE_SPAN_KIND in attributes, span.name


def test_kinds_are_what_arize_needs_to_read_the_span() -> None:
    with span_recorder() as recorder:
        emit_full_turn()

    def kind(name: str) -> str:
        return str(recorder.attributes_of(name)[SpanAttributes.OPENINFERENCE_SPAN_KIND])

    assert kind("chat.turn") == OpenInferenceSpanKindValues.CHAIN.value
    assert kind("guard.budget_check") == OpenInferenceSpanKindValues.GUARDRAIL.value
    assert kind("agent.step") == OpenInferenceSpanKindValues.AGENT.value
    assert kind("llm.completion") == OpenInferenceSpanKindValues.LLM.value
    assert kind("tool.place_order") == OpenInferenceSpanKindValues.TOOL.value
    assert kind("retriever.search") == OpenInferenceSpanKindValues.RETRIEVER.value
    assert kind("vision.describe") == OpenInferenceSpanKindValues.LLM.value


def test_llm_completion_records_the_attributes_evals_read() -> None:
    with span_recorder() as recorder:
        emit_full_turn()
    attributes = recorder.attributes_of("llm.completion")

    assert attributes[SpanAttributes.LLM_MODEL_NAME] == "gpt-4o"
    assert attributes[SpanAttributes.LLM_PROVIDER] == "azure"
    assert attributes[SpanAttributes.LLM_TOKEN_COUNT_PROMPT] == 812
    assert attributes[SpanAttributes.LLM_TOKEN_COUNT_COMPLETION] == 64
    assert attributes[SpanAttributes.LLM_TOKEN_COUNT_TOTAL] == 876
    assert attributes[SpanAttributes.LLM_FINISH_REASON] == "tool_calls"
    assert attributes[f"{SpanAttributes.LLM_INPUT_MESSAGES}.0.message.role"] == "user"
    assert attributes[f"{SpanAttributes.LLM_OUTPUT_MESSAGES}.0.message.content"] == "hi"
    assert f"{SpanAttributes.LLM_TOOLS}.0.tool.json_schema" in attributes


def test_retriever_records_documents_with_scores_and_citations() -> None:
    with span_recorder() as recorder:
        emit_full_turn()
    attributes = recorder.attributes_of("retriever.search")

    prefix = f"{SpanAttributes.RETRIEVAL_DOCUMENTS}.0.document"
    assert attributes[f"{prefix}.id"] == "doc-1"
    assert attributes[f"{prefix}.score"] == pytest.approx(0.91)
    assert "source_url" in str(attributes[f"{prefix}.metadata"])


def test_cortex_analyst_records_generated_sql_and_row_count() -> None:
    with span_recorder() as recorder:
        emit_full_turn()
    attributes = recorder.attributes_of("db.cortex_analyst")

    assert attributes[DbAttributes.DB_QUERY_TEXT] == "select 1"
    assert attributes[DbAttributes.DB_RESPONSE_RETURNED_ROWS] == 1
    assert attributes[DbAttributes.DB_SYSTEM] == "snowflake"


def test_tool_spans_record_their_arguments() -> None:
    with span_recorder() as recorder:
        emit_full_turn()
    attributes = recorder.attributes_of("tool.search_menu_knowledge")

    assert attributes[SpanAttributes.TOOL_NAME] == "search_menu_knowledge"
    assert "barbacoa" in str(attributes[SpanAttributes.INPUT_VALUE])


def test_ops_spans_record_the_draft_and_confirmation_state() -> None:
    with span_recorder() as recorder:
        emit_full_turn()
    attributes = recorder.attributes_of("ops.place_order")

    assert attributes[ChipChatAttributes.OPS_REFERENCE_ID] == "draft-9"
    assert (
        attributes[ChipChatAttributes.OPS_CONFIRMATION_STATE]
        == ConfirmationState.CONFIRMED
    )


def test_matcher_records_slot_confidences_and_skus() -> None:
    with span_recorder() as recorder:
        emit_full_turn()
    attributes = recorder.attributes_of("matcher.resolve")

    assert attributes[ChipChatAttributes.MATCHER_SLOT_VALUES] == ("vessel=bowl",)
    assert attributes[ChipChatAttributes.MATCHER_SLOT_CONFIDENCES] == pytest.approx(
        (0.97,)
    )
    assert attributes[ChipChatAttributes.MATCHER_RESOLVED_SKUS] == ("SKU-1",)


def test_every_span_carries_the_session_so_a_report_finds_the_trace() -> None:
    with span_recorder() as recorder:
        emit_full_turn()
    for span in recorder.finished_spans():
        attributes = dict(span.attributes or {})
        assert attributes[SpanAttributes.SESSION_ID] == "sess-1", span.name
        assert attributes[ChipChatAttributes.TURN_INDEX] == 0, span.name
        assert attributes[ChipChatAttributes.DEMO_ID] == "demo-42", span.name


def test_demo_id_is_only_ever_a_correlation_attribute() -> None:
    # It appears on spans and it is reachable from nowhere else in this package:
    # nothing reads it back, and no helper takes a decision from it.
    with span_recorder() as recorder:
        emit_full_turn()
    turn = recorder.attributes_of("chat.turn")
    assert turn[ChipChatAttributes.DEMO_ID] == "demo-42"
    assert turn[SpanAttributes.USER_ID] == "persona-7"


def test_a_blocked_budget_check_is_visible_as_a_block() -> None:
    with span_recorder() as recorder, chat_turn(session_id="s", turn_index=3) as turn:
        with budget_check() as budget:
            budget.record_budget(scope="global", tokens_used=10, tokens_limit=10)
            budget.block("daily_ceiling")
        turn.record_stopped("daily_ceiling")

    attributes = recorder.attributes_of("guard.budget_check")
    assert attributes[ChipChatAttributes.GUARD_OUTCOME] == GuardOutcome.BLOCKED
    assert attributes[ChipChatAttributes.GUARD_REASON] == "daily_ceiling"
    assert attributes[ChipChatAttributes.BUDGET_SCOPE] == "global"


def test_a_rejected_write_marks_the_span_failed() -> None:
    with span_recorder() as recorder, chat_turn(session_id="s", turn_index=0):
        with agent_step(index=0):
            with tool_call(ToolName.PLACE_ORDER, arguments={"draft_id": "d"}):
                with ops_write(OpsAction.PLACE_ORDER, reference_id="d") as ops:
                    ops.record_confirmation(ConfirmationState.REJECTED)

    span = recorder.span_named("ops.place_order")
    assert span.status.is_ok is False


def test_a_failing_lane_records_the_exception_and_the_turn_survives() -> None:
    with span_recorder() as recorder, chat_turn(session_id="s", turn_index=0) as turn:
        with agent_step(index=0):
            with tool_call(ToolName.SEARCH_MENU_KNOWLEDGE) as tool:
                with retriever_search(query="q") as search:
                    search.record_failure(TimeoutError("AI Search unavailable"))
                tool.record_result({"declined": True})
        turn.record_output("I can't look that up right now.")

    assert recorder.span_named("retriever.search").status.is_ok is False
    assert recorder.span_named("chat.turn").status.is_ok is True


def _turn_that_raises_inside_the_model_call() -> None:
    with chat_turn(session_id="s", turn_index=0), agent_step(index=0):
        with llm_completion(model="gpt-4o"):
            raise RuntimeError("boom")


def test_an_exception_escaping_a_helper_is_recorded_on_its_span() -> None:
    with span_recorder() as recorder, pytest.raises(RuntimeError, match="boom"):
        _turn_that_raises_inside_the_model_call()

    llm = recorder.span_named("llm.completion")
    assert llm.status.is_ok is False
    assert [event.name for event in llm.events] == ["exception"]


def test_current_turn_is_visible_inside_a_turn_and_not_outside_it() -> None:
    assert current_turn() is None
    with span_recorder(), chat_turn(session_id="s", turn_index=2, persona_id="p"):
        identity = current_turn()
        assert identity is not None
        assert (identity.session_id, identity.turn_index) == ("s", 2)
    assert current_turn() is None


def test_recorder_reassembles_multiple_turns_as_separate_roots() -> None:
    with span_recorder() as recorder:
        for index in range(2):
            with chat_turn(session_id="s", turn_index=index), budget_check() as guard:
                guard.allow()

    assert (
        recorder.tree_text()
        == "chat.turn\n  guard.budget_check\nchat.turn\n  guard.budget_check"
    )


def test_span_named_refuses_an_ambiguous_lookup() -> None:
    with span_recorder() as recorder:
        for index in range(2):
            with chat_turn(session_id="s", turn_index=index):
                pass
    with pytest.raises(AssertionError, match="exactly one"):
        recorder.span_named("chat.turn")
