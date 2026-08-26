"""Fixtures for driving the scorer, and one measurement they are honest about.

:class:`RoutingOracle` is a chat model that reads the golden set and calls,
for each message, exactly the tool that case expects. It is a stub, and a stub's
answers measure the stub -- ``chip_chat.eval.photos.testing`` says the same thing
at greater length about coloured rectangles, and it is no less true here.

So read what a run against it is and is not.

**It is not a score for the agent.** Nothing about model quality survives a model
that was told the answer. Every routing case passes by construction, and a
tool-selection figure computed over this run is a figure about a lookup table.

**It is a measurement of the plumbing, at its ceiling.** Give a deployment
perfect lane selection and some of the golden set still fails -- because a tool
is not built, because a card carries no notice, because the reply has nowhere to
put a citation. Those failures are properties of the wiring, they are
reproducible for free in CI, and they are the ones worth fixing before spending
money on a real run. A ceiling below the PRD's target is a fact about the
deployment that no amount of prompt work will move.

:func:`ceiling` is that run, named for what it is.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from chip_chat.agent.hardcoded import MENU
from chip_chat.agent.model import ModelReply, ToolInvocation
from chip_chat.eval.golden.cases import Check, GoldenCase, GoldenSet
from chip_chat.eval.golden.run import Observation, run_set
from chip_chat.eval.golden.slice import SliceDeployment
from chip_chat.otel.schema import ToolName
from chip_chat.vision.store import PHOTO_REF_ARGUMENT

__all__ = ["ORACLE_DEPLOYMENT", "RoutingOracle", "ceiling"]

ORACLE_DEPLOYMENT: Final = "routing-oracle"
"""What the oracle calls itself, so a report from one is obviously from one."""

_PROMPT_TOKENS: Final = 0
_COMPLETION_TOKENS: Final = 0
"""Zero, deliberately. A fixture that reported plausible token counts would put
a cost figure in a report that nobody paid, and cost per conversation is one of
the numbers PRD section 05 asks for."""

_CONFIRMED_DRAFT = re.compile(r"pressed Confirm on draft (\S+?)\.")
"""The draft id out of :data:`~chip_chat.agent.loop.CONFIRMATION_NOTE`.

The oracle has to pass ``place_order`` a real draft id, and this is where the
request handler puts one. Reading it back is what a model would do -- the note
is written to the model precisely because it cannot see the button.
"""


class RoutingOracle:
    """A :class:`~chip_chat.agent.model.ChatModel` that always routes correctly.

    One tool call, then one answer. Cases expecting no tool get the answer
    straight away, which is what makes them pass rather than a special case in
    the scorer.
    """

    __slots__ = ("_cases",)

    def __init__(self, golden: GoldenSet) -> None:
        """Build an oracle that knows this set's answers.

        Args:
            golden: The set. Messages are the key, so two cases sharing a
                message must expect the same tool -- which they do, since a
                message that means two things is a case about the persona
                rather than about the lane.
        """
        self._cases: dict[str, GoldenCase] = {case.message: case for case in golden}

    @property
    def deployment(self) -> str:
        """:data:`ORACLE_DEPLOYMENT`."""
        return ORACLE_DEPLOYMENT

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
    ) -> ModelReply:
        """Call the expected tool, or answer once it has been called.

        Args:
            messages: The conversation so far.
            tools: Offered tool definitions. Consulted, so that the oracle does
                not call a tool this deployment never registered -- a fixture
                that reached past the registration would measure a tool nobody
                offered.

        Returns:
            One tool call, or an answer.
        """
        case = self._cases.get(_last_user_message(messages))
        offered = {definition.get("function", {}).get("name") for definition in tools}
        tool = _next_tool(case, messages, offered)
        if tool is None:
            return _answer("Here you go.")
        return ModelReply(
            content=None,
            tool_calls=(
                ToolInvocation(
                    call_id="oracle-1",
                    name=tool.value,
                    arguments=_arguments(tool, messages),
                ),
            ),
            finish_reason="tool_calls",
            prompt_tokens=_PROMPT_TOKENS,
            completion_tokens=_COMPLETION_TOKENS,
        )


def ceiling(golden: GoldenSet) -> tuple[Observation, ...]:
    """Run the set against the slice with routing handed to it.

    Args:
        golden: The set.

    Returns:
        One observation per case. What fails here fails for a reason no model
        could have fixed -- see the module docstring.
    """
    return run_set(golden, SliceDeployment(RoutingOracle(golden)))


def _next_tool(
    case: GoldenCase | None,
    messages: Sequence[Mapping[str, Any]],
    offered: set[str | None],
) -> ToolName | None:
    """The next call this turn owes, or ``None`` to answer.

    Two steps, not one, and the second is why this is not simply a lookup. A
    reorder turn reaches ``get_usual_order`` and *then* proposes: the case's
    expected tool is only where the turn starts, and stopping there would leave
    the card unrendered and score a wiring failure that is really the fixture's.
    """
    if case is None:
        return None
    already = _called(messages)
    if not already:
        return case.tool if case.tool and case.tool.value in offered else None
    if (
        Check.CONFIRMS_FIRST in case.checks
        and ToolName.PROPOSE_ORDER.value not in already
        and ToolName.PROPOSE_ORDER.value in offered
        and not case.confirmed
    ):
        return ToolName.PROPOSE_ORDER
    return None


def _called(messages: Sequence[Mapping[str, Any]]) -> frozenset[str]:
    """Every tool already called in this conversation."""
    return frozenset(
        str(call.get("function", {}).get("name"))
        for message in messages
        for call in message.get("tool_calls") or ()
    )


def _answer(text: str) -> ModelReply:
    return ModelReply(
        content=text,
        finish_reason="stop",
        prompt_tokens=_PROMPT_TOKENS,
        completion_tokens=_COMPLETION_TOKENS,
    )


def _last_user_message(messages: Sequence[Mapping[str, Any]]) -> str:
    """The most recent thing the visitor said."""
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content", ""))
    return ""


def _arguments(
    tool: ToolName, messages: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any]:
    """Plausible arguments for one tool call.

    Plausible, not clever. What is being measured on the far side of this is
    whether the deployment can satisfy the case *given* the right tool, so the
    arguments only have to be well-formed enough to reach the tool body.
    """
    match tool:
        case ToolName.SEARCH_MENU_KNOWLEDGE:
            return {"query": _last_user_message(messages)}
        case ToolName.PROPOSE_ORDER:
            return {"items": [{"item_id": next(iter(MENU)), "quantity": 1}]}
        case ToolName.PLACE_ORDER:
            return {"draft_id": _confirmed_draft_id(messages)}
        case ToolName.CANCEL_ORDER:
            return {"order_id": "CC-4A1B"}
        case ToolName.REDEEM_POINTS:
            return {"reward_id": "guac"}
        case ToolName.UPDATE_PREFERENCES:
            return {"prefs": {"no_cheese": True}}
        case ToolName.MATCH_MEAL_FROM_PHOTO:
            return {PHOTO_REF_ARGUMENT: "uploads/oracle.jpg"}
        case _:
            return {}


def _confirmed_draft_id(messages: Sequence[Mapping[str, Any]]) -> str:
    """The draft the visitor confirmed, read off the note the handler wrote.

    Empty where there is none, which produces a refusal from the order desk --
    correctly, because a ``place_order`` on a draft nobody confirmed is the
    launch gate doing its job.
    """
    for message in messages:
        found = _CONFIRMED_DRAFT.search(str(message.get("content", "")))
        if found:
            return found.group(1)
    return ""
