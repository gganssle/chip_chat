"""The four shapes, one at a time, and the order they are asked in.

Each test drives one outcome from a tree built by hand, because what is under
test is the classifier rather than a deployment -- a span tree of invented spans
measures the invention, which is exactly right here and would be a fraud in a
baseline.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from chip_chat.eval.golden.lanes import Lane, lane_of
from chip_chat.eval.trajectory.expectations import Expectation
from chip_chat.eval.trajectory.shapes import FAILURE_SHAPES, Judgement, Shape, classify
from chip_chat.eval.trajectory.testing import turn_spans
from chip_chat.eval.trajectory.trees import read_trajectory
from chip_chat.otel.schema import ToolName

_MESSAGE = "how many calories are in a chicken bowl"


def test_the_expected_tool_alone_is_correct() -> None:
    """The shape everything else is defined against."""
    judgement = _classify(
        ToolName.SEARCH_MENU_KNOWLEDGE, [(ToolName.SEARCH_MENU_KNOWLEDGE, _query())]
    )

    assert judgement.shape is Shape.CORRECT
    assert judgement.selected
    assert judgement.clean
    assert judgement.detail == ""


def test_a_turn_that_called_nothing_is_no_tool() -> None:
    """The quiet killer for groundedness: fluent prose attached to nothing."""
    judgement = _classify(ToolName.SEARCH_MENU_KNOWLEDGE, [])

    assert judgement.shape is Shape.NO_TOOL
    assert not judgement.selected


def test_the_other_lane_is_wrong_lane() -> None:
    """A menu question answered from the account lane."""
    judgement = _classify(
        ToolName.SEARCH_MENU_KNOWLEDGE,
        [(ToolName.ASK_ACCOUNT_QUESTION, {"question": _MESSAGE})],
    )

    assert judgement.shape is Shape.WRONG_LANE
    assert "account" in judgement.detail


def test_a_forbidden_tool_is_wrong_lane_even_beside_the_right_one() -> None:
    """A row that names the wrong answer has said precisely what wrong means for it.

    This is the precedence that matters: the expected tool *was* called, so
    nothing else in the classifier would have objected.
    """
    judgement = _classify(
        ToolName.SEARCH_MENU_KNOWLEDGE,
        [
            (ToolName.SEARCH_MENU_KNOWLEDGE, _query()),
            (ToolName.GET_POINTS_BALANCE, {}),
        ],
        forbidden=(ToolName.GET_POINTS_BALANCE,),
    )

    assert judgement.shape is Shape.WRONG_LANE
    assert not judgement.selected


def test_an_unsanctioned_second_call_is_extra_tools() -> None:
    """Reached the lane, and paid for a call the turn did not need."""
    judgement = _classify(
        ToolName.SEARCH_MENU_KNOWLEDGE,
        [
            (ToolName.SEARCH_MENU_KNOWLEDGE, _query()),
            (ToolName.GET_RECOMMENDATIONS, {}),
        ],
    )

    assert judgement.shape is Shape.EXTRA_TOOLS
    assert judgement.extras == (ToolName.GET_RECOMMENDATIONS,)
    assert judgement.selected
    assert not judgement.clean


def test_the_same_tool_twice_is_extra_tools() -> None:
    """*Three when one would do* counts calls, not distinct tools."""
    judgement = _classify(
        ToolName.SEARCH_MENU_KNOWLEDGE,
        [
            (ToolName.SEARCH_MENU_KNOWLEDGE, _query()),
            (ToolName.SEARCH_MENU_KNOWLEDGE, _query()),
        ],
    )

    assert judgement.shape is Shape.EXTRA_TOOLS


def test_a_sanctioned_chain_is_correct() -> None:
    """*Get me my usual but add guac* is one request, not two.

    Scoring the draft as waste would mark the correct trajectory wrong, which is
    the same argument the golden set makes for reach-and-avoid routing.
    """
    judgement = _classify(
        ToolName.GET_USUAL_ORDER,
        [(ToolName.GET_USUAL_ORDER, {}), (ToolName.PROPOSE_ORDER, {"items": []})],
    )

    assert judgement.shape is Shape.CORRECT


def test_a_query_that_shares_nothing_with_the_ask_is_wrong_query() -> None:
    """Right lane, wrong question -- the shape that only two tools can show."""
    judgement = _classify(
        ToolName.SEARCH_MENU_KNOWLEDGE,
        [(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "store opening hours"})],
    )

    assert judgement.shape is Shape.WRONG_QUERY
    assert judgement.selected


def test_a_call_with_no_query_at_all_is_wrong_query() -> None:
    """The lane was entered and it was asked nothing."""
    judgement = _classify(
        ToolName.SEARCH_MENU_KNOWLEDGE, [(ToolName.SEARCH_MENU_KNOWLEDGE, {})]
    )

    assert judgement.shape is Shape.WRONG_QUERY
    assert "no query" in judgement.detail


def test_a_menu_term_the_row_leans_on_counts_as_the_ask_surviving() -> None:
    """A query phrased in the catalogue's words rather than the visitor's."""
    judgement = _classify(
        ToolName.SEARCH_MENU_KNOWLEDGE,
        [(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "guacamole allergens"})],
        message="does it have anything i can't eat",
        terms=("guacamole",),
    )

    assert judgement.shape is Shape.CORRECT


def test_a_tool_that_takes_no_query_is_never_wrong_query() -> None:
    """``get_points_balance`` takes nothing, so there is no ask to have dropped."""
    judgement = _classify(
        ToolName.GET_POINTS_BALANCE, [(ToolName.GET_POINTS_BALANCE, {})]
    )

    assert judgement.shape is Shape.CORRECT
    assert not judgement.expectation.scores_query


def test_a_turn_owing_nothing_is_correct_when_it_calls_nothing() -> None:
    """On these rows, calling nothing is the trajectory rather than the absence of one."""
    judgement = _classify(None, [])

    assert judgement.shape is Shape.CORRECT


def test_a_turn_owing_nothing_that_calls_something_is_wrong_lane() -> None:
    """``no_tool`` is defined against an expected tool, so it cannot apply here."""
    judgement = _classify(None, [(ToolName.ASK_ACCOUNT_QUESTION, {"question": "q"})])

    assert judgement.shape is Shape.WRONG_LANE
    assert "should have called nothing" in judgement.detail


def test_an_unreadable_trace_is_unscored_and_not_a_failure() -> None:
    """A split trace is evidence about propagation, not about a model."""
    expectation = _expect(ToolName.SEARCH_MENU_KNOWLEDGE)
    trajectory = read_trajectory(
        expectation.entry_id,
        turn_spans([(ToolName.SEARCH_MENU_KNOWLEDGE, _query())], split=True),
    )

    judgement = classify(expectation, trajectory)

    assert judgement.shape is Shape.UNSCORED
    assert judgement.shape not in FAILURE_SHAPES
    assert not judgement.scored
    assert not judgement.selected


def _query() -> Mapping[str, Any]:
    """The plainest right query there is: the visitor's own words."""
    return {"query": _MESSAGE}


def _expect(
    tool: ToolName | None,
    *,
    forbidden: Sequence[ToolName] = (),
    message: str = _MESSAGE,
    terms: Sequence[str] = (),
) -> Expectation:
    """One row, in the shape the dataset produces."""
    return Expectation(
        entry_id="golden/row",
        lane=lane_of(tool) if tool is not None else Lane.NONE,
        tool=tool,
        forbidden=frozenset(forbidden),
        message=message,
        menu_terms=tuple(terms),
    )


def _classify(
    tool: ToolName | None,
    calls: Sequence[tuple[ToolName, Mapping[str, Any]]],
    *,
    forbidden: Sequence[ToolName] = (),
    message: str = _MESSAGE,
    terms: Sequence[str] = (),
) -> Judgement:
    """Build a row and a tree, and give the turn its shape."""
    expectation = _expect(tool, forbidden=forbidden, message=message, terms=terms)
    trajectory = read_trajectory(expectation.entry_id, turn_spans(calls))
    return classify(expectation, trajectory)
