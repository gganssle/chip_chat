"""The scorer's arithmetic, and the third verdict it exists for.

Two verdicts would be enough for a scorer nobody had to trust. This one has to
distinguish *wrong* from *not asked*, because the deployment it will spend most
of its life pointed at cannot report citations and a column of zeros there would
read as an agent that never cites.

The method is the labeled photo set's: start from a run that is right by
construction, introduce one known change, and check that the one cell that
should have moved is the one that did.
"""

from collections.abc import Mapping, Sequence

import pytest

from chip_chat.eval.golden.cases import ANY_PERSONA, Check, GoldenCase, GoldenSet
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.run import Observation, Signal
from chip_chat.eval.golden.scoring import LaneScore, Verdict, score
from chip_chat.otel.schema import ToolName

_ALL_SIGNALS = frozenset(Signal)


def _case(**overrides: object) -> GoldenCase:
    defaults: dict[str, object] = {
        "case_id": "case-1",
        "message": "is the barbacoa spicy",
        "tool": ToolName.SEARCH_MENU_KNOWLEDGE,
        "lane": Lane.KNOWLEDGE,
        "requirements": ("K1",),
        "why": "A fixture.",
        "persona": ANY_PERSONA,
    }
    return GoldenCase(**{**defaults, **overrides})  # type: ignore[arg-type]


def _set(*cases: GoldenCase) -> GoldenSet:
    from pathlib import Path

    return GoldenSet(cases=cases, source=Path("fixture.json"))


def _observed(**overrides: object) -> Observation:
    defaults: dict[str, object] = {
        "case_id": "case-1",
        "reply": "Moderately.",
        "tools": ("search_menu_knowledge",),
        "reports": _ALL_SIGNALS,
    }
    return Observation(**{**defaults, **overrides})  # type: ignore[arg-type]


class _Yes:
    """A judge that says yes to everything. For driving the plumbing, not a judge."""

    def verdict(
        self, check: Check, case: GoldenCase, observation: Observation
    ) -> bool | None:
        return True


class _Unsure:
    """A judge unwilling to say. The verdict that must not become a failure."""

    def verdict(
        self, check: Check, case: GoldenCase, observation: Observation
    ) -> bool | None:
        return None


def test_reaching_the_expected_tool_passes() -> None:
    result = score(_set(_case()), [_observed()]).results[0]

    assert result.routing is Verdict.PASS
    assert result.verdict is Verdict.PASS


def test_a_turn_that_also_calls_another_tool_still_routes() -> None:
    """A whole turn is not one call. See the module docstring in scoring.py."""
    case = _case(tool=ToolName.GET_USUAL_ORDER, lane=Lane.PERSONALIZATION)
    observation = _observed(tools=("get_usual_order", "propose_order"))

    assert score(_set(case), [observation]).results[0].routing is Verdict.PASS


def test_a_forbidden_tool_fails_the_routing_however_right_the_rest_is() -> None:
    case = _case(forbidden_tools=frozenset({ToolName.GET_POINTS_BALANCE}))
    observation = _observed(tools=("search_menu_knowledge", "get_points_balance"))

    assert score(_set(case), [observation]).results[0].routing is Verdict.FAIL


def test_a_case_in_no_lane_wants_no_call_at_all() -> None:
    case = _case(tool=None, lane=Lane.NONE)

    assert score(_set(case), [_observed(tools=())]).results[0].routing is Verdict.PASS
    assert score(_set(case), [_observed()]).results[0].routing is Verdict.FAIL


def test_routing_is_unscored_where_the_deployment_cannot_report_tools() -> None:
    """The finding this type exists for: an HTTP reply carries no tool calls."""
    observation = _observed(tools=(), reports=_ALL_SIGNALS - {Signal.TOOLS})
    result = score(_set(_case()), [observation]).results[0]

    assert result.routing is Verdict.UNSCORED
    assert result.verdict is Verdict.UNSCORED


def test_an_unscored_check_never_reads_as_a_pass() -> None:
    case = _case(checks=frozenset({Check.CITES}))
    observation = _observed(reports=_ALL_SIGNALS - {Signal.CITATIONS})
    scores = score(_set(case), [observation])

    assert scores.results[0].checks[Check.CITES] is Verdict.UNSCORED
    assert scores.passed == 0
    assert scores.unscored == 1
    assert scores.completion == 0.0


