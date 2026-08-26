"""The baseline document, and the three cells it must never fill in wrongly.

A report is read by somebody who was not there for the run. So the tests here
are about what a reader would conclude: that coverage arrives before the scores,
that an unmeasured metric is a dash rather than a nought, and that the launch
gates are words rather than a percentage.
"""

from pathlib import Path

from chip_chat.eval.golden.cases import ANY_PERSONA, Check, GoldenCase, GoldenSet
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.report import build_report, render
from chip_chat.eval.golden.run import Observation, Signal
from chip_chat.eval.golden.testing import ceiling
from chip_chat.otel.schema import ToolName

_ALL_SIGNALS = frozenset(Signal)


def _case(**overrides: object) -> GoldenCase:
    defaults: dict[str, object] = {
        "case_id": "k1-example",
        "message": "is the barbacoa spicy",
        "tool": ToolName.SEARCH_MENU_KNOWLEDGE,
        "lane": Lane.KNOWLEDGE,
        "requirements": ("K1",),
        "why": "The plainest knowledge question there is.",
        "persona": ANY_PERSONA,
    }
    return GoldenCase(**{**defaults, **overrides})  # type: ignore[arg-type]


def _set(*cases: GoldenCase) -> GoldenSet:
    return GoldenSet(cases=cases, source=Path("fixture.json"))


def _observed(**overrides: object) -> Observation:
    defaults: dict[str, object] = {
        "case_id": "k1-example",
        "tools": ("search_menu_knowledge",),
        "reports": _ALL_SIGNALS,
    }
    return Observation(**{**defaults, **overrides})  # type: ignore[arg-type]


def _render(golden: GoldenSet, observations: list[Observation]) -> str:
    return render(build_report(golden, observations, deployment="fixture"))


def test_coverage_comes_before_the_scores() -> None:
    """A set missing a lane scores well and concludes wrongly."""
    document = _render(_set(_case()), [_observed()])

    assert document.index("## Coverage") < document.index("## Against the PRD's targets")


def test_an_uncovered_requirement_is_named_in_the_document() -> None:
    document = _render(_set(_case()), [_observed()])

    assert "Uncovered requirements" in document
    assert "`A1`" in document


def test_an_unmeasured_metric_is_a_dash_and_never_a_zero() -> None:
    """A nought in the uncited-claims cell is the target being met."""
    observations = [_observed(reports=_ALL_SIGNALS - {Signal.CITATIONS})]

    document = _render(_set(_case()), observations)

    assert "| Menu claims without a citation | 0 | -- |" in document
    assert "not measured" in document


def test_a_measured_gate_prints_its_count() -> None:
    observations = [_observed(claim_class="food", citations=())]

    document = _render(_set(_case()), observations)

    assert "| Menu claims without a citation | 0 | 1 |" in document
    assert "**FAIL**" in document


def test_the_gates_are_words_rather_than_a_percentage() -> None:
    observations = [_observed(claim_class="food", citations=("menu-1",))]

    document = _render(_set(_case()), observations)

    assert "| Both launch gates | pass | pass |" in document


def test_an_empty_lane_has_no_row_rather_than_a_row_of_noughts() -> None:
    document = _render(_set(_case()), [_observed()])

    assert "| knowledge |" in document
    assert "| action |" not in document


def test_the_signals_the_deployment_could_not_report_are_listed() -> None:
    observations = [_observed(reports=frozenset({Signal.TOOLS}))]

    document = _render(_set(_case()), observations)

    assert "The deployment does not report:" in document
    assert "`citations`" in document


def test_the_absent_judge_is_stated_once_rather_than_inferred() -> None:
    case = _case(checks=frozenset({Check.GROUNDED}))

    document = _render(_set(case), [_observed()])

    assert "No judge was supplied" in document
    assert "`grounded`" in document


def test_a_failure_arrives_with_the_reason_the_case_exists() -> None:
    case = _case(forbidden_tools=frozenset({ToolName.GET_POINTS_BALANCE}))
    observations = [_observed(tools=("get_points_balance",))]

    document = _render(_set(case), observations)

    assert "The plainest knowledge question there is." in document
    assert "routing" in document


def test_a_run_with_nothing_wrong_says_so_rather_than_printing_an_empty_table() -> None:
    document = _render(_set(_case()), [_observed()])

    assert "## Failures\n\nNone.\n" in document


def test_the_shipped_set_renders_a_complete_coverage_section(
    golden: GoldenSet,
) -> None:
    document = render(
        build_report(golden, ceiling(golden), deployment="ceiling", judge_name=None)
    )

    assert "Every requirement covered" in document
    assert "Uncovered requirements" not in document
    assert "## Per lane" in document
