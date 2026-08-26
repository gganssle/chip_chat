"""The span vocabulary is a contract. These tests are the contract's teeth."""

import re
from pathlib import Path

import pytest

from chip_chat.otel.schema import (
    OPS_SPAN_PREFIX,
    SPAN_NAMES,
    TOOL_SPAN_PREFIX,
    WRITE_TOOLS,
    OpsAction,
    SpanName,
    ToolName,
    allowed_parents,
    ops_span_name,
    span_kind,
    tool_span_name,
)

# RFC-001 section 09, transcribed. A change here must be a deliberate schema
# change, reviewed as one -- which is exactly why it is spelled out rather than
# derived from the code it is checking.
EXPECTED_FIXED_SPANS = {
    "chat.turn",
    "guard.budget_check",
    "guard.content_safety",
    "agent.step",
    "llm.completion",
    "retriever.search",
    "db.cortex_analyst",
    "vision.describe",
    "matcher.resolve",
    "render.response",
}

# RFC-001 section 06. Eleven tools, no more and no fewer.
EXPECTED_TOOLS = {
    "search_menu_knowledge",
    "ask_account_question",
    "get_points_balance",
    "get_usual_order",
    "get_recommendations",
    "match_meal_from_photo",
    "propose_order",
    "place_order",
    "cancel_order",
    "redeem_points",
    "update_preferences",
}

EXPECTED_OPS_ACTIONS = {
    "place_order",
    "cancel_order",
    "redeem_points",
    "update_preferences",
}


def test_fixed_span_names_match_the_rfc() -> None:
    fixed = {name.value for name in SpanName if name not in (SpanName.TOOL, SpanName.OPS)}
    assert fixed == EXPECTED_FIXED_SPANS


def test_there_are_exactly_eleven_tools() -> None:
    assert {tool.value for tool in ToolName} == EXPECTED_TOOLS
    assert len(ToolName) == 11


def test_ops_actions_are_the_four_writes() -> None:
    assert {action.value for action in OpsAction} == EXPECTED_OPS_ACTIONS
    assert {tool.value for tool in WRITE_TOOLS} == EXPECTED_OPS_ACTIONS


def test_span_names_expands_both_templates() -> None:
    assert (
        EXPECTED_FIXED_SPANS
        | {f"{TOOL_SPAN_PREFIX}{tool}" for tool in EXPECTED_TOOLS}
        | {f"{OPS_SPAN_PREFIX}{action}" for action in EXPECTED_OPS_ACTIONS}
    ) == SPAN_NAMES


def test_templates_are_not_emittable_names() -> None:
    # The '*' is the point: a template that leaked into a span name should look
    # obviously wrong in a trace rather than quietly become an axis.
    assert SpanName.TOOL.value not in SPAN_NAMES
    assert SpanName.OPS.value not in SPAN_NAMES


@pytest.mark.parametrize("tool", list(ToolName))
def test_tool_span_names_are_prefixed(tool: ToolName) -> None:
    assert tool_span_name(tool) == f"tool.{tool.value}"


@pytest.mark.parametrize("action", list(OpsAction))
def test_ops_span_names_are_prefixed(action: OpsAction) -> None:
    assert ops_span_name(action) == f"ops.{action.value}"


def test_every_node_declares_its_parents() -> None:
    for name in SpanName:
        assert allowed_parents(name), f"{name} has no declared parent"


def test_only_chat_turn_may_be_a_root() -> None:
    roots = {name for name in SpanName if None in allowed_parents(name)}
    assert roots == {SpanName.CHAT_TURN}


def test_every_node_declares_an_openinference_kind() -> None:
    for name in SpanName:
        assert span_kind(name).value


def test_span_names_are_lowercase_dotted() -> None:
    pattern = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    for name in SPAN_NAMES:
        assert pattern.match(name), name


def test_the_readme_documents_every_span_name() -> None:
    # The README is the schema of record for humans. If a name exists in code
    # and not in the README, one of the two is lying.
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    for name in sorted(SPAN_NAMES):
        assert name in readme, f"{name} is missing from otel/README.md"