def test_a_judged_check_is_unscored_without_a_judge() -> None:
    case = _case(checks=frozenset({Check.DECLINES}))

    assert score(_set(case), [_observed()]).results[0].verdict is Verdict.UNSCORED


def test_a_judge_unwilling_to_say_is_not_a_failure() -> None:
    case = _case(checks=frozenset({Check.DECLINES}))
    scores = score(_set(case), [_observed()], judge=_Unsure())

    assert scores.results[0].checks[Check.DECLINES] is Verdict.UNSCORED


def test_a_judge_that_answers_scores_the_check() -> None:
    case = _case(checks=frozenset({Check.DECLINES}))
    scores = score(_set(case), [_observed()], judge=_Yes())

    assert scores.results[0].checks[Check.DECLINES] is Verdict.PASS
    assert scores.results[0].verdict is Verdict.PASS


def test_a_citation_is_the_presence_of_an_id_and_nothing_else() -> None:
    case = _case(checks=frozenset({Check.CITES}))

    passing = score(_set(case), [_observed(citations=("menu-1",))]).results[0]
    failing = score(_set(case), [_observed(citations=())]).results[0]

    assert passing.checks[Check.CITES] is Verdict.PASS
    assert failing.checks[Check.CITES] is Verdict.FAIL


def test_adjacent_placement_follows_the_claim_class_the_renderer_reads() -> None:
    case = _case(checks=frozenset({Check.CITES, Check.CITES_ADJACENT}))
    allergen = _observed(citations=("menu-1",), claim_class="allergen")
    food = _observed(citations=("menu-1",), claim_class="food")

    assert (
        score(_set(case), [allergen]).results[0].checks[Check.CITES_ADJACENT]
        is Verdict.PASS
    )
    assert (
        score(_set(case), [food]).results[0].checks[Check.CITES_ADJACENT] is Verdict.FAIL
    )


def test_before_a_confirmation_the_turn_owes_a_card_and_no_write() -> None:
    case = _case(
        tool=ToolName.PROPOSE_ORDER,
        lane=Lane.ACTION,
        requirements=("T2",),
        checks=frozenset({Check.CONFIRMS_FIRST}),
    )
    carded = _observed(tools=("propose_order",), card={"draft_id": "d1"}, wrote=False)
    wrote = _observed(tools=("propose_order",), card={"draft_id": "d1"}, wrote=True)

    assert _confirms(case, carded) is Verdict.PASS
    assert _confirms(case, wrote) is Verdict.FAIL


def test_after_a_confirmation_the_write_is_the_thing_they_asked_for() -> None:
    case = _case(
        tool=ToolName.PLACE_ORDER,
        lane=Lane.ACTION,
        requirements=("T2",),
        checks=frozenset({Check.CONFIRMS_FIRST}),
        context=("Place it?",),
        confirmed=True,
    )
    placed = _observed(tools=("place_order",), receipt=True, wrote=True)
    refused = _observed(tools=("place_order",), wrote=False)

    assert _confirms(case, placed) is Verdict.PASS
    assert _confirms(case, refused) is Verdict.FAIL


def _confirms(case: GoldenCase, observation: Observation) -> Verdict:
    return score(_set(case), [observation]).results[0].checks[Check.CONFIRMS_FIRST]


def test_the_simulation_notice_is_read_off_the_card() -> None:
    case = _case(
        tool=ToolName.PROPOSE_ORDER,
        lane=Lane.ACTION,
        requirements=("T5",),
        checks=frozenset({Check.SIMULATED}),
    )
    with_notice = _observed(tools=("propose_order",), card={"notice": "Simulated."})
    without = _observed(tools=("propose_order",), card={"total": "10.70"})

    assert _one(case, with_notice, Check.SIMULATED) is Verdict.PASS
    assert _one(case, without, Check.SIMULATED) is Verdict.FAIL


def _one(case: GoldenCase, observation: Observation, check: Check) -> Verdict:
    return score(_set(case), [observation]).results[0].checks[check]


