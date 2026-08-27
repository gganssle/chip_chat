"""Four ways a trajectory is wrong, and why they are counted apart.

Issue #74 asks for the failure shapes to be *scored separately, because they
mean different things*, and that is the whole argument for this module. A single
``tool_selection: 0.88`` sends everybody to the same place -- the tool
descriptions -- when four different things are wrong and only one of them is a
description problem.

**Wrong lane** is a description problem, or a prompt one. The model chose, and
chose the other thing.

**No tool** is not a routing failure at all in the way the others are: the model
answered from what it already knew. That is the quiet killer for groundedness,
because the answer *reads* fine -- there is prose, it is fluent, and nothing in
it is attached to anything. It is also the shape a deployment produces when the
tool was never registered, which is a wiring fact rather than a model one, and
``eval/trajectory/BASELINE.md`` is where that distinction gets made in prose
because a span tree cannot see a tool that was not offered.

**Extra tools** is a cost problem before it is a correctness one. The turn got
there; it paid for three calls to do it, and PRD section 05 asks for cost per
conversation. :data:`~chip_chat.eval.trajectory.expectations.SANCTIONED` is what
keeps this from firing on the turns that legitimately chain two calls.

**Right lane, wrong query** is a lane the model got right and an ask it did not
carry -- and it is observable on the two tools whose argument is the ask itself.
See :data:`~chip_chat.eval.trajectory.expectations.QUERY_ARGUMENT`.

**The detector for the fourth is deliberately weak, and says so.** It fires when
the query shares *no* content word with what the visitor said or with the menu
terms the row leans on. That catches a query that drifted off the question
entirely, and a call that passed no query at all. It does not catch the subtle
paraphrase that quietly changes the ask -- *"steak allergens"* for *"is the
steak safe for a severe soy allergy"* shares ``steak`` and passes here. That one
is a judgement about meaning, it belongs behind
:class:`~chip_chat.eval.golden.run.Judge` with ``grounded`` and ``declines``,
and #76's online evals are where a model lands behind it. A keyword rule that
claimed to settle it would produce a number measuring the keyword rule.

**Precedence, and why it is this order.** A turn can be wrong in several ways at
once, and one row gets one shape. Unreadable beats everything, because a tree
that cannot be believed is evidence of nothing. Then no tool, then wrong lane,
then extra tools, then wrong query: each shape is only asked once the shape
above it has been ruled out, so the counts partition the set and can be read as
*where the turns went* rather than as overlapping tallies.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.trajectory.expectations import Expectation
from chip_chat.eval.trajectory.trees import ToolCall, Trajectory
from chip_chat.otel.schema import ToolName

__all__ = [
    "FAILURE_SHAPES",
    "STOPWORDS",
    "Judgement",
    "Shape",
    "classify",
]


class Shape(StrEnum):
    """What became of one turn's trajectory.

    Attributes:
        CORRECT: The expected tool was reached, nothing forbidden was, nothing
            extra was, and the query -- where there is one -- carried the ask.
        WRONG_LANE: A tool was called, and it was the wrong one: a menu
            question answered from the account lane, or a turn that should have
            called nothing calling something. A forbidden tool lands here
            however else the turn went, because a row that names a tool as the
            wrong answer has said precisely what wrong means for it.
        NO_TOOL: Nothing was called, where something should have been.
        EXTRA_TOOLS: The right lane, plus calls
            :data:`~chip_chat.eval.trajectory.expectations.SANCTIONED` does not
            sanction -- or the same tool more than once.
        WRONG_QUERY: The right tool, asked the wrong thing.
        UNSCORED: The span tree could not be believed. Not a failure shape: a
            split trace or a missing recording is evidence about propagation,
            and :attr:`~chip_chat.eval.trajectory.trees.Trajectory.
            unreadable_because` says which.
    """

    CORRECT = "correct"
    WRONG_LANE = "wrong_lane"
    NO_TOOL = "no_tool"
    EXTRA_TOOLS = "extra_tools"
    WRONG_QUERY = "wrong_query"
    UNSCORED = "unscored"


FAILURE_SHAPES: Final[tuple[Shape, ...]] = (
    Shape.WRONG_LANE,
    Shape.NO_TOOL,
    Shape.EXTRA_TOOLS,
    Shape.WRONG_QUERY,
)
"""#74's four, in the order the ticket lists them. :attr:`Shape.UNSCORED` is not
one of them and :attr:`Shape.CORRECT` is not one of them, which is what makes
``len(FAILURE_SHAPES)`` a thing ``coverage`` can hold the report to."""


STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "about",
        "actually",
        "after",
        "all",
        "am",
        "an",
        "and",
        "any",
        "anything",
        "are",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "get",
        "give",
        "got",
        "had",
        "has",
        "have",
        "here",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "know",
        "like",
        "many",
        "me",
        "much",
        "my",
        "of",
        "on",
        "one",
        "or",
        "our",
        "out",
        "please",
        "so",
        "some",
        "tell",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "whats",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)
"""Words that carry no ask, and therefore cannot be evidence that one survived.

