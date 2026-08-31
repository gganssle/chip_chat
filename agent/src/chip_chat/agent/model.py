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
from collections.abc import Callable, Mapping, Sequence
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

    usage_reported: bool = True
    """Whether the counts above came from the provider or are simply unknown.

    Always ``True`` on a non-streamed call, which carries a usage block or a
    plain absence of one. It exists for the streamed path: a stream reports
    usage only in a final chunk that has to be asked for, and a provider or an
    API version that declines to send it would otherwise hand the ledger a
    confident ``0`` for a turn that really cost thousands.

    Zero-that-means-unknown is exactly how an inline spend cap stops being one.
    So the flag is carried out to :mod:`chip_chat.api.turns`, which charges the
    pessimistic per-turn reservation instead of believing the zero. Over-
    counting by less than one turn is the safe direction to be wrong in.
    """

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
        on_text: Callable[[str], None] | None = None,
    ) -> ModelReply:
        """Make one round trip.

        Args:
            messages: OpenAI-shaped messages, oldest first.
            tools: OpenAI-shaped tool definitions the model may call.
            on_text: Called with each fragment of assistant prose as it
                arrives. Passing it asks the implementation to stream; passing
                ``None`` asks for one finished reply. An implementation is free
                to ignore it -- a fake that returns a canned answer has nothing
                to stream -- but one that honours it must still return the same
                complete :class:`ModelReply` at the end, because every caller
                downstream reads the whole reply off the return value and only
                the visitor's eyes read the fragments.

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
        on_text: Callable[[str], None] | None = None,
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
        if on_text is not None:
            return self._streamed(request, on_text)
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

    def _streamed(
        self, request: dict[str, Any], on_text: Callable[[str], None]
    ) -> ModelReply:
        """Make the same round trip, forwarding prose as the provider writes it.

        **Every step is streamed, not just the last one**, and that is forced
        rather than chosen: whether a step will answer in prose or ask for a
        tool is not knowable until the deltas start arriving, so the loop
        cannot decide in advance which call to stream. A step that turns out to
        be a tool call simply never calls ``on_text`` -- ``delta.content`` is
        empty on those -- and the visitor sees the dots go on turning, which is
        the truth about what the turn is doing.

        **``include_usage`` is what keeps the spend cap honest.** A stream ends
        with a chunk carrying no choices and a usage block, but only when it is
        asked for. Without it the provider reports nothing and this would
        return zeroes that the ledger would settle against the day's ceiling as
        though the turn were free. The flag is requested here, and
        :attr:`ModelReply.usage_reported` carries whether it actually arrived
        so that the caller can charge the reservation rather than trust a zero.
        """
        request = dict(request)
        request["stream"] = True
        request["stream_options"] = {"include_usage": True}

        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        served_by = ""
        prompt_tokens = 0
        completion_tokens = 0
        usage_reported = False

        for chunk in self._client.chat.completions.create(**request):
            if getattr(chunk, "model", ""):
                served_by = chunk.model
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                # The final chunk. It carries no choices, which is why this is
                # read before the choice indexing below rather than after it.
                prompt_tokens = usage.prompt_tokens or 0
                completion_tokens = usage.completion_tokens or 0
                usage_reported = True
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta is None:
                continue
            text = getattr(delta, "content", None)
            if text:
                content.append(text)
                on_text(text)
            _merge_tool_call_deltas(calls, getattr(delta, "tool_calls", None))

        return ModelReply(
            content="".join(content) or None,
            tool_calls=tuple(_invocations_from_deltas(calls)),
            finish_reason=finish_reason or "stop",
            model=served_by,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usage_reported=usage_reported,
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


def _merge_tool_call_deltas(calls: dict[int, dict[str, Any]], deltas: Any | None) -> None:
    """Accumulate streamed tool-call fragments in place, by index.

    A streamed tool call does not arrive whole. The first delta carries the
    index, the id and the function name; every delta after it carries a slice of
    the argument JSON and nothing else, and the slices are only valid
    concatenated in arrival order. The index is the identity -- not the id,
    which is absent from every fragment after the first -- which is why this
    keys on it.

    Nothing is parsed here. A half-arrived argument string is not JSON and
    trying to read it early is how a streamed tool call becomes a decoding error
    in the middle of a turn; :func:`_invocations_from_deltas` parses once, at the
    end, with the same tolerance :func:`_invocations` has always had.
    """
    for delta in deltas or ():
        index = getattr(delta, "index", 0) or 0
        call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if getattr(delta, "id", None):
            call["id"] = delta.id
        function = getattr(delta, "function", None)
        if function is None:
            continue
        if getattr(function, "name", None):
            call["name"] = function.name
        if getattr(function, "arguments", None):
            call["arguments"] += function.arguments


def _invocations_from_deltas(
    calls: Mapping[int, Mapping[str, Any]],
) -> Sequence[ToolInvocation]:
    """Turn accumulated fragments into invocations, tolerating bad arguments.

    The same contract as :func:`_invocations`, which this deliberately mirrors:
    arguments that will not parse become an empty mapping and the tool rejects
    them visibly on its own span, rather than raising out of the middle of a
    turn. A streamed call has one extra way to be malformed -- a truncated
    argument string, where the stream ended mid-JSON -- and it lands in exactly
    the same place, which is the point of handling it identically.
    """
    invocations: list[ToolInvocation] = []
    for index in sorted(calls):
        call = calls[index]
        try:
            parsed = json.loads(call.get("arguments") or "{}")
        except json.JSONDecodeError:
            parsed = {}
        invocations.append(
            ToolInvocation(
                call_id=str(call.get("id") or ""),
                name=str(call.get("name") or ""),
                arguments=parsed if isinstance(parsed, dict) else {},
            )
        )
    return invocations