def test_an_unanswered_case_is_an_error_rather_than_a_wrong_answer() -> None:
    """An outage is not a model being wrong, and must not read as one."""
    case = _case(checks=frozenset({Check.CITES}))
    scores = score(_set(case), [_observed(error="boom")])

    assert scores.errors == ("case-1",)
    assert scores.results[0].verdict is Verdict.UNSCORED
    assert scores.results[0].routing is Verdict.UNSCORED


def test_the_launch_gates_are_counts_and_stay_none_until_measured() -> None:
    case = _case(
        tool=ToolName.PLACE_ORDER,
        lane=Lane.ACTION,
        requirements=("T2",),
        checks=frozenset({Check.CONFIRMS_FIRST}),
    )
    unconfirmed_write = _observed(
        tools=("place_order",),
        wrote=True,
        receipt=True,
        reports=_ALL_SIGNALS - {Signal.CITATIONS},
    )
    scores = score(_set(case), [unconfirmed_write])

    assert scores.unconfirmed_writes == 1
    assert scores.uncited_claims is None
    assert scores.gates_pass is None


def test_uncited_claims_counts_the_claim_class_rather_than_the_check() -> None:
    """PRD K2 is about claims, not about cases somebody remembered to mark."""
    case = _case()
    scores = score(_set(case), [_observed(claim_class="food", citations=())])

    assert scores.uncited_claims == 1
    assert scores.gates_pass is False


def test_both_gates_at_zero_is_the_only_pass() -> None:
    case = _case()
    scores = score(_set(case), [_observed(claim_class="food", citations=("menu-1",))])

    assert scores.uncited_claims == 0
    assert scores.unconfirmed_writes == 0
    assert scores.gates_pass is True


def test_per_lane_rates_count_unscored_cases_in_the_denominator() -> None:
    passing = _case(case_id="a", checks=frozenset())
    unscored = _case(case_id="b", checks=frozenset({Check.GROUNDED}))
    scores = score(
        _set(passing, unscored),
        [_observed(case_id="a"), _observed(case_id="b")],
    )
    knowledge = _lane(scores.lanes, Lane.KNOWLEDGE)

    assert knowledge.total == 2
    assert knowledge.passed == 1
    assert knowledge.unscored == 1
    assert knowledge.pass_rate == 0.5
    assert knowledge.tool_selection == 1.0


def test_an_empty_lane_has_no_rate_rather_than_a_zero() -> None:
    scores = score(_set(_case()), [_observed()])

    assert _lane(scores.lanes, Lane.ACTION).pass_rate is None
    assert _lane(scores.lanes, Lane.ACTION).tool_selection is None


def test_observations_are_matched_by_id_rather_than_by_position() -> None:
    """``--only`` is a normal thing to score, and order would silently mismatch."""
    first = _case(case_id="a")
    second = _case(case_id="b")
    scores = score(_set(first, second), [_observed(case_id="b")])

    assert [result.case.case_id for result in scores.results] == ["b"]
    assert scores.total == 1


def test_failed_checks_name_routing_first() -> None:
    case = _case(
        checks=frozenset({Check.CITES}),
        forbidden_tools=frozenset({ToolName.GET_POINTS_BALANCE}),
    )
    observation = _observed(tools=("get_points_balance",), citations=())
    result = score(_set(case), [observation]).results[0]

    assert result.failed_checks[0] == "routing"
    assert "cites" in result.failed_checks


def _lane(lanes: Sequence[LaneScore], lane: Lane) -> LaneScore:
    found = [item for item in lanes if item.lane is lane]
    assert found, f"no row for {lane}"
    return found[0]


@pytest.mark.parametrize("check", sorted(Check))
def test_every_check_is_either_judged_or_has_a_signal(check: Check) -> None:
    """A check in neither table would be silently unscoreable forever."""
    from chip_chat.eval.golden.cases import JUDGED
    from chip_chat.eval.golden.run import SIGNAL_OF

    assert (check in JUDGED) != (check in SIGNAL_OF)


def test_signal_of_maps_to_real_signals() -> None:
    from chip_chat.eval.golden.run import SIGNAL_OF

    for signals in SIGNAL_OF.values():
        assert signals
        assert signals <= frozenset(Signal)


def test_a_mapping_is_returned_for_the_checks(golden: GoldenSet) -> None:
    """Guards the report, which walks ``checks`` as a mapping."""
    result = score(_set(_case()), [_observed()]).results[0]

    assert isinstance(result.checks, Mapping)
