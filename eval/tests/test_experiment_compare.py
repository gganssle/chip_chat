"""Two results, subtracted — and the aggregate that lies, caught.

The test this module exists for is
:func:`test_a_lane_regression_is_reported_even_when_the_aggregate_improved`.
Everything else here is arithmetic; that one is #73's sentence.
"""

from chip_chat.eval.experiment.compare import MATERIAL, compare
from chip_chat.eval.experiment.report import render_comparison
from chip_chat.eval.experiment.results import (
    ExperimentResult,
    LaneResult,
    Metric,
    RequirementResult,
)


def _lane(
    name: str, completion: float | None, selection: float | None = 1.0, **shapes: int
) -> LaneResult:
    return LaneResult(
        lane=name,
        cases=10,
        passed=int((completion or 0) * 10),
        failed=10 - int((completion or 0) * 10),
        unscored=0,
        completion=completion,
        tool_selection=selection,
        shapes={
            "wrong_lane": 0,
            "no_tool": 0,
            "extra_tools": 0,
            "wrong_query": 0,
            **shapes,
        },
    )


def _result(
    name: str,
    *,
    completion: float | None = 0.5,
    uncited: float | None = 0.0,
    lanes: tuple[LaneResult, ...] = (),
    requirements: tuple[RequirementResult, ...] = (),
    fingerprint: str = "aaaaaaaaaaaa",
    dataset_version: str = "9ba196eb786c",
    judge: str = "",
) -> ExperimentResult:
    return ExperimentResult(
        experiment=name,
        fingerprint=fingerprint,
        configuration={},
        prompt_version="v1+abc",
        dataset="cilantro-golden-set",
        dataset_version=dataset_version,
        source="a slice",
        judge=judge,
        rows=34,
        metrics=(
            Metric("completion", completion, scored=34, asked=34),
            Metric("tool_selection", 0.9, scored=34, asked=34),
            Metric("groundedness", None),
            Metric("uncited_claims", uncited, scored=34, asked=34),
            Metric("photo_f1", None, note="delegated"),
        ),
        lanes=lanes,
        requirements=requirements,
    )


def test_a_lane_regression_is_reported_even_when_the_aggregate_improved() -> None:
    """#73's sentence, as the assertion the whole module exists for."""
    baseline = _result(
        "before",
        completion=0.60,
        lanes=(_lane("knowledge", 0.50), _lane("account", 0.90)),
    )
    candidate = _result(
        "after",
        completion=0.70,
        fingerprint="bbbbbbbbbbbb",
        lanes=(_lane("knowledge", 0.90), _lane("account", 0.40)),
    )

    comparison = compare(baseline, candidate)

    assert any("Task completion" in line for line in comparison.improvements)
    assert any("lane account" in line for line in comparison.regressions)
    assert "regression" in comparison.verdict


def test_a_count_regresses_on_any_movement_and_a_rate_needs_a_material_one() -> None:
    """PRD §05 makes the gates zero. One more than zero is one more than zero."""
    baseline = _result("before", completion=0.60, uncited=0.0)
    candidate = _result(
        "after", completion=0.60 - MATERIAL / 2, uncited=1.0, fingerprint="bbbbbbbbbbbb"
    )

    comparison = compare(baseline, candidate)
    regressions = comparison.regressions

    assert any("without a citation" in line for line in regressions)
    assert not any("Task completion" in line for line in regressions)


def test_an_unmeasured_metric_produces_no_delta_rather_than_zero() -> None:
    baseline = _result("before")
    candidate = _result("after", fingerprint="bbbbbbbbbbbb")

    comparison = compare(baseline, candidate)
    grounded = next(m for m in comparison.metrics if m.target.metric == "groundedness")

    assert grounded.delta is None
    assert grounded.regressed is False


def test_a_metric_that_stopped_being_measured_is_named() -> None:
    """The one improvement nobody should accept without reading why."""
    baseline = _result("before", completion=0.6)
    candidate = _result("after", completion=None, fingerprint="bbbbbbbbbbbb")

    comparison = compare(baseline, candidate)

    assert any("unmeasured now" in line for line in comparison.regressions)


def test_a_different_dataset_version_is_a_warning_rather_than_a_refusal() -> None:
    """Refusing would make the harness useless exactly when somebody needs it."""
    baseline = _result("before")
    candidate = _result(
        "after", dataset_version="ffffffffffff", fingerprint="bbbbbbbbbbbb"
    )

    comparison = compare(baseline, candidate)

    assert comparison.comparable is False
    assert any("different dataset versions" in note for note in comparison.warnings)


def test_two_runs_of_the_same_configuration_are_named_as_variance() -> None:
    comparison = compare(_result("before"), _result("after"))

    assert any("run-to-run variance" in note for note in comparison.warnings)


def test_a_requirement_regression_has_no_threshold() -> None:
    """A requirement covered by two cases has no resolution for a threshold."""
    before = RequirementResult("K1", "knowledge", cases=2, passed=2)
    after = RequirementResult("K1", "knowledge", cases=2, passed=1, failed=1)

    comparison = compare(
        _result("before", requirements=(before,)),
        _result("after", requirements=(after,), fingerprint="bbbbbbbbbbbb"),
    )

    assert any("requirement K1" in line for line in comparison.regressions)


def test_the_failure_shapes_that_moved_are_reported_even_at_a_flat_rate() -> None:
    """The same number made of different shapes is two different problems."""
    baseline = _result("before", lanes=(_lane("account", 0.5, 0.5, no_tool=4),))
    candidate = _result(
        "after",
        lanes=(_lane("account", 0.5, 0.5, wrong_lane=4),),
        fingerprint="bbbbbbbbbbbb",
    )

    comparison = compare(baseline, candidate)
    lane = comparison.lanes[0]

    assert lane.shape_deltas()["no_tool"] == -4
    assert lane.shape_deltas()["wrong_lane"] == 4


def test_the_document_puts_the_regressions_above_the_numbers() -> None:
    baseline = _result("before", lanes=(_lane("account", 0.9),))
    candidate = _result(
        "after", lanes=(_lane("account", 0.4),), fingerprint="bbbbbbbbbbbb"
    )

    document = render_comparison(compare(baseline, candidate))

    assert document.index("What got worse") < document.index("The targets, side by side")
    assert "measured in" not in document.split("## By requirement")[0]
