"""The agent loop: model, tools, model again, until it has an answer.

One ``agent.step`` per round trip, one ``llm.completion`` inside each, and one
``tool.<tool_name>`` per call the model asks for -- which is exactly the tree
RFC-001 section 09 fixes, and is the reason this file is written as the nesting
rather than as a flat sequence of function calls.

What the loop is *not* is the turn. ``chat.turn``, ``guard.budget_check`` and
``render.response`` all belong to the request handler in
:mod:`chip_chat.api.app`, because the budget check has to happen before this
module is reached and the rendered response after it. :func:`run_turn` refuses
to open ``agent.step`` outside a ``chat.turn`` -- the span helpers enforce that,
not this docstring.

**Later, this becomes a hosted Foundry agent.** ``docs/decisions/foundry-agent-shape.md``
settles that the agent runs on the Agent Service with Microsoft-managed threads,
and #64 is where it moves. What survives the move is the span tree and the tool
contracts; what does not is this file's own message list. That is why
:class:`Conversation` holds nothing but messages and a turn counter: it is the
part designed to be thrown away.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from chip_chat.agent.hardcoded import ACCOUNT, MENU, SIMULATION_NOTICE, STORE
from chip_chat.agent.model import ChatModel, ModelReply
from chip_chat.agent.orders import OrderDesk
from chip_chat.agent.prompt import load
from chip_chat.agent.tools import dispatch, offered_schemas, offered_tools
from chip_chat.otel import (
    Message,
    TokenUsage,
    ToolName,
    agent_step,
    llm_completion,
)
from chip_chat.vision.lane import PhotoLane

__all__ = [
    "CONFIRMATION_NOTE",
    "DEFAULT_MAX_STEPS",
    "PROMPT_VERSION",
    "RUNTIME_CONTEXT",
    "SYSTEM_PROMPT",
    "Conversation",
    "TurnResult",
    "run_turn",
]

DEFAULT_MAX_STEPS = 5
"""Round trips one turn may take before the loop stops asking.

A spend control as much as a correctness one. The reservation the spend cap
holds is per *turn*, so an unbounded loop is a turn that can cost an unbounded
amount against a fixed reservation -- which is the one way the ceiling in
:mod:`chip_chat.api.ledger` can be overshot.
"""

_PROVIDER = "azure"
_SYSTEM = "openai"

CONFIRMATION_NOTE = (
    "The visitor has just pressed Confirm on draft {draft_id}. It is now "
    "confirmed and place_order will succeed. Call place_order with that "
    "draft_id now; do not ask them to press Confirm again."
)
"""What the model is told when a draft has actually been confirmed.

The model cannot see the button and has no other way to learn that it was
pressed -- and having been refused once, it will go on refusing, politely and
forever, which is exactly what the first deployed run of this slice did.

Read what this is and is not. It is a *hint*, written by the server, only after
:meth:`chip_chat.agent.orders.OrderDesk.confirm` actually confirmed the draft.
It is not the enforcement: a visitor who posts somebody else's draft id gets no
note and no confirmation, and ``place_order`` refuses them either way. Deleting
this string would make the agent unhelpful; it would not make it unsafe."""

_MENU_LINES = "\n".join(
    f"  {item.item_id} - {item.name} (${item.unit_price})" for item in MENU.values()
)

SYSTEM_PROMPT = load().text
"""The versioned system prompt, from ``prompts/system-{REVISION}.md``.

Invariant across visitors and across turns, which is what makes
:data:`PROMPT_VERSION` mean anything: a digest that moved because a visitor
happened to be called Sam would identify nothing. Everything that *does* vary --
the account, the store, the menu, which tools are actually registered today --
is :data:`RUNTIME_CONTEXT`, a second system message.

That split is the reason issue #60 wants this file versioned at all. The prompt
describes how Cilantro behaves; the context describes what is true right now.
An eval experiment swaps the first and holds the second."""

PROMPT_VERSION = load().version
"""What ``chat.turn`` records. See :mod:`chip_chat.agent.prompt`."""

RUNTIME_CONTEXT = f"""\
Facts about this turn. These change; the instructions above do not.

