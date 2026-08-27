"""The headline number, the stricter one beside it, and the breakdown that matters.

PRD section 05 sets tool-selection accuracy at **≥ 95%**, the highest bar in the
table, and the system design says why: *whether the model picks the right lane is
the single question the entire architecture turns on*. This module is where that
number is computed, and three properties of it are deliberate.

**Two rates, not one.** :attr:`TrajectoryScores.tool_selection` is the metric the
target is set on -- the expected tool was reached and nothing forbidden was --
and it is the same rule :func:`chip_chat.eval.golden.scoring.score` applies, so
the two reports cannot quote different numbers for the same thing.
:attr:`TrajectoryScores.clean` is the stricter reading: the whole trajectory was
right, extra calls and drifted queries included. The gap between them is the cost
and the sloppiness that a lane-selection rate is blind to by construction.

**Per lane, always.** #74 asks for the breakdown *because a 95% aggregate can
hide a vision lane at 60%*, and the aggregate is computed from the lanes rather
than beside them so the two cannot disagree. Lanes with no rows are kept as
empty rows: a lane that lost its cases has to be visible as an absence rather
than as an omission.

**Unscored is in no numerator and no denominator, and is counted out loud.** A
turn whose trace arrived split is not a turn that routed badly -- it is a turn
nobody can say anything about, and #103 is where that gets fixed rather than in
a prompt. :attr:`TrajectoryScores.unscored` and
:attr:`TrajectoryScores.split_traces` are counts, and the report prints them
above the rates.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.scoring import TOOL_SELECTION_TARGET
from chip_chat.eval.trajectory.expectations import Expectation
from chip_chat.eval.trajectory.shapes import FAILURE_SHAPES, Judgement, Shape, classify
from chip_chat.eval.trajectory.trees import Trajectory

__all__ = [
    "TOOL_SELECTION_TARGET",
    "LaneTrajectories",
    "TrajectoryScores",
    "score",
]


@dataclass(frozen=True, slots=True)
class LaneTrajectories:
    """One lane's turns, and what became of them.

    Attributes:
        lane: Which lane. Includes
            :attr:`~chip_chat.eval.golden.lanes.Lane.NONE`, whose rows are the
            turns that should reach for nothing -- a lane in the arithmetic
            even though it is not a lane in the architecture, because *call
            nothing* is an answer routing can be wrong about.
        judgements: Its rows, in dataset order.
    """

    lane: Lane
    judgements: tuple[Judgement, ...]

    @property
    def total(self) -> int:
        """How many rows this lane holds."""
        return len(self.judgements)

    @property
    def scored(self) -> int:
        """Rows whose trajectory could be read at all."""
        return sum(1 for judgement in self.judgements if judgement.scored)

    @property
    def selected(self) -> int:
        """Rows that reached the expected tool and avoided the forbidden ones."""
        return sum(1 for judgement in self.judgements if judgement.selected)

    @property
    def clean(self) -> int:
        """Rows whose whole trajectory was right."""
        return sum(1 for judgement in self.judgements if judgement.clean)

    @property
    def tool_selection(self) -> float | None:
        """Lane selection over the rows that could be scored, or ``None``."""
        return None if not self.scored else self.selected / self.scored

    @property
    def clean_rate(self) -> float | None:
        """Clean trajectories over the rows that could be scored, or ``None``."""
        return None if not self.scored else self.clean / self.scored

    @property
    def shapes(self) -> Mapping[Shape, int]:
        """How many rows took each shape, :class:`Shape` order, zeroes included."""
        return {
            shape: sum(1 for judgement in self.judgements if judgement.shape is shape)
            for shape in Shape
        }

    @property
    def meets_target(self) -> bool | None:
        """Whether this lane is at PRD's target, or ``None`` where nothing was scored."""
        rate = self.tool_selection
        return None if rate is None else rate >= TOOL_SELECTION_TARGET


