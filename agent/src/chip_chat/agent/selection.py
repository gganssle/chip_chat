"""Does the deployed agent pick the right lane? Ask it, and count.

Issue #60's fourth acceptance criterion is the one that cannot be asserted in a
unit test:

    *Tool descriptions are good enough that lane selection works without prompt
    gymnastics; tool-selection accuracy is the metric this whole architecture
    exists to get right.*

So this is a probe, in the same spirit as :mod:`chip_chat.agent.verify`: it
registers the eleven tools against the deployed chat model, sends each case
below, and reports which tool came back. It costs tokens and needs Azure
credentials, so it is a ``make`` target and not part of ``make ci`` -- a gate
that costs money and needs a logged-in human is not a gate.

    uv run python -m chip_chat.agent.selection
    uv run python -m chip_chat.agent.selection --no-prompt

``--no-prompt`` is the interesting one. It sends the same cases with the tools
and *no system prompt at all*. The gap between the two runs is the size of the
prompt's contribution to lane selection, and the criterion above wants that gap
small: descriptions that only work when the prompt is holding them up are
descriptions that will fail the first time an eval swaps the prompt.

**The cases are chosen adversarially, not representatively.** Six of the twelve
sit on a boundary two tools share -- published-versus-mine, balance-versus-
query, usual-versus-new, propose-versus-place -- because a case only one tool
could possibly answer measures nothing. That makes the printed percentage
harsher than PRD's 95% target, which is measured over the golden set in Phase 9.
Read it as a smoke test with a number attached, not as the metric itself.

**What the first runs found**, because it changes how to read a bad score. On
26 August 2026: ``gpt-5-mini`` 8/12 with the prompt, ``gpt-4.1-mini`` 10/12 with
it, and ``gpt-4.1-mini`` **11/12 with no prompt at all**. So the descriptions
carry lane selection on their own -- the best run is the one with no prompt in
it -- and the variable that actually moved the number was the deployment. That
is what ``--deployment`` is for, and why the failure message names it first.
Bead ``cc-6n5`` holds the model question; ``agent/README.md`` holds the table.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from openai.types.chat import ChatCompletion, ChatCompletionMessageFunctionToolCall

from chip_chat.agent.definition import AgentDefinition
from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError, chat_client
from chip_chat.otel.schema import ToolName

__all__ = ["CASES", "main"]

_TARGET = 0.95
"""PRD's tool-selection target. See the module docstring on what it means here."""


@dataclass(frozen=True, slots=True)
class Case:
    """One visitor message and the tool it should have reached for."""

    message: str
    expected: ToolName
    why: str
    """What this case is testing, printed beside a failure so the reader does not
    have to guess which boundary moved."""


