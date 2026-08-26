"""The agent: one model, one prompt, eleven tools, one version string.

``docs/decisions/foundry-agent-shape.md`` settled the shape -- a **hosted
agent**, our container run by Foundry Agent Service, on a project provisioned
with basic setup -- and that shape decides where this module stops.

**A hosted agent's resource carries no instructions and no tools.** What is
registered with Foundry is an image reference and an environment;
:mod:`chip_chat.agent.version` renders that manifest and it is deliberately
short. The prompt and the tool schemas live *inside the container*, because in
this shape the container runs the loop. So this module is the definition the
container assembles for itself at start-up, and the thing you would hand to
Foundry is next door.

Three properties are worth naming, because each one is enforced here rather than
remembered elsewhere.

**Eleven, or nothing.** :class:`AgentDefinition` cannot be constructed with ten
tools or with twelve. RFC-001 section 06 fixes the surface, and a tool quietly
missing from a registration is a lane that silently stops working -- the
knowledge lane answering an account question because the account tool was not
there is exactly the tool-selection failure Phase 9 exists to measure, and it
would look like a model problem.

**The version is the loaded bytes.** :attr:`AgentDefinition.prompt_version`
comes off the prompt that was actually read, so the value stamped on
``chat.turn`` cannot drift from the text the model was given. See
:mod:`chip_chat.agent.prompt`.

**The agent's name is the service name.** The decision records that a hosted
agent's ``service.name`` is forced to the agent's name and ``OTEL_SERVICE_NAME``
is ignored. Rather than discover that as a mismatch in Phase 9, the agent is
*named* :data:`chip_chat.agent.SERVICE_NAME` -- so the value Foundry forces is
the value this repo would have chosen, and issue #64's two-``service.name``
trace reads ``chip-chat-api`` and ``chip-chat-agent``.

.. code-block:: python

    definition = AgentDefinition.build(FoundryConfig.from_env())
    with chat_turn(
        session_id=sid, turn_index=n, message=text,
        prompt_version=definition.prompt_version,
    ) as turn:
        ...
"""

from dataclasses import dataclass
from typing import Any

from chip_chat.agent import SERVICE_NAME
from chip_chat.agent.foundry import FoundryConfig
from chip_chat.agent.prompt import REVISION, SystemPrompt, load
from chip_chat.agent.surface import TOOL_SPECS, ToolSpec
from chip_chat.otel.schema import ToolName

__all__ = ["AgentDefinition", "AgentDefinitionError"]


class AgentDefinitionError(RuntimeError):
    """The definition does not describe the agent RFC-001 section 06 specifies."""


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """A single agent with tools, which is decision D1 and not an accident.

    D1's reasoning, kept here because it is the thing most likely to be
    relitigated by someone adding a second agent: a two-second turn budget does
    not accommodate extra model round trips, a public endpoint's token budget
    does not accommodate the multiplier, and one prompt with one trace is
    dramatically easier to debug. Most turns need exactly one tool.
    """

    name: str
    model_deployment: str
    prompt: SystemPrompt
    tools: tuple[ToolSpec, ...]

    def __post_init__(self) -> None:
        registered = [tool.name for tool in self.tools]
        missing = sorted(name.value for name in ToolName if name not in registered)
        if missing:
            raise AgentDefinitionError(
                "every tool in RFC-001 section 06 must be registered; missing: "
                + ", ".join(missing)
            )
        duplicates = sorted(
            {name.value for name in registered if registered.count(name) > 1}
        )
        if duplicates:
            raise AgentDefinitionError(
                "a tool is registered twice: " + ", ".join(duplicates)
            )

    @classmethod
    def build(
        cls,
        config: FoundryConfig,
        *,
        revision: str = REVISION,
        name: str = SERVICE_NAME,
    ) -> "AgentDefinition":
        """Build the definition this repository ships.

        Args:
            config: Where the models are and which deployment answers the chat
                lane. The deployment is read from configuration and never named
                as a literal, which is issue #8's acceptance criterion and the
                thing that makes a Phase 9 prompt-versus-model experiment an
                environment change.
            revision: Which prompt revision to run. An experiment comparing two
                prompts builds two definitions differing only here.
            name: The agent's name, which Foundry also forces as ``service.name``.

        Returns:
            The agent, with all eleven tools registered.
        """
        return cls(
            name=name,
            model_deployment=config.deployment_for("chat"),
            prompt=load(revision),
            tools=TOOL_SPECS,
        )

    @property
    def prompt_version(self) -> str:
        """The value to pass to :func:`chip_chat.otel.chat_turn`."""
        return self.prompt.version

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Return the eleven tools in the wire form the model endpoint takes."""
        return [tool.as_tool_definition() for tool in self.tools]

    def describe(self) -> dict[str, Any]:
        """Return what this container is running, for a log line or a health check.

        Not a registration document -- see the module docstring. It exists so
        that a deployed container can be asked *which prompt and which
        deployment am I actually running*, and answer from the objects it
        loaded rather than from the environment it was configured with.
        """
        return {
            "name": self.name,
            "model": self.model_deployment,
            "prompt_version": self.prompt_version,
            "tools": [tool.name.value for tool in self.tools],
        }
