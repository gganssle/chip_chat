"""One model round trip, in the smallest shape the loop and a test both accept.

:class:`ChatModel` is a protocol with a single method, and the reason it exists
is the acceptance criterion the spend cap was built around: *assert on a mock
that would record the call, not on the response text*. A loop that reached for
``AzureOpenAI`` directly could not be driven past its ceiling in a unit test
without buying tokens, so the seam is here rather than at the SDK.

:class:`AzureChatModel` is the real implementation and does nothing but
translate: OpenAI-shaped messages in, :class:`ModelReply` out. The deployment it
calls is read from configuration -- see :mod:`chip_chat.agent.foundry` for why
the deployment name is never a literal.

Everything the model reports about *itself* -- the model that actually served
the request, the finish reason, the token counts -- is carried on the reply
rather than looked up afterwards, because ``llm.completion`` records all three
and a span that had to guess at any of them would be worse than no span.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from chip_chat.agent.foundry import FoundryConfig, chat_client
from chip_chat.otel import ToolName

__all__ = [
    "AzureChatModel",
    "ChatModel",
    "ModelReply",
    "ToolInvocation",
    "UnknownToolError",
]


class UnknownToolError(ValueError):
    """The model asked for a tool that is not in the schema's enumeration.

    A runtime condition rather than a programming error: a model can emit any
    string it likes, and the loop answers it with a tool result saying so. It is
    never allowed to become a span name -- that is what
    :class:`~chip_chat.otel.schema.ToolName` is for.
    """


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """One tool call the model asked for."""

    call_id: str
    """The model's own id for the call, echoed back on the tool result message."""

    name: str
    """The tool as the model named it, which may not be a real tool."""

    arguments: Mapping[str, Any] = field(default_factory=dict)

    @property
    def tool(self) -> ToolName:
        """The schema's enumeration member for :attr:`name`.

        Raises:
            UnknownToolError: If the model named something that is not a tool.
        """
        try:
            return ToolName(self.name)
        except ValueError as error:
            raise UnknownToolError(
                f"{self.name!r} is not one of the eleven tools"
            ) from error


@dataclass(frozen=True, slots=True)
class ModelReply:
    """What one round trip returned, including what it cost."""

    content: str | None
    tool_calls: tuple[ToolInvocation, ...] = ()
    finish_reason: str = "stop"
    model: str = ""
    """The model the service reports having served, which is not the deployment
    name the call asked for the moment an eval experiment swaps one."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatModel(Protocol):
    """A chat completion with tools. The only thing the loop needs from a model."""

    @property
    def deployment(self) -> str:
        """The deployment name, as ``llm.completion`` records it."""
        ...

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> ModelReply:
        """Make one round trip.

        Args:
            messages: OpenAI-shaped messages, oldest first.
            tools: OpenAI-shaped tool definitions the model may call.

        Returns:
            What came back, with its token counts.
        """
        ...


class AzureChatModel:
    """:class:`ChatModel` over the Foundry chat deployment.

    The client is built once and the deployment passed per call, which is how
    :func:`chip_chat.agent.foundry.chat_client` is meant to be used -- one client
    serves both lanes.
    """

    __slots__ = ("_client", "_config", "_deployment", "_max_completion_tokens")

    def __init__(
        self, config: FoundryConfig, *, max_completion_tokens: int = 2_000
    ) -> None:
        """Bind to the chat deployment ``config`` names.

        Args:
            config: Where the models are and how to authenticate.
            max_completion_tokens: Ceiling on one reply. A second line of
                defence under the spend cap rather than a substitute for it:
                this bounds a single call, the cap bounds the day.
        """
        self._config = config
        self._deployment = config.deployment_for("chat")
        self._max_completion_tokens = max_completion_tokens
        self._client = chat_client(config)

    @property
    def deployment(self) -> str:
        return self._deployment

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> ModelReply:
        """Make one round trip against the Foundry chat deployment."""
        request: dict[str, Any] = {
            "model": self._deployment,
            "messages": list(messages),
            "max_completion_tokens": self._max_completion_tokens,
        }
        if tools:
            request["tools"] = list(tools)
            request["tool_choice"] = "auto"
        response = self._client.chat.completions.create(**request)
        choice = response.choices[0]
        usage = response.usage
        return ModelReply(
            content=choice.message.content,
            tool_calls=tuple(_invocations(choice.message)),
            finish_reason=choice.finish_reason or "stop",
            model=response.model,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )


def _invocations(message: Any) -> Sequence[ToolInvocation]:
    """Read tool calls off an SDK message, tolerating malformed arguments.

    A model that emits arguments which are not JSON is a thing that happens, and
    the loop's answer is to hand the tool an empty mapping and let the tool
    reject it -- which is visible on the tool span -- rather than to raise out of
    the middle of a turn.
    """
    calls = getattr(message, "tool_calls", None) or ()
    invocations: list[ToolInvocation] = []
    for call in calls:
        raw = getattr(call.function, "arguments", "") or "{}"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {}
        invocations.append(
            ToolInvocation(
                call_id=call.id,
                name=call.function.name,
                arguments=parsed if isinstance(parsed, dict) else {},
            )
        )
    return invocations
