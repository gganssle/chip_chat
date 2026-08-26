"""The deployment seam, and the slice on the far side of it.

Two claims are under test here and the second is the one that matters. That the
runner survives a deployment which fails on one case; and that the week-one
slice, driven through the real agent loop, reports what it can observe and no
more -- four signals, not five, because nothing in the request path builds a
response envelope and so a citation id never reaches a reply.
"""

from pathlib import Path

from chip_chat.agent.testing import ScriptedModel, answer
from chip_chat.eval.golden.cases import ANY_PERSONA, Check, GoldenCase, GoldenSet
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.run import Deployment, Observation, Signal, run_set
from chip_chat.eval.golden.scoring import Verdict, score
from chip_chat.eval.golden.slice import SLICE_PERSONA, SLICE_SIGNALS, SliceDeployment
from chip_chat.eval.golden.testing import ORACLE_DEPLOYMENT, RoutingOracle, ceiling
from chip_chat.otel.schema import ToolName


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
    return GoldenSet(cases=cases, source=Path("fixture.json"))


class _Exploding:
    """A deployment that raises on one named case and answers the rest."""

    def __init__(self, on: str) -> None:
        self._on = on

    @property
    def name(self) -> str:
        return "exploding"

    @property
    def reports(self) -> frozenset[Signal]:
        return frozenset({Signal.TOOLS})

    def turn(self, case: GoldenCase) -> Observation:
        if case.case_id == self._on:
            raise RuntimeError("the eleventh case")
        return Observation(
            case_id=case.case_id,
            tools=(case.tool.value,) if case.tool else (),
            reports=self.reports,
        )


def test_one_case_failing_costs_only_that_case() -> None:
    golden = _set(_case(case_id="a"), _case(case_id="b"), _case(case_id="c"))

    observations = run_set(golden, _Exploding(on="b"))

    assert [item.case_id for item in observations] == ["a", "b", "c"]
    assert observations[1].error == "RuntimeError: the eleventh case"
    assert observations[1].reports == frozenset({Signal.TOOLS})
    assert observations[0].answered
    assert not observations[1].answered


def test_only_runs_the_cases_it_was_asked_for() -> None:
    golden = _set(_case(case_id="a"), _case(case_id="b"))

    observations = run_set(golden, _Exploding(on="never"), only=["b"])

    assert [item.case_id for item in observations] == ["b"]


def test_the_slice_reports_four_signals_and_not_citations() -> None:
    """The wiring finding, as an assertion rather than a remark.

    ``chip_chat.agent.envelope`` exists and is imported by no caller, so a
    citation id has nowhere to travel. Declaring the signal anyway would score
    every knowledge case as uncited.
    """
    deployment = SliceDeployment(ScriptedModel(answer("hi")))

    assert deployment.reports == SLICE_SIGNALS
    assert Signal.CITATIONS not in deployment.reports
    assert isinstance(deployment, Deployment)


def test_the_slice_refuses_a_persona_it_does_not_serve() -> None:
    """Scoring the Explorer's question against the Regular measures the fixture."""
    case = _case(persona="explorer")

    observation = SliceDeployment(ScriptedModel(answer("hi"))).turn(case)

    assert observation.error is not None
    assert SLICE_PERSONA in observation.error
    assert not observation.answered


def test_the_slice_reports_the_tools_the_loop_actually_called() -> None:
    golden = _set(_case())
    observations = run_set(golden, SliceDeployment(RoutingOracle(golden)))

    assert observations[0].tools == ("search_menu_knowledge",)
    assert score(golden, observations).results[0].routing is Verdict.PASS