The visitor is signed in as {ACCOUNT.display_name}, a rewards member at the
{STORE.name} store. You already know who they are, so never ask for a name, an
email, a phone number or a payment detail.

This is a proof of concept running on a deliberately tiny hardcoded menu, and
you never pretend otherwise. The menu is exactly these items and nothing else
exists:
{_MENU_LINES}

If search_menu_knowledge returns nothing, say the menu is only these items.

The tools registered right now are: {", ".join(name.value for name in TOOLS)}.
Any lane above whose tool is not on that list is not available on this turn --
say so plainly rather than improvising an answer or reaching for another tool.
There is no retrieval corpus behind this menu yet, so answer menu questions from
what search_menu_knowledge returns and leave the citations field empty.

{SIMULATION_NOTICE} Say so whenever an order is placed.

Keep replies to a few sentences. Plain text, no markdown."""
"""What is true today, as opposed to how the assistant behaves.

Deliberately a separate message rather than an f-string spliced into the prompt.
Splicing would make the prompt digest a function of the menu and the persona,
and a version that changes when a visitor changes is not a version."""


@dataclass(slots=True)
class Conversation:
    """One visitor's message history, and how many turns they have taken.

    In memory, and therefore lost on a restart. Honest for a single-replica demo
    and deliberately not papered over: durable visitor state is issue #9, and
    the thread that replaces this list is Microsoft-managed.
    """

    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    turn_index: int = 0

    def __post_init__(self) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": SYSTEM_PROMPT})
            self.messages.append({"role": "system", "content": RUNTIME_CONTEXT})

    def next_turn_index(self) -> int:
        """Return this turn's index and move the counter on."""
        index = self.turn_index
        self.turn_index += 1
        return index


@dataclass(frozen=True, slots=True)
class TurnResult:
    """What one turn produced: the reply, what it cost, and any card to render."""

    reply: str
    prompt_tokens: int
    completion_tokens: int
    steps: int
    card: Mapping[str, Any] | None = None
    """A draft awaiting confirmation, or a receipt. The widget renders it; the
    model's prose describes it. Both come from the same tool result, so they
    cannot disagree about the total."""

    receipt: bool = False
    """True when :attr:`card` is a receipt rather than a draft to confirm."""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_FALLBACK_REPLY = "I got a bit tangled there -- could you ask me that again, more simply?"
"""What the visitor sees when the loop hits its step ceiling.

RFC-001 section 10: a lane may fail, the conversation may not fail with it. The
turn is still a complete trace and the step ceiling is still visible on it."""


def run_turn(
    conversation: Conversation,
    message: str,
    *,
    model: ChatModel,
    desk: OrderDesk,
    lane: PhotoLane | None = None,
    confirmed_draft_id: str | None = None,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> TurnResult:
    """Run one visitor message to an answer.

    Must be called inside a ``chat.turn``; the span helpers refuse otherwise.

    Args:
        conversation: The visitor's history, appended to in place.
        message: What the visitor said.
        model: The chat model to call.
        desk: The order desk holding this session's drafts.
        lane: The photo lane, where one is wired. ``None`` means the model is
            not offered ``match_meal_from_photo`` at all -- see
            :func:`~chip_chat.agent.tools.offered_tools` for why an unanswerable
            tool definition is worse than an absent one.
        confirmed_draft_id: A draft the desk has *already* confirmed. The model
            is told, because it cannot see the button. See
            :data:`CONFIRMATION_NOTE` for why telling it is not the same as
            trusting it.
        max_steps: Round trips before the loop gives up. See
            :data:`DEFAULT_MAX_STEPS` for why it is a spend control.

    Returns:
        The reply, the tokens it cost across every round trip, and any card.
    """
    if confirmed_draft_id:
        conversation.messages.append(
            {
                "role": "system",
                "content": CONFIRMATION_NOTE.format(draft_id=confirmed_draft_id),
            }
        )
    conversation.messages.append({"role": "user", "content": message})
    prompt_tokens = 0
    completion_tokens = 0
    card: Mapping[str, Any] | None = None
    receipt = False

    schemas = offered_schemas(lane=lane)

    for step_index in range(max_steps):
        with agent_step(index=step_index) as step:
            reply = _complete(
                model, conversation.messages, schemas, is_first=step_index == 0
            )
            prompt_tokens += reply.prompt_tokens
            completion_tokens += reply.completion_tokens
            # What this round trip cost, on the span that contains it. The turn
            # total lives on `chat.turn`; this is the per-step breakdown a
            # runaway loop shows up in.
            step.record_token_rollup(
                TokenUsage(
                    prompt_tokens=reply.prompt_tokens,
                    completion_tokens=reply.completion_tokens,
                )
            )
            conversation.messages.append(_assistant_message(reply))

            if not reply.tool_calls:
                step.record_output(reply.content or "")
                return TurnResult(
                    reply=(reply.content or "").strip() or _FALLBACK_REPLY,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    steps=step_index + 1,
                    card=card,
                    receipt=receipt,
                )

            for invocation in reply.tool_calls:
                result = dispatch(
                    invocation,
                    session_id=conversation.session_id,
                    desk=desk,
                    lane=lane,
                )
                conversation.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": invocation.call_id,
                        "content": _as_json(result),
                    }
                )
                found = _card_from(invocation.name, result)
                if found is not None:
                    card, receipt = found
            step.record_output(
                "called " + ", ".join(call.name for call in reply.tool_calls)
            )

    return TurnResult(
        reply=_FALLBACK_REPLY,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        steps=max_steps,
        card=card,
        receipt=receipt,
    )


