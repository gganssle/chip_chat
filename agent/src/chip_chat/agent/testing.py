"""A chat model that buys nothing, for the tests that must not buy anything.

:class:`~chip_chat.api.testing.RecordingModel` covers the guard's own tests,
where the only question is whether a call happened at all. The loop needs one
step further: a model that can be told to ask for a tool, so that a whole turn
-- tool call, tool result, second round trip, answer -- can be driven without a
deployment, a credential or a token.

That is what :class:`ScriptedModel` is. It hands back the replies it was given,
in order, and keeps every message list it was handed, so a test can assert both
on what the model was shown and on what the turn did with what it said.

Ships with the package for the same reason :mod:`chip_chat.otel.testing` does:
the API package's tests drive whole turns through this, and a double that lived
under ``agent/tests`` would not be importable from there.
"""

import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from chip_chat.agent.model import ModelReply, ToolInvocation
from chip_chat.otel import ToolName

__all__ = ["ScriptedModel", "answer", "calls_tool"]


class ScriptedModel:
    """Returns pre-built replies in order, and records what it was asked.

    Raises rather than looping when the script runs out: a turn that asked for
    more round trips than the test wrote is a test that is no longer describing
    what it thinks it is.
    """

    __slots__ = ("_deployment", "_lock", "_replies", "requests")

    def __init__(self, *replies: ModelReply, deployment: str = "gpt-test-mini") -> None:
        """Initialise the double.

        Args:
            replies: What :meth:`complete` returns, one per round trip.
            deployment: What :attr:`deployment` reports, as ``llm.completion``
                records it.
        """
        self._replies = list(replies)
        self._deployment = deployment
        self._lock = threading.Lock()
        self.requests: list[Sequence[Mapping[str, Any]]] = []

    @property
    def deployment(self) -> str:
        return self._deployment

    @property
    def call_count(self) -> int:
        """How many round trips were made. Zero is the assertion that matters."""
        with self._lock:
            return len(self.requests)

    def queue(self, *replies: ModelReply) -> None:
        """Add more replies to the end of the script.

        For a test that cannot know a reply until an earlier turn has run --
        the ``draft_id`` a tool minted, most of the time.
        """
        with self._lock:
            self._replies.extend(replies)

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
        on_text: Callable[[str], None] | None = None,
    ) -> ModelReply:
        """Record the request and return the next scripted reply.

        ``on_text`` is honoured rather than ignored, and the scripted content
        is handed over in one fragment. A fake that accepted the callback and
        never called it would let a test pass while the streamed route
        delivered nothing to the browser, which is the one bug this seam is
        supposed to make impossible to ship. One fragment rather than several
        because the number of fragments is the provider's business and no
        caller may depend on it.

        Raises:
            AssertionError: If the script has run out.
        """
        with self._lock:
            self.requests.append([dict(message) for message in messages])
            if not self._replies:
                raise AssertionError(
                    f"ScriptedModel ran out of replies after {len(self.requests)} "
                    "round trips"
                )
            reply = self._replies.pop(0)
        if on_text is not None and reply.content:
            on_text(reply.content)
        return reply


def calls_tool(
    tool: ToolName,
    arguments: Mapping[str, Any] | None = None,
    *,
    call_id: str = "call-1",
    prompt_tokens: int = 900,
    completion_tokens: int = 40,
) -> ModelReply:
    """Build a reply that asks for one tool call."""
    return ModelReply(
        content=None,
        tool_calls=(
            ToolInvocation(
                call_id=call_id, name=tool.value, arguments=dict(arguments or {})
            ),
        ),
        finish_reason="tool_calls",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def answer(
    text: str, *, prompt_tokens: int = 1_100, completion_tokens: int = 60
) -> ModelReply:
    """Build a reply that answers and asks for nothing."""
    return ModelReply(
        content=text,
        finish_reason="stop",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
