"""Where a span tree comes from, and the two directions it can arrive in.

:class:`TraceSource` is one method wide. Anything that can answer a dataset row
and hand back the turn's spans is a source: the in-process slice
(:mod:`chip_chat.eval.trajectory.slice`), a hosted agent behind a URL, or a
recorded trace being re-scored after a shape changed.

**One row's failure is one row's failure.** A source that raises on the eleventh
row must not cost the other thirty-three, so every row runs inside its own
``try`` and an adapter error becomes a recorded
:attr:`~chip_chat.eval.trajectory.trees.Trajectory.error` -- which scores as
unscored, never as a wrong lane. An outage is not a model being wrong, and
:mod:`chip_chat.eval.golden.run` makes the same move for the same reason.

**Live traffic arrives the other way round, and that is why the scorer takes
two sequences.** Here a row is known and its trace is fetched. Against
production there is no row: a trace exists, and what it *should* have done has
to be supplied by something -- a judge, on the turn's text. #74's fourth
acceptance criterion is that this eval also runs against live traffic once
online evals are on (#76), and the seam for it is not another method on this
protocol. It is :func:`chip_chat.eval.trajectory.scoring.score`, which takes
expectations and trajectories as two matched sequences and has never been told
where either came from. An online runner assembles the pairs and calls it; the
shapes, the precedence and the per-lane arithmetic are then the same code, which
is the only way the live number and the dataset number mean the same thing.
"""

from collections.abc import Iterator, Sequence
from typing import Protocol, runtime_checkable

from chip_chat.eval.trajectory.expectations import Expectation
from chip_chat.eval.trajectory.trees import Trajectory

__all__ = ["TraceSource", "run_trajectories"]


@runtime_checkable
class TraceSource(Protocol):
    """Something that can produce one turn's span tree for a dataset row."""

    @property
    def name(self) -> str:
        """What produced the traces, for the report. A deployment, a build, a URL."""
        ...

    def trajectory(self, expectation: Expectation) -> Trajectory:
        """Run one row and read its span tree back.

        Args:
            expectation: The row. Its message, context and persona are what the
                turn has to be run with; a source that drops the context is
                measuring a different question.

        Returns:
            The trajectory. Raising is permitted -- :func:`run_trajectories`
            records it against the row -- but returning a trajectory carrying an
            ``error`` is better where the source knows what went wrong.
        """
        ...


def run_trajectories(
    expectations: Sequence[Expectation],
    source: TraceSource,
    *,
    only: Sequence[str] | None = None,
) -> tuple[Trajectory, ...]:
    """Run every row against one source.

    Args:
        expectations: The rows to run, in dataset order.
        source: What to run them against.
        only: Entry ids to run, for iterating on one row. ``None`` runs all.

    Returns:
        One trajectory per row run, in dataset order.
    """
    return tuple(_trajectories(expectations, source, only))


def _trajectories(
    expectations: Sequence[Expectation],
    source: TraceSource,
    only: Sequence[str] | None,
) -> Iterator[Trajectory]:
    wanted = None if only is None else set(only)
    for expectation in expectations:
        if wanted is not None and expectation.entry_id not in wanted:
            continue
        yield _run_one(expectation, source)


def _run_one(expectation: Expectation, source: TraceSource) -> Trajectory:
    """Run one row, turning a source failure into a recorded line.

    Broad by design, and narrow in what it does with what it catches: a source
    is a network, a model and somebody else's code. What is *not* caught is the
    two that are never data about a row -- ``KeyboardInterrupt`` and
    ``SystemExit`` do not inherit from ``Exception`` and pass straight through.
    """
    try:
        return source.trajectory(expectation)
    except Exception as error:  # a source is somebody else's code; see the docstring
        return Trajectory(
            entry_id=expectation.entry_id,
            error=f"{type(error).__name__}: {error}",
        )
