"""Two results, subtracted — and the aggregate that lies, caught.

The test this module exists for is
:func:`test_a_lane_regression_is_reported_even_when_the_aggregate_improved`.
Everything else here is arithmetic; that one is #73's sentence.
"""

import json
from pathlib import Path

from chip_chat.eval.experiment.compare import MATERIAL, compare
from chip_chat.eval.experiment.report import render_comparison
from chip_chat.eval.experiment.results import (
    ExperimentResult,
    LaneResult,
    Metric,
    RequirementResult,
    load_result,
    write_result,
)
from chip_chat.eval.wiring import UNSTATED


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
    wiring: str = "none",
) -> ExperimentResult:
    # `wiring` defaults to a stated `none` rather than to UNSTATED, because an
    # unstated side makes `compare` refuse to draw anything and every test below
    # this one is about the arithmetic. The refusal has its own tests.
    return ExperimentResult(
        experiment=name,
        fingerprint=fingerprint,
        configuration={},
        prompt_version="v1+abc",
        dataset="cilantro-golden-set",
        dataset_version=dataset_version,
        source="a slice",
        wiring=wiring,
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


def test_a_comparison_refuses_a_side_that_did_not_say_which_lanes_it_had() -> None:
    """The refusal ``cc-lanes`` is about, and the one thing this module declines.

    A result recorded before the harness wrote the lane configuration down is
    not a run with an unknown-but-small difference from a run that recorded one:
    it is a run whose delta could be entirely a lane coming up, and a reader
    would see a number that looks like a better model. So the tables are not
    drawn at all.
    """
    baseline = _result("before", wiring=UNSTATED)
    candidate = _result("after", fingerprint="bbbbbbbbbbbb", wiring="account")

    comparison = compare(baseline, candidate)
    document = render_comparison(comparison)

    assert not comparison.stated
    assert comparison.unstated_sides == ("baseline 'before'",)
    assert "No comparison" in document
    assert "The targets, side by side" not in document


def test_both_sides_unstated_names_both_of_them() -> None:
    comparison = compare(
        _result("before", wiring=UNSTATED),
        _result("after", fingerprint="bbbbbbbbbbbb", wiring=UNSTATED),
    )

    assert comparison.unstated_sides == ("baseline 'before'", "candidate 'after'")


def test_two_stated_wirings_are_compared_and_the_difference_is_a_warning() -> None:
    """Stated and different is the interesting comparison, not a refused one.

    Wired against unwired is the delta between what the model can do and what
    the deployment lets it do, which is the most useful subtraction this harness
    performs. Refusing it would be the wrong lesson to draw from the refusal
    above: what cannot be read is an *unlabelled* difference.
    """
    baseline = _result("before", wiring="none")
    candidate = _result("after", wiring="account+personalization")

    comparison = compare(baseline, candidate)
    document = render_comparison(comparison)

    assert comparison.stated
    assert any("different lanes wired" in note for note in comparison.warnings)
    assert "The targets, side by side" in document


def test_the_same_fingerprint_under_two_wirings_is_not_called_run_to_run_noise() -> None:
    """It is a measurement of the wiring, and the warning has to say so."""
    comparison = compare(
        _result("before", wiring="none"),
        _result("after", wiring="account+personalization"),
    )

    variance = [note for note in comparison.warnings if "fingerprint" in note]

    assert variance
    assert "measuring the wiring" in variance[0]
    assert "run-to-run variance" not in variance[0]


def test_a_result_records_its_wiring_and_reads_it_back(tmp_path: Path) -> None:
    """The column survives a round trip, which is what a comparison reads."""
    path = tmp_path / "recorded.json"
    write_result(_result("wired", wiring="account+personalization"), path)

    assert load_result(path).wiring == "account+personalization"


def test_a_result_recorded_before_the_column_existed_reads_back_unstated(
    tmp_path: Path,
) -> None:
    """An old file is a file that did not say, and must not read as ``none``.

    Defaulting the missing key to ``none`` would be a guess -- almost certainly
    a correct one, since nothing could wire a lane before this landed -- written
    into a record as though it had been measured.
    """
    path = tmp_path / "old.json"
    payload = dict(_result("old").as_json())
    del payload["wiring"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_result(path).wiring == UNSTATED