@dataclass(frozen=True, slots=True)
class TrajectoryScores:
    """Everything the run says, before it is a document.

    Attributes:
        judgements: One per row scored, in dataset order.
        lanes: Per-lane totals in :class:`~chip_chat.eval.golden.lanes.Lane`
            order, empty lanes included.
    """

    judgements: tuple[Judgement, ...]
    lanes: tuple[LaneTrajectories, ...]

    @property
    def total(self) -> int:
        """How many rows were run."""
        return len(self.judgements)

    @property
    def scored(self) -> int:
        """Rows whose trajectory could be read."""
        return sum(lane.scored for lane in self.lanes)

    @property
    def unscored(self) -> int:
        """Rows whose span tree could not be believed. See the module docstring."""
        return self.total - self.scored

    @property
    def split_traces(self) -> int:
        """Rows that arrived as more than one trace.

        The #103 counter. Nonzero means trace context is not propagating across
        the app-to-agent boundary, every count in this report is over a subset
        nobody chose, and the thing to fix is the propagation rather than
        anything a model did. ``make trace-boundary``.
        """
        return sum(1 for judgement in self.judgements if judgement.trajectory.split)

    @property
    def tool_selection(self) -> float | None:
        """PRD section 05's metric, over every row that could be scored."""
        scored = self.scored
        return None if not scored else sum(lane.selected for lane in self.lanes) / scored

    @property
    def clean(self) -> float | None:
        """Clean trajectories over every row that could be scored."""
        scored = self.scored
        return None if not scored else sum(lane.clean for lane in self.lanes) / scored

    @property
    def meets_target(self) -> bool | None:
        """Whether the headline is at ≥ 95%, or ``None`` where nothing was scored."""
        rate = self.tool_selection
        return None if rate is None else rate >= TOOL_SELECTION_TARGET

    @property
    def gap(self) -> float | None:
        """How far the headline is below the target, or ``0.0`` where it is not.

        #74's third acceptance criterion asks for the gap *explained* where the
        target is not met, and an explanation starts with its size. The shapes
        below it are the explanation itself: a gap made of ``no_tool`` and a gap
        made of ``wrong_lane`` are the same number and two different problems.
        """
        rate = self.tool_selection
        return None if rate is None else max(0.0, TOOL_SELECTION_TARGET - rate)

    @property
    def shapes(self) -> Mapping[Shape, int]:
        """How many rows took each shape across the whole run."""
        return {
            shape: sum(1 for judgement in self.judgements if judgement.shape is shape)
            for shape in Shape
        }

    @property
    def query_scoreable(self) -> int:
        """Rows where *right lane, wrong query* could be observed at all.

        Two of the eleven tools take the ask as an argument, so this is well
        under the total by construction. Printed rather than inferred, because
        ``wrong_query: 0`` over rows that could never have shown one is not
        evidence that no query drifted.
        """
        return sum(1 for j in self.judgements if j.expectation.scores_query)

    def failures(self) -> tuple[Judgement, ...]:
        """Every row that took a failure shape, worst shape first then in set order.

        Ordered by :data:`~chip_chat.eval.trajectory.shapes.FAILURE_SHAPES`,
        which is #74's own order: wrong lane, no tool, extra tools, wrong query.
        """
        by_shape = {shape: index for index, shape in enumerate(FAILURE_SHAPES)}
        failed = [j for j in self.judgements if j.shape in by_shape]
        return tuple(sorted(failed, key=lambda j: by_shape[j.shape]))

    def unreadable(self) -> tuple[Judgement, ...]:
        """Every row whose trace could not be believed, in set order."""
        return tuple(j for j in self.judgements if j.shape is Shape.UNSCORED)


def score(
    expectations: Sequence[Expectation],
    trajectories: Sequence[Trajectory],
) -> TrajectoryScores:
    """Score a run.

    Args:
        expectations: The dataset rows that were run.
        trajectories: What came back, matched to rows by
            :attr:`~chip_chat.eval.trajectory.trees.Trajectory.entry_id` rather
            than by position -- a partial run with ``--only`` is a normal thing
            to score, and a positional match would silently score the wrong rows.

    Returns:
        The scores. Rows with no trajectory are skipped rather than failed:
        they were not run.
    """
    by_id = {trajectory.entry_id: trajectory for trajectory in trajectories}
    judgements = tuple(
        classify(expectation, by_id[expectation.entry_id])
        for expectation in expectations
        if expectation.entry_id in by_id
    )
    return TrajectoryScores(
        judgements=judgements,
        lanes=tuple(
            LaneTrajectories(
                lane=lane,
                judgements=tuple(j for j in judgements if j.lane is lane),
            )
            for lane in Lane
        ),
    )
