"""Is this the set the ticket asked for? Two questions, both answered here.

The scorer says how well a deployment did on the cases it was given. This says
whether those cases were the ones worth giving it -- and that failure is
invisible to any pass rate, because a set of twelve easy knowledge questions
scores beautifully and proves nothing about the five lanes.

**Requirement coverage** is #29's first acceptance criterion: *every PRD
requirement has at least one golden-set entry referencing it*. Three outcomes,
not two. A requirement is covered by a case, or covered by a
:class:`~chip_chat.eval.golden.requirements.Delegation` that names where it is
measured instead, or uncovered. Collapsing the middle one into either of the
others is how a coverage report starts lying: fold delegations into "covered" and
the vision lane looks scored here when it is scored in ``eval/photos``; fold them
into "uncovered" and a complete set can never go green, so nobody looks at it.

**Shape** is everything else the ticket asks for in prose. *"Weight coverage
toward tool selection"*, *"include questions the corpus genuinely cannot
answer"*, *"each of the six supported actions"*. Same treatment the labeled photo
set gives #56's scope: one :class:`Shape` per clause, checked against the set on
disk, printed above the scores rather than below them.

One clause is worth reading twice. :data:`SHAPES` requires **boundary cases** --
cases that name a tool the answer must *not* reach for. A case only one tool
could possibly answer measures nothing about lane selection, which is the metric
this architecture exists to get right; ``chip_chat.agent.selection`` makes the
same argument about its twelve probe cases and chose six of them on a shared
boundary. A golden set that scored 95% on twelve unambiguous questions would
report the target met and have measured a coin that only lands one way.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from chip_chat.eval.golden.cases import Check, GoldenCase, GoldenSet
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.requirements import (
    DELEGATIONS,
    REQUIREMENTS,
    Delegation,
    Requirement,
)
from chip_chat.otel.schema import WRITE_TOOLS, ToolName

__all__ = [
    "SHAPES",
    "Coverage",
    "Shape",
    "coverage",
]


@dataclass(frozen=True, slots=True)
class Shape:
    """One clause of what the set has to contain, and how to count it.

    Attributes:
        name: How the report names it.
        minimum: How many cases must satisfy it.
        source: Which document asks for it, so a reader deciding whether it
            still applies can go and read the argument.
        satisfied_by: The test one case either passes or does not.
    """

    name: str
    minimum: int
    source: str
    satisfied_by: Callable[[GoldenCase], bool]

    def met_by(self, cases: Sequence[GoldenCase]) -> tuple[str, ...]:
        """The ids of the cases satisfying this clause, in set order."""
        return tuple(case.case_id for case in cases if self.satisfied_by(case))


def _in(lane: Lane) -> Callable[[GoldenCase], bool]:
    """A test for a case expecting ``lane``."""
    return lambda case: case.lane is lane


def _expects(tool: ToolName) -> Callable[[GoldenCase], bool]:
    """A test for a case expecting ``tool``."""
    return lambda case: case.tool is tool


def _checks(check: Check) -> Callable[[GoldenCase], bool]:
    """A test for a case carrying ``check``."""
    return lambda case: check in case.checks


SHAPES: Final[tuple[Shape, ...]] = (
    Shape(
        name="boundary cases -- a tool the answer must not reach for",
        minimum=10,
        source="#29 scope, and chip_chat.agent.selection's argument",
        satisfied_by=lambda case: bool(case.forbidden_tools),
    ),
    Shape(
        name="knowledge cases",
        minimum=8,
        source="#29 scope",
        satisfied_by=_in(Lane.KNOWLEDGE),
    ),
    Shape(
        name="account cases",
        minimum=5,
        source="#29 scope",
        satisfied_by=_in(Lane.ACCOUNT),
    ),
    Shape(
        name="personalization cases",
        minimum=4,
        source="#29 scope",
        satisfied_by=_in(Lane.PERSONALIZATION),
    ),
    Shape(
        name="action cases",
        minimum=7,
        source="#29 scope -- six supported actions, plus the write attempt",
        satisfied_by=_in(Lane.ACTION),
    ),
    Shape(
        name="vision routing cases",
        minimum=1,
        source="#29 scope -- 'referenced from here'",
        # Deliberately one. The vision lane's accuracy is the labeled photo
        # set's job and is delegated as such; what the photo set cannot see is
        # whether a photo turn reaches the lane at all, because it runs the
        # lane directly. That gap is what this case is for, and adding more of
        # them would be building a second photo set out of sentences.
        satisfied_by=_in(Lane.VISION),
    ),
    Shape(
        name="questions the corpus cannot answer",
        minimum=4,
        source="#29 scope, and cc-usl -- 'so K3 is measurable rather than assumed'",
        satisfied_by=_checks(Check.DECLINES),
    ),
    Shape(
        name="write attempts that must not execute",
        minimum=2,
        source="PRD launch gate two",
        satisfied_by=_checks(Check.NO_WRITE),
    ),
    Shape(
        name="cases requiring a citation",
        minimum=8,
        source="PRD K2 -- target zero uncited claims",
        satisfied_by=_checks(Check.CITES),
    ),
    Shape(
        name="the confusable account pair",
        minimum=2,
        source="RFC-001 section 06 -- get_points_balance beside ask_account_question",
        satisfied_by=lambda case: bool(
            case.forbidden_tools
            & {ToolName.GET_POINTS_BALANCE, ToolName.ASK_ACCOUNT_QUESTION}
        ),
    ),
    Shape(
        name="a write tool named as the wrong answer",
        minimum=2,
        source="#29 scope -- a policy question that names an action",
        # *"Can I cancel an order?"* is a published-policy question wearing a
        # write tool's name, and it is the failure that costs the most: a
        # miss here is not a bad answer, it is an unrequested action.
        satisfied_by=lambda case: bool(case.forbidden_tools & WRITE_TOOLS),
    ),
)
"""Every clause of the shape. Order is the order the report prints them in."""


@dataclass(frozen=True, slots=True)
class Coverage:
    """Whether a set is the set #29 asked for.

    Attributes:
        cases: How many entries the set holds.
        covered: Requirements with at least one case, and the ids of those
            cases.
        delegated: Requirements measured elsewhere, with the delegation. A
            requirement appears here *and* in ``covered`` where it has both --
            V2 is delegated to the photo set for components and covered here
            for routing, and reporting only one of those would hide half the
            arrangement.
        uncovered: Requirements with neither. #29's first acceptance criterion
            is that this is empty.
        tools: Every one of the eleven tools, and the cases expecting it. The
            weighting cc-usl asks for, as a check: tool-selection accuracy is
            what the five-lane architecture exists to get right, and a tool no
            case ever expects contributes nothing to it however good the
            number looks. PRD T1's six actions fall out of this -- each of
            them is one of these tools, or a ``propose_order`` turn separated
            from its neighbours by its context rather than by its tool.
        met: Shape clauses the set satisfies, with the ids that satisfy them.
        unmet: Shape clauses it does not, likewise.
    """

    cases: int
    covered: tuple[tuple[Requirement, tuple[str, ...]], ...]
    delegated: tuple[tuple[Requirement, Delegation], ...]
    uncovered: tuple[Requirement, ...]
    tools: tuple[tuple[ToolName, tuple[str, ...]], ...]
    met: tuple[tuple[Shape, tuple[str, ...]], ...]
    unmet: tuple[tuple[Shape, tuple[str, ...]], ...]

    @property
    def every_requirement_covered(self) -> bool:
        """#29's first acceptance criterion, as a boolean."""
        return not self.uncovered

    @property
    def tools_without_a_case(self) -> tuple[ToolName, ...]:
        """The tools no case expects, in :class:`ToolName` order."""
        return tuple(tool for tool, ids in self.tools if not ids)

    @property
    def complete(self) -> bool:
        """Whether every requirement, every tool and every shape clause is met."""
        return (
            self.every_requirement_covered
            and not self.tools_without_a_case
            and not self.unmet
        )


