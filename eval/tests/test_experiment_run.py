"""One configuration in, one comparable result out — driven with no model.

The runner is the piece #73 makes a claim about, and the claim is that it is
cheap. So every test here uses the routing oracle, which is free, and the one
number that matters is asserted directly: **one turn per row**, not three. A
harness that ran the golden set, the trajectory eval and the grounding eval in
sequence would be three times the cost for the same information, and nothing but
a count can tell the two designs apart from the outside.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from chip_chat.agent.model import ModelReply
from chip_chat.eval.dataset.build import Dataset
from chip_chat.eval.experiment.configurations import ExperimentConfiguration
from chip_chat.eval.experiment.results import TARGETS
from chip_chat.eval.experiment.run import run_experiment
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.golden.run import Signal
from chip_chat.eval.golden.slice import SliceDeployment
from chip_chat.eval.golden.testing import RoutingOracle

FIXED = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


@dataclass
class CountingOracle:
    """The routing oracle, wrapped so the round trips can be counted."""

    inner: RoutingOracle
    calls: list[str] = field(default_factory=list)

    @property
    def deployment(self) -> str:
        return self.inner.deployment

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        tools: Sequence[Mapping[str, Any]] = (),
        on_text: Callable[[str], None] | None = None,
    ) -> ModelReply:
        self.calls.append("complete")
        return self.inner.complete(messages, tools=tools, on_text=on_text)


def _arm(name: str = "an-arm") -> ExperimentConfiguration:
    return ExperimentConfiguration(name=name, why="a test")


def test_a_run_produces_one_result_carrying_both_breakdowns(
    golden: GoldenSet, shipped: Dataset
) -> None:
    experiment = run_experiment(
        _arm(),
        golden,
        shipped,
        lambda _: SliceDeployment(RoutingOracle(golden)),
        now=FIXED,
    )

    result = experiment.result
    assert result.rows == len(shipped.entries)
    assert {lane.lane for lane in result.lanes} >= {"knowledge", "account", "action"}
    assert {item.requirement for item in result.requirements} >= {"K1", "A1", "T1"}
    assert {metric.metric for metric in result.metrics} == {
        target.metric for target in TARGETS
    }


def test_every_row_is_run_exactly_once_across_all_three_scorers(
    golden: GoldenSet, shipped: Dataset
) -> None:
    """#73's *cheap enough to run on a whim*, as a count of round trips.

    The oracle answers a routing case in two calls -- one to reach for the tool
    and one to answer once it has -- so the ceiling is two per row. Three
    separate runners would be six.
    """
    oracle = CountingOracle(RoutingOracle(golden))

    experiment = run_experiment(
        _arm(),
        golden,
        shipped,
        lambda _: SliceDeployment(oracle),
        now=FIXED,
    )

    assert len(oracle.calls) <= 2 * len(experiment.recorded)


def test_the_recorded_result_round_trips_through_json(
    golden: GoldenSet, shipped: Dataset, tmp_path: Any
) -> None:
    """A candidate scored today is compared to a baseline recorded weeks ago."""
    from chip_chat.eval.experiment.results import load_result, write_result

    experiment = run_experiment(
        _arm(),
        golden,
        shipped,
        lambda _: SliceDeployment(RoutingOracle(golden)),
        now=FIXED,
    )
    path = tmp_path / "result.json"
    write_result(experiment.result, path)

    read_back = load_result(path)

    assert read_back.as_json() == experiment.result.as_json()


def test_an_unmeasured_target_leaves_the_verdict_neither_met_nor_missed(
    golden: GoldenSet, shipped: Dataset
) -> None:
    """A target nobody measured has not passed, and has not failed either."""
    experiment = run_experiment(
        _arm(),
        golden,
        shipped,
        lambda _: SliceDeployment(RoutingOracle(golden)),
        now=FIXED,
    )

    result = experiment.result
    assert result.metric("groundedness").value is None
    assert result.targets_met is None


def test_the_prompt_axis_is_recorded_as_inert_under_the_oracle(
    golden: GoldenSet, shipped: Dataset
) -> None:
    """Two arms differing only in prompt would produce identical numbers here.

    That is what *nothing read the change* looks like, and reporting it as *no
    difference* would be the single most misleading document this harness could
    emit.
    """
    experiment = run_experiment(
        _arm(),
        golden,
        shipped,
        lambda _: SliceDeployment(RoutingOracle(golden)),
        prompt_read=False,
        now=FIXED,
    )

    assert "prompt" in experiment.result.inert_axes


def test_a_deployment_that_raises_costs_one_row_rather_than_the_run(
    golden: GoldenSet, shipped: Dataset
) -> None:
    class Exploding:
        name = "explodes on the eleventh"
        reports: frozenset[Signal] = frozenset()
        seen = 0

        def turn(self, case: Any) -> Any:
            Exploding.seen += 1
            if Exploding.seen == 11:
                raise RuntimeError("the deployment fell over")
            return SliceDeployment(RoutingOracle(golden)).turn(case)

    experiment = run_experiment(_arm(), golden, shipped, lambda _: Exploding(), now=FIXED)

    errors = [row for row in experiment.recorded if row.observation.error is not None]
    assert len(experiment.recorded) == len(shipped.entries)
    assert any("fell over" in (row.observation.error or "") for row in errors)


def test_the_spans_are_kept_so_a_run_can_be_written_out_as_a_capture(
    golden: GoldenSet, shipped: Dataset
) -> None:
    """#77 reads captures, and a run that already produced a tree is the source."""
    experiment = run_experiment(
        _arm(),
        golden,
        shipped,
        lambda _: SliceDeployment(RoutingOracle(golden)),
        now=FIXED,
    )

    answered = [row for row in experiment.recorded if row.observation.error is None]
    assert answered
    assert all(row.spans for row in answered)
    assert any(span.name == "chat.turn" for span in answered[0].spans)


def test_a_delegated_requirement_is_neither_covered_here_nor_a_gap(
    golden: GoldenSet, shipped: Dataset
) -> None:
    experiment = run_experiment(
        _arm(),
        golden,
        shipped,
        lambda _: SliceDeployment(RoutingOracle(golden)),
        now=FIXED,
    )

    vision = [item for item in experiment.result.requirements if item.requirement == "V3"]
    assert len(vision) == 1
    assert vision[0].delegated_to
    assert vision[0].cases == 0
