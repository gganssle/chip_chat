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
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from chip_chat.agent.desk import Desk
from chip_chat.agent.hardcoded import ACCOUNT, MENU, SIMULATION_NOTICE, STORE
from chip_chat.agent.lanes import NO_LANES, Lanes
from chip_chat.agent.model import ChatModel, ModelReply
from chip_chat.agent.prompt import load
from chip_chat.agent.tools import (
    DESK_WRITES,
    TOOLS,
    dispatch,
    offered_schemas,
    offered_tools,
)
from chip_chat.otel import (
    Message,
    TokenUsage,
    ToolName,
    agent_step,
    llm_completion,
)

__all__ = [
    "CONFIRMATION_NOTE",
    "DEFAULT_MAX_STEPS",
    "PROMPT_VERSION",
    "RUNTIME_CONTEXT",
    "SYSTEM_PROMPT",
    "Conversation",
    "ToolRegistrationError",
    "TurnResult",
    "run_turn",
    "runtime_context",
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


def runtime_context(tools: Sequence[ToolName] = TOOLS, *, lanes: Lanes = NO_LANES) -> str:
    """What is true today, as opposed to how the assistant behaves.

    Deliberately a separate message rather than an f-string spliced into the
    prompt. Splicing would make the prompt digest a function of the menu and the
    persona, and a version that changes when a visitor changes is not a version.

    A function rather than a constant because neither the registered tool list
    nor the data behind it is the same in every deployment. Telling the model a
    tool is registered when nothing can answer it, or withholding one that is,
    are both worse than either being consistently true -- see
    :func:`~chip_chat.agent.tools.offered_tools`.

    ``lanes`` is here for the same reason and it is not cosmetic: the paragraph
    about the three-item menu is *true* on a deployment with no knowledge lane
    and *false* on one with the harvested corpus behind it. A model told the
    menu is three items while retrieval returns forty will either contradict the
    corpus or refuse to read it, and both look like a retrieval bug.

    **The paragraph about who the visitor is has the same property, and it cost a
    live demo to find out.** This function used to name
    :data:`chip_chat.agent.hardcoded.ACCOUNT` unconditionally, so every
    conversation opened by telling the model it was serving *"the Ballard
    regular"*. That was true of a deployment whose account tools read a fixture
    and false the moment ``cc-lpy4`` wired the account lane: the visitor's own
    rows say a different store and a different balance, and the model,
    reasonably, repeated what the system message told it and then quoted what the
    tool returned. ``docs/public-demo.md`` §9 has that transcript. So the
    sentence is now conditional on the lane, like the menu paragraph above it and
    for exactly the same reason.

    Args:
        tools: The tools actually registered for this deployment.
        lanes: What is wired, which decides which facts below are facts.

    Returns:
        The second system message a conversation opens with.
    """
    return f"""\
Facts about this turn. These change; the instructions above do not.

{_account_facts(lanes)}

{_menu_facts(lanes)}

The tools registered right now are: {", ".join(name.value for name in tools)}.
Any lane above whose tool is not on that list is not available on this turn --
say so plainly rather than improvising an answer or reaching for another tool.

{SIMULATION_NOTICE} Say so whenever an order is placed.

Keep replies to a few sentences. Plain text, no markdown."""


_HARDCODED_MENU_FACTS = f"""\
This is a proof of concept running on a deliberately tiny hardcoded menu, and
you never pretend otherwise. The menu is exactly these items and nothing else
exists:
{_MENU_LINES}

If search_menu_knowledge returns nothing, say the menu is only these items.
There is no retrieval corpus behind this menu yet, so answer menu questions from
what search_menu_knowledge returns and leave the citations field empty."""
"""What is true of a deployment with no knowledge lane wired."""

_RETRIEVED_MENU_FACTS = """\
Menu questions are answered from the restaurant's published pages through
search_menu_knowledge, and from nowhere else -- not from what you remember about
this restaurant and not from an earlier turn. Every passage it returns carries
an id; cite the ones you used and never a source it did not return. If it comes
back with nothing, or says its confidence is low, say you could not find it
rather than filling the gap."""
"""What is true of a deployment with the harvested corpus behind #49's lane."""


_HARDCODED_ACCOUNT_FACTS = f"""\
The visitor is signed in as {ACCOUNT.display_name}, a rewards member at the
{STORE.name} store. You already know who they are, so never ask for a name, an
email, a phone number or a payment detail."""
"""What is true of a deployment with no account lane: one fixture, everybody."""

_BOUND_ACCOUNT_FACTS = """\
The visitor is signed in and their account is already bound to this
conversation, so you already know who they are and must never ask for a name, an
email, a phone number or a payment detail -- and never accept one offered.

You are not told which customer they are and you do not need to be. Every
account and personalization tool answers for this visitor and none of them takes
an identifier, so anything you say about their points, their spending, their
history or their usual order comes from a tool result on this turn and from
nowhere else. Never state a store, a balance or an order from memory, from this
message or from an earlier turn. Where a tool has not given you a figure, leave
it out -- do not announce that it is missing.

"Say what the visitor is holding" is already done and it was not done by you.
The application shows the visitor a sentence describing their account before
your first reply is written, so do not open by naming their persona, their store
or their balance again. Answer what they asked."""
"""What is true once #43's policies and #44's pool are actually in the path.

The second paragraph is the load-bearing one and it is a *consequence* of the
architecture rather than an instruction bolted on top of it: the tools genuinely
have no identifier argument, so a model that invented one could not use it. What
it prevents is the softer failure -- a model that has been told a store in a
system message repeating it beside a balance a tool returned, which is one
sentence containing two visitors.

Its last sentence is there because an earlier draft did not have it. Listing
what the tools cover ("their home store, their points balance, ...") made the
model report the ones no tool had answered on that turn, and the live reply
opened *"You're signed into a persona with no home store set"* -- accurate,
obedient and a worse first impression than the sentence it replaced. A model
told to prefer tool results should be told to say less, not to narrate the gap.

**The third paragraph overrides an instruction in the versioned prompt, and does
it here rather than there on purpose.** ``prompts/system-v1.md`` says *"Your
first message names the persona they were given -- home store, points balance,
and a characteristic order"*, which was written for a tier where the model was
the only thing that could say it. It is not any more:
:func:`chip_chat.web.persona.opening_message` writes that sentence from the
assigned fixture and the visitor has read it before a model is called. Obeying
the prompt therefore produces the same sentence twice, and on a wired deployment
the second copy is assembled from tool results while the first was assembled
from the roster -- which is how *"AL Town 1 Mall, 397 points on the card"* and
*"1,363 points"* end up in one screen.

Editing ``system-v1.md`` would be the tidier fix and it is the wrong file to
reach for from here: the prompt is versioned, ``PROMPT_VERSION`` is recorded on
every ``chat.turn``, and ``eval/`` holds baselines against it. This is a *fact
about this deployment* -- the app writes the opening -- which is precisely what
the runtime context is for, and it costs no version.
"""


def _menu_facts(lanes: Lanes) -> str:
    """Return the menu paragraph that is true for this wiring."""
    if lanes.knowledge is None:
        return _HARDCODED_MENU_FACTS
    return _RETRIEVED_MENU_FACTS


def _account_facts(lanes: Lanes) -> str:
    """Return the identity paragraph that is true for this wiring.

    Keyed on the *account* lane rather than on both Snowflake lanes, because the
    account lane is the one that makes the visitor's own store and balance
    readable. A deployment with personalization and no account is not a shape
    :func:`chip_chat.api.app.build_lanes` produces -- they are wired from one
    pool -- so the second condition would be a branch nothing takes.
    """
    if lanes.account is None:
        return _HARDCODED_ACCOUNT_FACTS
    return _BOUND_ACCOUNT_FACTS


RUNTIME_CONTEXT = runtime_context()
"""The runtime context of a deployment with nothing wired: the hardcoded menu,
the hardcoded account, and none of the three conditional tools offered."""


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
    tools: tuple[ToolName, ...] = TOOLS
    """The tools registered for this deployment, as the runtime context names
    them. Set from :func:`~chip_chat.agent.tools.offered_tools`, which is what
    :func:`run_turn` checks it against -- a conversation told one list while the
    model is offered another is a wiring fault, and a silent one."""

    lanes: Lanes = NO_LANES
    """What is wired, for the two paragraphs of :func:`runtime_context` whose
    truth depends on it.

    A second field rather than something derived from :attr:`tools`, because the
    tool list cannot answer the question. ``get_points_balance`` is offered on
    every deployment -- it falls back to the fixture -- so a conversation that
    inferred *"the account lane is wired"* from seeing that name would infer it
    on the week-one slice too, and the sentence it chose would be wrong on
    exactly the deployment the fallback exists for."""

    def __post_init__(self) -> None:
        if not self.messages:
            self.messages.append({"role": "system", "content": SYSTEM_PROMPT})
            self.messages.append(
                {
                    "role": "system",
                    "content": runtime_context(self.tools, lanes=self.lanes),
                }
            )

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


class ToolRegistrationError(RuntimeError):
    """A turn was run with a tool set the conversation was not opened for.

    Always a programming error: it means the runtime context the model is
    reading and the tool definitions it is being offered disagree about what
    exists, which is a discrepancy no prompt can recover from.
    """


_FALLBACK_REPLY = "I got a bit tangled there -- could you ask me that again, more simply?"
"""What the visitor sees when the loop hits its step ceiling.

RFC-001 section 10: a lane may fail, the conversation may not fail with it. The
turn is still a complete trace and the step ceiling is still visible on it."""


def run_turn(
    conversation: Conversation,
    message: str,
    *,
    model: ChatModel,
    desk: Desk,
    lanes: Lanes = NO_LANES,
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
        lanes: The backing services this deployment has. A lane that is absent
            withdraws its tool rather than leaving one nothing can answer -- see
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

    Raises:
        ToolRegistrationError: If ``conversation`` was opened believing a
            different set of tools was registered than ``lanes`` implies.
    """
    offered = offered_tools(lanes, desk)
    if tuple(conversation.tools) != offered:
        # The runtime context is written once, when the conversation opens, and
        # names the registered tools. A lane wired after that -- or a
        # conversation built without one and then run with one -- would leave
        # the model told one thing and offered another, and the symptom would be
        # a model politely declining a lane it can in fact reach.
        raise ToolRegistrationError(
            f"the conversation was opened with {[t.value for t in conversation.tools]} "
            f"registered, but this turn offers {[t.value for t in offered]}"
        )
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

    schemas = offered_schemas(lanes, desk)

    for step_index in range(max_steps):
        with agent_step(index=step_index) as step:
            reply = _complete(
                model, conversation.messages, schemas, is_first=step_index == 0
            )
            # This step's own model call, plus whatever its tools spend on model
            # calls of their own. A tool that calls a model -- the photo lane
            # does, stage 4 is a vision completion -- bills tokens as real as
            # the agent's, and a step total that stopped at the agent's would
            # make every downstream figure wrong by exactly the lane.
            spent = _spent(reply)
            conversation.messages.append(_assistant_message(reply))

            if not reply.tool_calls:
                step.record_output(reply.content or "")
                step.record_token_rollup(spent)
                prompt_tokens += spent.prompt_tokens
                completion_tokens += spent.completion_tokens
                return TurnResult(
                    reply=(reply.content or "").strip() or _FALLBACK_REPLY,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    steps=step_index + 1,
                    card=card,
                    receipt=receipt,
                )

            for invocation in reply.tool_calls:
                lane_spend: list[TokenUsage] = []
                result = dispatch(
                    invocation,
                    session_id=conversation.session_id,
                    desk=desk,
                    lanes=lanes,
                    record_spend=lane_spend.append,
                )
                for usage in lane_spend:
                    spent = spent + usage
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
            # After the tools, so the step's rollup covers its whole subtree.
            step.record_token_rollup(spent)
            prompt_tokens += spent.prompt_tokens
            completion_tokens += spent.completion_tokens

    return TurnResult(
        reply=_FALLBACK_REPLY,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        steps=max_steps,
        card=card,
        receipt=receipt,
    )


def _spent(reply: ModelReply) -> TokenUsage:
    """What one round trip's model call cost, as the provider reported it."""
    return TokenUsage(
        prompt_tokens=reply.prompt_tokens, completion_tokens=reply.completion_tokens
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
    if name not in _CARD_TOOLS:
        return None
    # The other four are symmetrical and answered by the same two keys, because
    # `cancel_order`, `redeem_points` and `update_preferences` are each one tool
    # answered twice -- a card the first time and a receipt after the visitor
    # confirmed. `place_order` only ever produces the second, which is why it is
    # in this list rather than beside `propose_order`: it costs nothing to read
    # a key that is never there, and a fifth branch would be a fifth place to
    # forget a tool.
    receipt = result.get("receipt")
    if isinstance(receipt, dict):
        return receipt, True
    card = result.get("card")
    return (card, False) if isinstance(card, dict) else None


_CARD_TOOLS: Final[frozenset[str]] = frozenset(
    tool.value for tool in (ToolName.PLACE_ORDER, *DESK_WRITES)
)
"""The write tools whose results carry a card or a receipt for the widget.

Derived from :data:`chip_chat.agent.tools.DESK_WRITES` rather than written out,
so a deployment that offers a write tool cannot also be a deployment whose
widget silently never renders its card.
"""


def _as_json(value: object) -> str:
    return json.dumps(_delimited(value), default=str, sort_keys=True)


_PASSAGE_FIELD: Final = "passages"
_PASSAGE_TEXT: Final = "text"


def _delimited(value: object) -> object:
    """Return ``value`` with every retrieved passage wrapped in an envelope.

    Issue #79 asks for the instructions-are-data rule to be enforced
    *structurally* -- "clear delimiting of retrieved content" -- rather than by
    asking the system prompt to hold the line. A tool result used to reach the
    model as bare ``json.dumps``, so a corpus document reading *IGNORE ALL
    PREVIOUS INSTRUCTIONS* arrived in the same undifferentiated soup as the
    instructions it was addressing.

    The envelope is per-call rather than fixed, and that is the whole point. A
    constant ``</document>`` is forged by a document containing ``</document>``,
    and an attacker who can influence the corpus -- which PRD S2 assumes -- can
    put anything in a document. A nonce minted here is not in the corpus when
    the corpus is written, so there is nothing for a planted passage to close.

    This narrows what an injection can do; it does not stop one. A passage that
    corrupts an *answer* still corrupts it, which is the residual risk #81
    measures and `docs/decisions/corpus-injection-residual.md` accepts
    deliberately. What it removes is the ambiguity about which bytes were
    retrieved.
    """
    if not isinstance(value, Mapping):
        return value
    passages = value.get(_PASSAGE_FIELD)
    if not isinstance(passages, list):
        return value
    nonce = secrets.token_hex(8)
    wrapped = [_wrap(passage, nonce) for passage in passages]
    return {**value, _PASSAGE_FIELD: wrapped}


def _wrap(passage: object, nonce: str) -> object:
    """Return one passage with its text between tags it cannot close."""
    if not isinstance(passage, Mapping):
        return passage
    text = passage.get(_PASSAGE_TEXT)
    if not isinstance(text, str):
        return passage
    # Stripped before wrapping rather than escaped after. The passage cannot
    # know this turn's nonce, so this removes nothing a real document contains
    # -- it is here so that the invariant is a property of the code rather than
    # an argument about probability.
    body = text.replace(nonce, "")
    identifier = passage.get("id", "")
    return {
        **passage,
        _PASSAGE_TEXT: (
            f'<retrieved id="{identifier}" nonce="{nonce}">{body}</retrieved:{nonce}>'
        ),
    }