def coverage(golden: GoldenSet) -> Coverage:
    """Check a set against the PRD register and against :data:`SHAPES`.

    Args:
        golden: The set, already loaded.

    Returns:
        The :class:`Coverage`. Never raises: an incomplete set is a fact to
        report beside the scores, not a reason to refuse to compute them. The
        photo set's argument, and the same caveat -- the number is only safe
        while nobody can read it without this beside it.
    """
    delegations = {item.requirement_id: item for item in DELEGATIONS}
    covered: list[tuple[Requirement, tuple[str, ...]]] = []
    delegated: list[tuple[Requirement, Delegation]] = []
    uncovered: list[Requirement] = []
    for item in REQUIREMENTS:
        ids = tuple(case.case_id for case in golden.covering(item.id))
        delegation = delegations.get(item.id)
        if ids:
            covered.append((item, ids))
        if delegation is not None:
            delegated.append((item, delegation))
        if not ids and delegation is None:
            uncovered.append(item)

    tools = tuple(
        (
            tool,
            tuple(case.case_id for case in golden.cases if case.tool is tool),
        )
        for tool in ToolName
    )

    met: list[tuple[Shape, tuple[str, ...]]] = []
    unmet: list[tuple[Shape, tuple[str, ...]]] = []
    for shape in SHAPES:
        ids = shape.met_by(golden.cases)
        (met if len(ids) >= shape.minimum else unmet).append((shape, ids))

    return Coverage(
        cases=len(golden),
        covered=tuple(covered),
        delegated=tuple(delegated),
        uncovered=tuple(uncovered),
        tools=tools,
        met=tuple(met),
        unmet=tuple(unmet),
    )