Hand-written and short. A stemmer or a stopword corpus would be a dependency
bought to make a deliberately weak detector marginally less weak, and the
module docstring is clear about where the strong version lives instead.
"""


@dataclass(frozen=True, slots=True)
class Judgement:
    """One row, one trajectory, one shape.

    Attributes:
        expectation: What was expected of the turn.
        trajectory: What the spans say happened.
        shape: Which of :class:`Shape`.
        detail: One line naming what went wrong, for the report. Empty on a
            correct turn.
        extras: The calls counted as extra, where the shape is
            :attr:`Shape.EXTRA_TOOLS`.
    """

    expectation: Expectation
    trajectory: Trajectory
    shape: Shape
    detail: str = ""
    extras: tuple[ToolName, ...] = ()

    @property
    def lane(self) -> Lane:
        """The lane this row belongs to, whatever the model did."""
        return self.expectation.lane

    @property
    def scored(self) -> bool:
        """Whether the trajectory could be scored at all."""
        return self.shape is not Shape.UNSCORED

    @property
    def selected(self) -> bool:
        """Whether the turn picked the right lane.

        The headline. True for :attr:`Shape.CORRECT`, and also for
        :attr:`Shape.EXTRA_TOOLS` and :attr:`Shape.WRONG_QUERY` -- both of
        those reached the expected tool and avoided every forbidden one, which
        is the same rule :func:`chip_chat.eval.golden.scoring.score` applies, so
        the two reports agree about the metric PRD section 05 sets a target on.

        The stricter reading is :attr:`clean`, and the report prints both.
        """
        return self.shape in (Shape.CORRECT, Shape.EXTRA_TOOLS, Shape.WRONG_QUERY)

    @property
    def clean(self) -> bool:
        """Whether the whole trajectory was right, not only the lane."""
        return self.shape is Shape.CORRECT


def classify(expectation: Expectation, trajectory: Trajectory) -> Judgement:
    """Give one turn its shape.

    Args:
        expectation: What the dataset row expects.
        trajectory: What the turn's spans say happened.

    Returns:
        The judgement. See the module docstring for the precedence.
    """
    unreadable = trajectory.unreadable_because
    if unreadable is not None:
        return Judgement(expectation, trajectory, Shape.UNSCORED, detail=unreadable)

    if expectation.tool is None:
        return _no_lane_expected(expectation, trajectory)

    if not trajectory.calls:
        return Judgement(
            expectation,
            trajectory,
            Shape.NO_TOOL,
            detail=(
                f"answered without calling {expectation.tool.value}; "
                f"{trajectory.steps} model round trip(s), no tool span"
            ),
        )

    forbidden = [call for call in trajectory.calls if call.tool in expectation.forbidden]
    if forbidden:
        return Judgement(
            expectation,
            trajectory,
            Shape.WRONG_LANE,
            detail=(
                f"called {forbidden[0].tool.value}, which this row names as the "
                f"wrong answer"
            ),
        )

    if expectation.tool not in trajectory.tools:
        called = trajectory.calls[0]
        return Judgement(
            expectation,
            trajectory,
            Shape.WRONG_LANE,
            detail=(
                f"reached {called.tool.value} in the {called.lane.value} lane; "
                f"expected {expectation.tool.value} in {expectation.lane.value}"
            ),
        )

    extras = _extras(expectation, trajectory.calls)
    if extras:
        return Judgement(
            expectation,
            trajectory,
            Shape.EXTRA_TOOLS,
            detail=(
                f"{len(trajectory.calls)} calls where "
                f"{1 + len(expectation.sanctioned)} would do: extra "
                + ", ".join(tool.value for tool in extras)
            ),
            extras=extras,
        )

    drift = _query_drift(expectation, trajectory)
    if drift is not None:
        return Judgement(expectation, trajectory, Shape.WRONG_QUERY, detail=drift)

    return Judgement(expectation, trajectory, Shape.CORRECT)


def _no_lane_expected(expectation: Expectation, trajectory: Trajectory) -> Judgement:
    """Score a row whose right answer is to call nothing.

    ``NO_TOOL`` is defined against an expected tool, so it cannot apply here:
    on these rows calling nothing is the correct trajectory, and calling
    anything is a lane the turn had no business entering.
    """
    if not trajectory.calls:
        return Judgement(expectation, trajectory, Shape.CORRECT)
    called = trajectory.calls[0]
    return Judgement(
        expectation,
        trajectory,
        Shape.WRONG_LANE,
        detail=(f"reached {called.tool.value} on a turn that should have called nothing"),
    )


def _extras(expectation: Expectation, calls: Sequence[ToolCall]) -> tuple[ToolName, ...]:
    """The calls beyond the expected one and its sanctioned companions.

    One of each is allowed, so a second ``propose_order`` on a turn that already
    proposed is extra -- *reached for three when one would do* counts calls, not
    distinct tools.
    """
    budget: dict[ToolName, int] = {expectation.tool: 1} if expectation.tool else {}
    for companion in expectation.sanctioned:
        budget[companion] = 1
    extras: list[ToolName] = []
    for call in calls:
        if budget.get(call.tool, 0) > 0:
            budget[call.tool] -= 1
            continue
        extras.append(call.tool)
    return tuple(extras)


def _query_drift(expectation: Expectation, trajectory: Trajectory) -> str | None:
    """Why the query did not carry the ask, or ``None`` where it did or cannot be told.

    Returns ``None`` on every row whose expected tool takes no natural-language
    argument. That is *unscoreable*, not *clean*, and the caller does not need
    to know the difference because
    :attr:`~chip_chat.eval.trajectory.expectations.Expectation.scores_query` is
    what the report counts to say how many rows this check could see at all.
    """
    argument = expectation.query_argument
    if argument is None or expectation.tool is None:
        return None
    call = trajectory.calls_to(expectation.tool)[0]
    asked = call.arguments.get(argument)
    if not isinstance(asked, str) or not asked.strip():
        return f"called {expectation.tool.value} with no {argument}"
    if not _content(expectation.message) | _terms(expectation.menu_terms):
        # Nothing to compare against. A row whose message is entirely
        # stopwords is not a row this detector can say anything about.
        return None
    if _content(asked) & (_content(expectation.message) | _terms(expectation.menu_terms)):
        return None
    return (
        f"asked {expectation.tool.value} for {asked!r}, which shares nothing "
        f"with {expectation.message!r}"
    )


_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def _content(text: str) -> frozenset[str]:
    """The words in ``text`` that carry an ask."""
    words = _NOT_ALPHANUMERIC.sub(" ", text.lower()).split()
    return frozenset(word for word in words if word not in STOPWORDS)


def _terms(terms: Sequence[str]) -> frozenset[str]:
    """The content words of every menu term the row leans on."""
    found: set[str] = set()
    for term in terms:
        found |= _content(term)
    return frozenset(found)