def _complete(
    model: ChatModel,
    messages: Sequence[Mapping[str, Any]],
    schemas: Sequence[Mapping[str, Any]],
    *,
    is_first: bool,
) -> ModelReply:
    """One ``llm.completion``, with everything the span schema wants on it."""
    with llm_completion(
        model=model.deployment, provider=_PROVIDER, system=_SYSTEM
    ) as recorder:
        if is_first:
            # Once per turn, not once per step: the tool definitions are the
            # same on every round trip, and repeating them would triple the
            # size of a trace to say the same thing three times.
            recorder.record_tools(schemas)
        recorder.record_input_messages(_as_messages(messages))
        reply = model.complete(messages, tools=schemas)
        recorder.record_usage(
            prompt_tokens=reply.prompt_tokens,
            completion_tokens=reply.completion_tokens,
        )
        recorder.record_finish_reason(reply.finish_reason)
        if reply.content:
            recorder.record_output_messages(
                [Message(role="assistant", content=reply.content)]
            )
        if reply.model and reply.model != model.deployment:
            # The deployment asked for and the model that answered are different
            # facts, and Phase 9 needs the second one.
            recorder.set_metadata(served_by=reply.model)
        return reply


def _as_messages(messages: Sequence[Mapping[str, Any]]) -> Sequence[Message]:
    """Flatten the message list for ``llm.input_messages``.

    Tool results are messages too and belong on the span: "which tool result did
    the model have in front of it" is the first question asked of a turn that
    answered something odd.
    """
    return [
        Message(role=str(message.get("role", "")), content=_content_of(message))
        for message in messages
    ]


def _content_of(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    calls = message.get("tool_calls")
    if calls:
        return _as_json(calls)
    return ""


def _assistant_message(reply: ModelReply) -> dict[str, Any]:
    """The assistant turn, in the shape the next request has to be given back."""
    message: dict[str, Any] = {"role": "assistant", "content": reply.content}
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": _as_json(call.arguments)},
            }
            for call in reply.tool_calls
        ]
    return message


def _card_from(
    name: str, result: Mapping[str, Any]
) -> tuple[Mapping[str, Any], bool] | None:
    """Pull the card out of a tool result, if that tool produces one.

    Returns:
        ``(card, is_receipt)``, or ``None`` when the tool produced no card.
    """
    if name == ToolName.PROPOSE_ORDER.value:
        draft = result.get("draft")
        return (draft, False) if isinstance(draft, dict) else None
    if name == ToolName.PLACE_ORDER.value:
        found = result.get("receipt")
        return (found, True) if isinstance(found, dict) else None
    return None


def _as_json(value: object) -> str:
    return json.dumps(value, default=str, sort_keys=True)