CASES: tuple[Case, ...] = (
    Case(
        "is the barbacoa spicy?",
        ToolName.SEARCH_MENU_KNOWLEDGE,
        "plain knowledge",
    ),
    Case(
        "can I cancel an order after I've placed it?",
        ToolName.SEARCH_MENU_KNOWLEDGE,
        "policy question that names an action -- must not become cancel_order",
    ),
    Case(
        "how do points work?",
        ToolName.SEARCH_MENU_KNOWLEDGE,
        "published programme rules -- must not become get_points_balance",
    ),
    Case(
        "how many points do I have?",
        ToolName.GET_POINTS_BALANCE,
        "the fixed question -- must not become ask_account_question",
    ),
    Case(
        "what did I spend at the Ballard store this year?",
        ToolName.ASK_ACCOUNT_QUESTION,
        "aggregate and time range -- must not become get_points_balance",
    ),
    Case(
        "what's my usual?",
        ToolName.GET_USUAL_ORDER,
        "the precomputed habit -- must not become ask_account_question",
    ),
    Case(
        "what should I try that I haven't had before?",
        ToolName.GET_RECOMMENDATIONS,
        "the other mart -- must not become get_usual_order",
    ),
    Case(
        "I've uploaded a photo of my friend's bowl, make me that",
        ToolName.MATCH_MEAL_FROM_PHOTO,
        "the vision lane",
    ),
    Case(
        "yes, that one -- build it",
        ToolName.PROPOSE_ORDER,
        "a new order starts as a draft -- must not become place_order",
    ),
    Case(
        "yes, place it",
        ToolName.PLACE_ORDER,
        "explicit confirmation of a draft already on screen",
    ),
    Case(
        "redeem my free guac",
        ToolName.REDEEM_POINTS,
        "the redemption write -- must not become propose_order",
    ),
    Case(
        "remember that I never want cheese",
        ToolName.UPDATE_PREFERENCES,
        "a standing preference, not an order",
    ),
)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the model did with one case."""

    case: Case
    called: tuple[str, ...]
    """Every tool it called. Empty when it answered in prose instead.

    A tuple rather than a single name because the first version of this probe
    scored ``calls[0]`` and produced a number that was mostly measuring its own
    reading: with parallel tool calls left on, the model would reach for a
    no-argument tool alongside the right one, and whichever came back first won.
    :func:`run` now turns parallel calls off -- *which lane* is a question with
    one answer -- but the field stays plural so that a model calling two anyway
    is visible rather than silently truncated.
    """

    @property
    def chosen(self) -> str | None:
        """The tool it settled on, if it settled on exactly one."""
        return self.called[0] if len(self.called) == 1 else None

    @property
    def correct(self) -> bool:
        """Whether it reached for the tool the case expected, and only that one."""
        return self.called == (self.case.expected.value,)


def _prior_turn_context(case: Case) -> list[dict[str, str]]:
    """Give the two cases that presuppose screen state the state they need.

    *"Yes, place it"* is not answerable without a draft on screen, and *"redeem
    my free guac"* is not answerable without having been told a reward exists.
    Inventing that context in the case list would be measuring the fixture; a
    prior assistant turn is what the real conversation would have.

    ``propose_order`` needs the same treatment for a reason worth recording,
    because the first version of this probe got it wrong. *"Get me a chicken
    burrito with white rice and black beans"* is **not** a ``propose_order``
    turn: that tool takes catalogue item ids, the model has none, and it
    correctly reaches for ``search_menu_knowledge`` first. The turn measured
    here is the second half, once the ids are on screen -- which is the
    propose-versus-place boundary the case is actually about.
    """
    if case.expected is ToolName.PROPOSE_ORDER:
        return [
            {
                "role": "assistant",
                "content": (
                    "The Chicken Burrito (item_id CMG-BUR-CHK) comes with your "
                    "choice of rice and beans -- white rice is MOD-RICE-WHT and "
                    "black beans are MOD-BEAN-BLK."
                ),
            }
        ]
    if case.expected is ToolName.PLACE_ORDER:
        return [
            {
                "role": "assistant",
                "content": (
                    "Here's a chicken burrito with white rice and black beans, "
                    "$10.95, as draft draft_7f31. Place it?"
                ),
            }
        ]
    if case.expected is ToolName.REDEEM_POINTS:
        return [
            {
                "role": "assistant",
                "content": (
                    "You have 1,250 points. Free guacamole (reward guac) is "
                    "available at 1,000."
                ),
            }
        ]
    return []


def run(*, with_prompt: bool = True, deployment: str | None = None) -> list[Outcome]:
    """Send every case to the deployed model and report what it chose.

    Args:
        with_prompt: Whether to send the system prompt. False measures the tool
            descriptions on their own.
        deployment: Run against this deployment instead of the configured chat
            one. This is the same swap a Phase 9 experiment makes, and it is a
            flag here because the first run of this probe found the deployment
            to be the variable that mattered -- see the module docstring.

    Returns:
        One :class:`Outcome` per case, in :data:`CASES` order.
    """
    config = FoundryConfig.from_env()
    definition = AgentDefinition.build(config)
    model = deployment or definition.model_deployment
    client = chat_client(config)
    tools = definition.tool_definitions()

    outcomes: list[Outcome] = []
    for case in CASES:
        messages: list[dict[str, str]] = []
        if with_prompt:
            messages.append({"role": "system", "content": definition.prompt.text})
        messages.extend(_prior_turn_context(case))
        messages.append({"role": "user", "content": case.message})

        # The SDK's message and tool parameter types are large TypedDict unions.
        # Casting is honest here: what is sent is the wire shape the endpoint
        # documents, and `as_tool_definition` is the thing that decides it.
        completion = client.chat.completions.create(
            model=model,
            messages=cast(Any, messages),
            tools=cast(Any, tools),
            tool_choice="auto",
            parallel_tool_calls=False,
        )
        outcomes.append(Outcome(case=case, called=_called_tools(completion)))
    return outcomes


def _called_tools(completion: ChatCompletion) -> tuple[str, ...]:
    """Return the names of every tool the model called, in order.

    All eleven are function tools, so a call of any other kind is not one of
    ours and is dropped -- which shows up as a miss, because a case scores only
    when the model called exactly the tool it should have.
    """
    calls = completion.choices[0].message.tool_calls or []
    return tuple(
        call.function.name
        for call in calls
        if isinstance(call, ChatCompletionMessageFunctionToolCall)
    )


def _report(outcomes: Sequence[Outcome], *, with_prompt: bool, model: str) -> float:
    """Print the run and return the accuracy."""
    correct = sum(1 for outcome in outcomes if outcome.correct)
    accuracy = correct / len(outcomes) if outcomes else 0.0
    heading = "with the system prompt" if with_prompt else "with no system prompt"
    print(f"Tool selection on {model}, {heading}: {correct}/{len(outcomes)}\n")
    for outcome in outcomes:
        mark = "ok  " if outcome.correct else "MISS"
        chose = ", ".join(outcome.called) or "(answered in prose, called nothing)"
        print(f"  {mark}  {outcome.case.message}")
        print(f"        expected {outcome.case.expected.value}, chose {chose}")
        if not outcome.correct:
            print(f"        this case tests: {outcome.case.why}")
    print(f"\n  accuracy {accuracy:.0%}, against PRD's {_TARGET:.0%} target")
    return accuracy


def main(argv: Sequence[str] | None = None) -> int:
    """Run the probe. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.agent.selection",
        description="Register the eleven tools and see which lane the model picks.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="send the tools without the system prompt, to measure them alone",
    )
    parser.add_argument(
        "--deployment",
        help="run against this deployment instead of the configured chat one",
    )
    args = parser.parse_args(argv)
    with_prompt = not args.no_prompt

    try:
        outcomes = run(with_prompt=with_prompt, deployment=args.deployment)
        model = args.deployment or FoundryConfig.from_env().chat_deployment
    except FoundryConfigError as error:
        print(f"configuration: {error}", file=sys.stderr)
        return 2

    accuracy = _report(outcomes, with_prompt=with_prompt, model=model)
    if accuracy < _TARGET:
        print(
            "\nBelow target. Two things to try, in this order. Compare "
            "deployments -- `--deployment gpt-4.1-mini` against the configured "
            "one -- because the model is a bigger variable here than it looks. "
            "Then fix the tool description, not the system prompt: a lane the "
            "prompt has to rescue is a lane that breaks the next time the prompt "
            "changes.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