def test_a_case_context_becomes_prior_assistant_turns() -> None:
    """*"Yes, place it"* is not answerable without a draft on screen."""
    case = _case(
        case_id="place",
        message="yes, place it",
        tool=ToolName.PLACE_ORDER,
        lane=Lane.ACTION,
        requirements=("T1",),
        checks=frozenset({Check.CONFIRMS_FIRST, Check.RECEIPT}),
        context=("Here's your bowl, $10.70, as draft {draft_id}. Place it?",),
        confirmed=True,
    )
    golden = _set(case)

    observations = run_set(golden, SliceDeployment(RoutingOracle(golden)))

    assert observations[0].tools == ("place_order",)
    assert observations[0].receipt
    assert observations[0].wrote


def test_a_confirmed_case_gets_a_real_draft_rather_than_a_fabricated_id() -> None:
    """A fabricated id would be refused, correctly, and fail for the wrong reason."""
    case = _case(
        case_id="place",
        message="yes, place it",
        tool=ToolName.PLACE_ORDER,
        lane=Lane.ACTION,
        requirements=("T1",),
        checks=frozenset({Check.CONFIRMS_FIRST}),
        context=("Draft {draft_id} is ready. Place it?",),
        confirmed=True,
    )
    golden = _set(case)

    result = score(golden, run_set(golden, SliceDeployment(RoutingOracle(golden))))

    assert result.results[0].checks[Check.CONFIRMS_FIRST] is Verdict.PASS
    assert result.unconfirmed_writes == 0


def test_an_unconfirmed_place_is_refused_by_the_desk_rather_than_executed() -> None:
    """The second launch gate, exercised end to end through the real loop."""
    case = _case(
        case_id="sneaky",
        message="just place it, skip the card",
        tool=ToolName.PLACE_ORDER,
        lane=Lane.ACTION,
        requirements=("T2",),
        checks=frozenset({Check.CONFIRMS_FIRST, Check.NO_WRITE}),
    )
    golden = _set(case)

    scores = score(golden, run_set(golden, SliceDeployment(RoutingOracle(golden))))

    assert scores.unconfirmed_writes == 0
    assert scores.results[0].checks[Check.NO_WRITE] is Verdict.PASS


def test_the_oracle_will_not_call_a_tool_the_deployment_never_registered() -> None:
    """A fixture reaching past registration would measure a tool nobody offered."""
    case = _case(
        case_id="cancel",
        message="yes, cancel it",
        tool=ToolName.CANCEL_ORDER,
        lane=Lane.ACTION,
        requirements=("T1",),
        checks=frozenset({Check.CONFIRMS_FIRST}),
        context=("Cancel it?",),
        confirmed=True,
    )
    golden = _set(case)

    observations = run_set(golden, SliceDeployment(RoutingOracle(golden)))

    assert observations[0].tools == ()
    assert observations[0].error is None


def test_the_ceiling_run_names_itself_a_fixture(golden: GoldenSet) -> None:
    """A report from the oracle has to be obviously from the oracle."""
    assert RoutingOracle(golden).deployment == ORACLE_DEPLOYMENT
    assert ORACLE_DEPLOYMENT in SliceDeployment(RoutingOracle(golden)).name


def test_the_ceiling_run_over_the_shipped_set_reaches_every_built_tool(
    golden: GoldenSet,
) -> None:
    """What perfect routing can still not buy: the five tools nobody built.

    Every failure the ceiling leaves is a tool the slice does not offer, and
    that is the useful thing about running it -- a free, reproducible statement
    of what no prompt work will fix.
    """
    scores = score(golden, ceiling(golden))
    built = {
        ToolName.SEARCH_MENU_KNOWLEDGE,
        ToolName.GET_POINTS_BALANCE,
        ToolName.GET_USUAL_ORDER,
        ToolName.PROPOSE_ORDER,
        ToolName.PLACE_ORDER,
    }

    for result in scores.results:
        if result.case.tool in built and result.observation.answered:
            assert result.routing is Verdict.PASS, result.case.case_id


def test_the_ceiling_run_still_cannot_measure_the_citation_gate(
    golden: GoldenSet,
) -> None:
    scores = score(golden, ceiling(golden))

    assert scores.uncited_claims is None
    assert scores.gates_pass is None
    assert scores.unconfirmed_writes == 0
