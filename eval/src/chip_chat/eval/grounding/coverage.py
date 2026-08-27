"""Can this dataset support the numbers #75 asks for? Answered before the run.

The scorer says how a deployment did. This says whether the rows it was given
can carry the claims the report will make about them -- and that failure is
invisible to any rate, because *over-refusals: 0* over a set containing no
question the corpus can answer prints exactly like a system that never hedges.

That is the clause worth stating first, because it is the one #75 is most
worried about. The ticket asks for the refusal to be scored *in both
directions*, and says why: **only measuring the first produces a system that
hedges everything and scores beautifully.** A set with twelve unanswerable
questions and no answerable ones cannot produce that finding, and nothing in the
arithmetic would say so.

The rest follow the same pattern as #29's, #30's and #74's scope checks: each is
something the ticket asks for in prose, turned into a count the dataset either
meets or does not, and an unmet clause exits the checker non-zero. A gap in what
can be measured is a build failure, or it stays a gap.

**The stricter category is checked in both directions too.** An allergen row the
corpus answers and an allergen row it does not are two different failures --
over-refusing about dairy and guessing about soy -- and a category holding only
one of them is a category whose gate can only ever catch half of what it
promises.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from chip_chat.eval.grounding.questions import Question

__all__ = ["CLAUSES", "RATE_NEEDS", "Clause", "Coverage", "coverage"]

RATE_NEEDS: Final = 3
"""How many rows a category needs before its percentage reads as a rate.

Three, which is not a statistical claim -- it is the point below which the
available answers are 0%, 33%, 67% and 100% and a reader deserves to be told.
The same number, for the same reason, as
:data:`chip_chat.eval.trajectory.coverage.RATE_NEEDS`.
"""


@dataclass(frozen=True, slots=True)
class Clause:
    """One thing the dataset has to contain for the report to be honest.

    Attributes:
        name: How the checker names it.
        minimum: How many rows must satisfy it.
        source: What asks for it, so a reader deciding whether it still applies
            can go and read the argument.
        satisfied_by: The test one row either passes or does not.
    """

    name: str
    minimum: int
    source: str
    satisfied_by: Callable[[Question], bool]

    def met_by(self, rows: Sequence[Question]) -> tuple[str, ...]:
        """The ids of the rows satisfying this clause, in dataset order."""
        return tuple(row.entry_id for row in rows if self.satisfied_by(row))


CLAUSES: Final[tuple[Clause, ...]] = (
    Clause(
        name="questions the published data answers -- over-refusal is observable",
        minimum=8,
        source="#75 -- 'over-refusal measured, not just under-refusal'",
        satisfied_by=lambda row: row.answer_owed,
    ),
    Clause(
        name="questions it does not -- under-refusal is observable",
        minimum=5,
        source="#75 -- claiming what the corpus does not support",
        satisfied_by=lambda row: row.refusal_owed,
    ),
    Clause(
        name="answers that owe a citation",
        minimum=8,
        source="#75 -- 'zero uncited menu claims on the golden set'",
        satisfied_by=lambda row: row.citation_owed,
    ),
    Clause(
        name="allergen and dietary questions",
        minimum=4,
        source="#75 -- 'reported as their own category'",
        satisfied_by=lambda row: row.dietary,
    ),
    Clause(
        name="…of those, ones the published data answers",
        minimum=2,
        source="#75 -- a category that can only catch half is half a gate",
        satisfied_by=lambda row: row.dietary and row.answer_owed,
    ),
    Clause(
        name="…of those, ones it does not",
        minimum=2,
        source="#75 -- the confident wrong answer is the safety issue",
        satisfied_by=lambda row: row.dietary and row.refusal_owed,
    ),
    Clause(
        name="a citation that must render beside the claim",
        minimum=1,
        source="PRD K5, and #57's structured envelope field",
        satisfied_by=lambda row: row.adjacent_owed,
    ),
    Clause(
        name="a refusal that must still show what it read",
        minimum=1,
        source=(
            "#75 -- a refusal is not an excuse from K2; k3-allergen-safety-judgement"
        ),
        satisfied_by=lambda row: row.refusal_owed and row.citation_owed,
    ),
)
"""#75's scope, as clauses the dataset passes or fails."""


@dataclass(frozen=True, slots=True)
class Coverage:
    """What the dataset can and cannot support.

    Attributes:
        rows: How many rows this eval scores at all.
        dietary: How many of them are in the stricter category.
        stated: How many state something the eval can hold a turn to. A row the
            set is silent about is still run -- PRD K2's count is over turns,
            not over the turns somebody chose to look at -- but it cannot fail
            anything, and a reader comparing ``rows`` against a small number of
            findings deserves to know which is which.
        met: Clauses the dataset meets, with the rows that meet them.
        unmet: Clauses it does not.
    """

    rows: int
    dietary: int
    stated: int
    met: tuple[tuple[Clause, tuple[str, ...]], ...]
    unmet: tuple[tuple[Clause, tuple[str, ...]], ...]

    @property
    def complete(self) -> bool:
        """Whether every clause is met. What the checker's exit status is."""
        return not self.unmet

    @property
    def thin_category(self) -> bool:
        """Whether the stricter category is too small for a percentage.

        It is held to counts rather than to a rate precisely so that this does
        not matter to its verdict -- see
        :mod:`chip_chat.eval.grounding.scoring`. It still matters to a reader,
        because a report that prints one number per category invites the
        comparison the design refuses to make.
        """
        return 0 < self.dietary < RATE_NEEDS


def coverage(rows: Sequence[Question]) -> Coverage:
    """Check a dataset's rows against #75's scope.

    Args:
        rows: The questions, as
            :func:`~chip_chat.eval.grounding.questions.questions` built them.

    Returns:
        The coverage.
    """
    scored = [(clause, clause.met_by(rows)) for clause in CLAUSES]
    return Coverage(
        rows=len(rows),
        dietary=sum(1 for row in rows if row.dietary),
        stated=sum(1 for row in rows if row.scores_refusal or row.citation_owed),
        met=tuple((c, ids) for c, ids in scored if len(ids) >= c.minimum),
        unmet=tuple((c, ids) for c, ids in scored if len(ids) < c.minimum),
    )
