"""Can this dataset support the numbers #74 asks for? Answered before the run.

The scorer says how a deployment did. This says whether the rows it was given
can carry the claims the report will make about them -- and that failure is
invisible to any rate, because a per-lane table with one row in a lane prints a
number that reads exactly like a number computed over thirty.

Two things are checked, and they fail differently.

**The clauses block.** Each is something #74 asks for in prose, turned into a
count the dataset either meets or does not: a row in every lane, or the per-lane
breakdown is missing a lane; a row that names a forbidden tool, or *wrong lane*
is a shape nothing in the set can demonstrate; a row whose tool takes the ask as
an argument, or *right lane, wrong query* is unobservable everywhere; a row whose
tool has a sanctioned companion, or
:data:`~chip_chat.eval.trajectory.expectations.SANCTIONED` is a table no run ever
exercises and could be wrong without anything saying so. An unmet clause exits
the checker non-zero, the same way #29's and #30's do: a gap in what can be
measured is a build failure, or it stays a gap.

**Thin lanes warn.** :data:`RATE_NEEDS` rows is what it takes for a lane's
percentage to be worth printing as one; below it, the only rates available are
0%, 100% and a couple of coarse fractions in between. That is not a reason to
refuse the dataset -- the vision lane holds exactly one routing row on purpose,
because ``eval/photos`` is where the vision lane is actually scored and
``eval/README.md`` explains the division -- but it is a reason for the report to
say so above the table rather than let a reader take ``100%`` from one row as a
measurement.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.trajectory.expectations import Expectation

__all__ = [
    "CLAUSES",
    "RATE_NEEDS",
    "Clause",
    "Coverage",
    "coverage",
]

RATE_NEEDS: Final = 3
"""How many rows a lane needs before its percentage reads as a rate.

Three, which is not a statistical claim -- it is the point below which the
available answers are 0%, 33%, 67% and 100% and a reader deserves to be told.
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
    satisfied_by: Callable[[Expectation], bool]

    def met_by(self, rows: Sequence[Expectation]) -> tuple[str, ...]:
        """The ids of the rows satisfying this clause, in dataset order."""
        return tuple(row.entry_id for row in rows if self.satisfied_by(row))


def _in(lane: Lane) -> Callable[[Expectation], bool]:
    """A test for a row expecting ``lane``."""
    return lambda row: row.lane is lane


CLAUSES: Final[tuple[Clause, ...]] = (
    Clause(
        name="knowledge rows",
        minimum=1,
        source="#74 -- break down by lane",
        satisfied_by=_in(Lane.KNOWLEDGE),
    ),
    Clause(
        name="account rows",
        minimum=1,
        source="#74 -- break down by lane",
        satisfied_by=_in(Lane.ACCOUNT),
    ),
    Clause(
        name="personalization rows",
        minimum=1,
        source="#74 -- break down by lane",
        satisfied_by=_in(Lane.PERSONALIZATION),
    ),
    Clause(
        name="action rows",
        minimum=1,
        source="#74 -- break down by lane",
        satisfied_by=_in(Lane.ACTION),
    ),
    Clause(
        name="vision rows",
        minimum=1,
        source="#74 -- break down by lane; eval/photos scores the rest",
        satisfied_by=_in(Lane.VISION),
    ),
    Clause(
        name="rows that should reach for nothing",
        minimum=1,
        source="#74 -- 'no tool' is only a failure where a tool was owed",
        satisfied_by=_in(Lane.NONE),
    ),
    Clause(
        name="boundary rows -- a tool the turn must not reach for",
        minimum=10,
        source="#29's scope, which is what makes wrong_lane nameable",
        satisfied_by=lambda row: bool(row.forbidden),
    ),
    Clause(
        name="rows whose tool takes the ask as an argument",
        minimum=4,
        source="#74 -- right lane, wrong query",
        satisfied_by=lambda row: row.scores_query,
    ),
    Clause(
        name="rows whose tool has a sanctioned companion",
        minimum=1,
        source="SANCTIONED, which nothing else exercises",
        satisfied_by=lambda row: bool(row.sanctioned),
    ),
)
"""#74's scope, as clauses the dataset passes or fails."""


@dataclass(frozen=True, slots=True)
class Coverage:
    """What the dataset can and cannot support.

    Attributes:
        rows: How many rows score routing at all.
        per_lane: How many rows each lane holds, in
            :class:`~chip_chat.eval.golden.lanes.Lane` order.
        met: Clauses the dataset meets, with the rows that meet them.
        unmet: Clauses it does not.
    """

    rows: int
    per_lane: tuple[tuple[Lane, int], ...]
    met: tuple[tuple[Clause, tuple[str, ...]], ...]
    unmet: tuple[tuple[Clause, tuple[str, ...]], ...]

    @property
    def complete(self) -> bool:
        """Whether every clause is met. What the checker's exit status is."""
        return not self.unmet

    @property
    def thin(self) -> tuple[Lane, ...]:
        """Lanes with rows, but too few for a percentage. See :data:`RATE_NEEDS`."""
        return tuple(lane for lane, held in self.per_lane if 0 < held < RATE_NEEDS)

    @property
    def empty_lanes(self) -> tuple[Lane, ...]:
        """Lanes with no rows at all, which is an unmet clause as well."""
        return tuple(lane for lane, held in self.per_lane if not held)


def coverage(rows: Sequence[Expectation]) -> Coverage:
    """Check a dataset's routing rows against #74's scope.

    Args:
        rows: The expectations, as
            :func:`~chip_chat.eval.trajectory.expectations.expectations` built
            them.

    Returns:
        The coverage.
    """
    scored = [(clause, clause.met_by(rows)) for clause in CLAUSES]
    return Coverage(
        rows=len(rows),
        per_lane=tuple(
            (lane, sum(1 for row in rows if row.lane is lane)) for lane in Lane
        ),
        met=tuple((c, ids) for c, ids in scored if len(ids) >= c.minimum),
        unmet=tuple((c, ids) for c, ids in scored if len(ids) < c.minimum),
    )
