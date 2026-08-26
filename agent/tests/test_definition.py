"""The agent definition: eleven tools, a configured deployment, a live version.

The interesting assertion is the negative one -- an agent missing a tool does
not exist, because a lane silently answering from the wrong tool looks like a
model problem and is not one.
"""

import pytest

from chip_chat.agent import SERVICE_NAME
from chip_chat.agent.definition import AgentDefinition, AgentDefinitionError
from chip_chat.agent.foundry import FoundryConfig
from chip_chat.agent.prompt import load
from chip_chat.agent.surface import TOOL_SPECS, spec
from chip_chat.otel import chat_turn
from chip_chat.otel.schema import ToolName
from chip_chat.otel.testing import span_recorder

CONFIG = FoundryConfig.from_env(
    {
        "CHIP_CHAT_FOUNDRY_ENDPOINT": "https://aif-example.cognitiveservices.azure.com/",
        "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT": "gpt-5-mini",
        "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT": "gpt-4.1-mini",
    }
)


def test_the_agent_registers_all_eleven_tools() -> None:
    definition = AgentDefinition.build(CONFIG)

    registered = [tool["function"]["name"] for tool in definition.tool_definitions()]
    assert registered == [name.value for name in ToolName]


def test_an_agent_missing_a_tool_cannot_be_constructed() -> None:
    """Ten tools is not a smaller agent; it is a lane that stopped working."""
    ten = tuple(tool for tool in TOOL_SPECS if tool.name is not ToolName.GET_USUAL_ORDER)

    with pytest.raises(AgentDefinitionError, match="get_usual_order"):
        AgentDefinition(
            name=SERVICE_NAME,
            model_deployment="gpt-5-mini",
            prompt=load(),
            tools=ten,
        )


def test_a_tool_registered_twice_is_refused_too() -> None:
    with pytest.raises(AgentDefinitionError, match="place_order"):
        AgentDefinition(
            name=SERVICE_NAME,
            model_deployment="gpt-5-mini",
            prompt=load(),
            tools=(*TOOL_SPECS, spec(ToolName.PLACE_ORDER)),
        )


def test_the_deployment_is_configuration_and_never_a_literal() -> None:
    """Issue #8's criterion, inherited: a Phase 9 experiment is an env change."""
    candidate = FoundryConfig.from_env(
        {
            "CHIP_CHAT_FOUNDRY_ENDPOINT": "https://aif-example.cognitiveservices.azure.com/",
            "CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT": "gpt-4.1-mini-candidate",
            "CHIP_CHAT_FOUNDRY_VISION_DEPLOYMENT": "gpt-4.1-mini",
        }
    )

    assert AgentDefinition.build(candidate).model_deployment == "gpt-4.1-mini-candidate"


def test_the_agent_is_named_what_foundry_will_force_as_service_name() -> None:
    """``docs/decisions/foundry-agent-shape.md``: ``OTEL_SERVICE_NAME`` is ignored.

    Naming the agent anything else means discovering in Phase 9 that one turn's
    trace carries a service name no dashboard filters on.
    """
    assert AgentDefinition.build(CONFIG).name == SERVICE_NAME == "chip-chat-agent"


def test_a_container_can_say_which_prompt_it_is_running() -> None:
    """From the objects it loaded, not from the environment it was handed.

    A container that reports its configuration reports what it was *asked* to
    run. This reports what it *is* running, which is the only version worth
    putting next to a trace.
    """
    definition = AgentDefinition.build(CONFIG)
    described = definition.describe()

    assert described["prompt_version"] == definition.prompt_version
    assert described["model"] == "gpt-5-mini"
    assert len(described["tools"]) == len(ToolName)


def test_the_definition_is_not_a_foundry_registration_document() -> None:
    """A hosted agent's resource carries an image and an environment, no prompt.

    Registering an agent whose document had ``instructions`` in it would be the
    *prompt agent* shape, which decision D8 rejected -- and the mistake would
    only surface as spans that never reach Arize.
    """
    described = AgentDefinition.build(CONFIG).describe()

    assert "instructions" not in described
    assert "container" not in described


def test_the_prompt_version_reaches_the_chat_turn_span() -> None:
    """Issue #60's second acceptance criterion, end to end.

    An Arize experiment groups on ``chip_chat.prompt.version`` to attribute a
    score change to a prompt, so the value has to be on the root span of every
    turn and not only in a config file somewhere.
    """
    definition = AgentDefinition.build(CONFIG)

    with (
        span_recorder() as spans,
        chat_turn(
            session_id="s1",
            turn_index=0,
            message="is the barbacoa spicy?",
            prompt_version=definition.prompt_version,
        ) as turn,
    ):
        turn.record_output("Moderately.")

    attributes = spans.attributes_of("chat.turn")
    assert attributes["chip_chat.prompt.version"] == definition.prompt_version
