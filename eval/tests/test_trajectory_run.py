"""The seam: what a source is, and what happens when one of them breaks.

One row's failure is one row's failure. A runner that stopped on the eleventh
row would have spent the first ten turns for nothing, and an outage recorded as
a wrong lane would send somebody to fix a prompt.
"""

from collections.abc import Mapping
from typing import Any

from chip_chat.eval.trajectory.expectations import Expectation
from chip_chat.eval.trajectory.run import TraceSource, run_trajectories
from chip_chat.eval.trajectory.scoring import score
from chip_chat.eval.trajectory.shapes import Shape
from chip_chat.eval.trajectory.testing import ScriptedSource, turn_spans
from chip_chat.eval.trajectory.trees import Trajectory
from chip_chat.otel.schema import ToolName


class _BrokenSource:
    """A source that raises on one row and answers the rest."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    @property
    def name(self) -> str:
        return "a source that fails on one row"

    def trajectory(self, expectation: Expectation) -> Trajectory:
        if expectation.entry_id == self._entry_id:
            raise RuntimeError("the deployment went away")
        calls: list[tuple[ToolName, Mapping[str, Any]]] = (
            [] if expectation.tool is None else [(expectation.tool, {})]
        )
        return ScriptedSource(
            script={expectation.entry_id: turn_spans(calls)}
        ).trajectory(expectation)


def test_a_source_that_raises_costs_one_row(rows: tuple[Expectation, ...]) -> None:
    """The error is recorded against the row and the run continues."""
    broken = rows[0].entry_id

    trajectories = run_trajectories(rows, _BrokenSource(broken))

    assert len(trajectories) == len(rows)
    assert trajectories[0].error == "RuntimeError: the deployment went away"
    assert all(t.error is None for t in trajectories[1:])


def test_an_outage_is_unscored_rather_than_a_wrong_lane(
    rows: tuple[Expectation, ...],
) -> None:
    """An outage is not a model being wrong, and the two must not share a column."""
    scores = score(rows, run_trajectories(rows, _BrokenSource(rows[0].entry_id)))

    assert scores.judgements[0].shape is Shape.UNSCORED
    assert scores.unscored == 1
    assert scores.split_traces == 0


def test_only_runs_the_named_rows(rows: tuple[Expectation, ...]) -> None:
    """A partial run is a normal thing, and it is matched by id rather than position."""
    wanted = rows[3].entry_id

    trajectories = run_trajectories(rows, ScriptedSource(), only=[wanted])

    assert [t.entry_id for t in trajectories] == [wanted]


def test_the_fixtures_are_sources(rows: tuple[Expectation, ...]) -> None:
    """The seam is one method and a name, so a second adapter is a small thing."""
    assert isinstance(ScriptedSource(), TraceSource)
    assert isinstance(_BrokenSource("x"), TraceSource)
