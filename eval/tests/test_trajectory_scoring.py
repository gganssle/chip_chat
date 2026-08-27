"""The arithmetic: per lane, in aggregate, and what it refuses to average.

Driven from a scripted source, so every number in these tests was computed on
paper first. The shipped dataset is used for the register, because a per-lane
rate over invented lanes would be a test of the test.
"""

from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.trajectory.expectations import Expectation
from chip_chat.eval.trajectory.run import run_trajectories
from chip_chat.eval.trajectory.scoring import (
    TOOL_SELECTION_TARGET,
    TrajectoryScores,
    score,
)
from chip_chat.eval.trajectory.shapes import Shape
from chip_chat.eval.trajectory.testing import ScriptedSource, turn_spans
from chip_chat.eval.trajectory.trees import TraceSpan
from chip_chat.otel.schema import ToolName


def test_a_perfect_run_meets_the_target(rows: tuple[Expectation, ...]) -> None:
    """Every row routed, so the headline is 100% and the gap is nothing."""
    scores = _scored(rows, _perfect(rows))

    assert scores.total == len(rows)
    assert scores.scored == len(rows)
    assert scores.tool_selection == 1.0
    assert scores.meets_target
    assert scores.gap == 0.0
    assert scores.shapes[Shape.CORRECT] == len(rows)


def test_one_wrong_lane_lands_in_its_own_lane_and_in_the_aggregate(
    rows: tuple[Expectation, ...],
) -> None:
    """The breakdown and the total are computed from the same rows.

    #74 asks for the lanes *because* an aggregate can hide one of them, so the
    two cannot be allowed to come from different arithmetic.
    """
    knowledge = next(row for row in rows if row.lane is Lane.KNOWLEDGE)
    script = _perfect(rows)
    script[knowledge.entry_id] = turn_spans([(ToolName.GET_POINTS_BALANCE, {})])

    scores = _scored(rows, script)
    lane = next(lane for lane in scores.lanes if lane.lane is Lane.KNOWLEDGE)

    assert scores.shapes[Shape.WRONG_LANE] == 1
    assert lane.shapes[Shape.WRONG_LANE] == 1
    assert lane.tool_selection == (lane.total - 1) / lane.total
    assert scores.tool_selection == (len(rows) - 1) / len(rows)
    # One miss in thirty-four is still above the bar, which is worth seeing
    # once: on a set this size the target has room for exactly one wrong lane.
    assert scores.meets_target


def test_an_unreadable_trace_is_in_no_numerator_and_no_denominator(
    rows: tuple[Expectation, ...],
) -> None:
    """A turn nobody can read is not a turn that routed badly.

    The rate is over the rows that *could* be scored, and the count of the rest
    is carried beside it rather than folded into it.
    """
    split = next(row for row in rows if row.tool is not None)
    assert split.tool is not None
    script = _perfect(rows)
    script[split.entry_id] = turn_spans([(split.tool, {})], split=True)

    scores = _scored(rows, script)

    assert scores.unscored == 1
    assert scores.split_traces == 1
    assert scores.scored == len(rows) - 1
    assert scores.tool_selection == 1.0


def test_an_empty_lane_has_no_rate_rather_than_a_nought() -> None:
    """A nought in that cell reads as a lane that failed everything."""
    scores = score((), ())

    assert scores.tool_selection is None
    assert scores.meets_target is None
    for lane in scores.lanes:
        assert lane.tool_selection is None
        assert lane.meets_target is None


def test_extra_calls_move_the_clean_rate_and_not_the_headline(
    rows: tuple[Expectation, ...],
) -> None:
    """The two rates, and the gap between them.

    Lane selection is the metric PRD section 05 sets a target on, and it is the
    same reach-and-avoid rule the golden set applies. The stricter reading is
    what catches a turn that got there expensively.
    """
    row = next(r for r in rows if r.tool is ToolName.SEARCH_MENU_KNOWLEDGE)
    script = _perfect(rows)
    script[row.entry_id] = turn_spans(
        [
            (ToolName.SEARCH_MENU_KNOWLEDGE, {"query": row.message}),
            (ToolName.GET_RECOMMENDATIONS, {}),
        ]
    )

    scores = _scored(rows, script)

    assert scores.tool_selection == 1.0
    assert scores.clean == (len(rows) - 1) / len(rows)
    assert scores.shapes[Shape.EXTRA_TOOLS] == 1


def test_failures_come_back_in_the_ticket_s_order(
    rows: tuple[Expectation, ...],
) -> None:
    """Wrong lane, no tool, extra tools, wrong query -- #74's own order."""
    knowledge = [row for row in rows if row.lane is Lane.KNOWLEDGE][:2]
    script = _perfect(rows)
    script[knowledge[0].entry_id] = turn_spans([])
    script[knowledge[1].entry_id] = turn_spans(
        [(ToolName.SEARCH_MENU_KNOWLEDGE, {"query": "unrelated store hours"})]
    )

    shapes = [j.shape for j in _scored(rows, script).failures()]

    assert shapes == [Shape.NO_TOOL, Shape.WRONG_QUERY]


def test_the_target_is_the_prd_s(rows: tuple[Expectation, ...]) -> None:
    """One constant, imported rather than restated. PRD section 05: ≥ 95%."""
    assert TOOL_SELECTION_TARGET == 0.95


def _perfect(rows: tuple[Expectation, ...]) -> dict[str, tuple[TraceSpan, ...]]:
    """A script in which every row routes correctly and cleanly."""
    script: dict[str, tuple[TraceSpan, ...]] = {}
    for row in rows:
        calls: list[tuple[ToolName, dict[str, str]]] = (
            [] if row.tool is None else [(row.tool, _arguments(row))]
        )
        for companion in sorted(row.sanctioned):
            calls.append((companion, {}))
        script[row.entry_id] = turn_spans(calls)
    return script


def _arguments(row: Expectation) -> dict[str, str]:
    """The visitor's own words, where the tool takes them."""
    argument = row.query_argument
    return {} if argument is None else {argument: row.message}


def _scored(
    rows: tuple[Expectation, ...], script: dict[str, tuple[TraceSpan, ...]]
) -> TrajectoryScores:
    """Run a script and score it."""
    source = ScriptedSource(script=script)
    return score(rows, run_trajectories(rows, source))
