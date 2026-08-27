"""The slice, recorded: one connected trace per turn, and two readings that agree.

This is the only module here that runs the real loop. Three things are worth
asserting about it, and the middle one is the reason #74 reads spans at all.

It is emphatically **not** a test that the agent routes well: routing is handed
to the oracle, so every lane it can reach is reached by construction. What
remains is the wiring, which is what a ceiling measures.
"""

from chip_chat.eval.dataset.build import Dataset
from chip_chat.eval.dataset.entries import GOLDEN_PREFIX
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.golden.testing import ceiling as golden_ceiling
from chip_chat.eval.trajectory.expectations import Expectation
from chip_chat.eval.trajectory.scoring import score
from chip_chat.eval.trajectory.shapes import Shape
from chip_chat.eval.trajectory.testing import ceiling
from chip_chat.otel.schema import ToolName


def test_every_turn_arrives_as_one_trace(
    golden: GoldenSet, shipped: Dataset, rows: tuple[Expectation, ...]
) -> None:
    """The #103 assertion, in the only place this repository can make it in-process.

    One process, so this does not discharge the dependency -- a turn that never
    crossed a boundary cannot show that context survives one, and
    ``make trace-boundary`` is that check. What it does hold is the reader: no
    turn the slice emits splits, so a split in a real run is a fact about the
    deployment rather than about this module.
    """
    scores = score(rows, ceiling(golden, shipped))

    assert scores.split_traces == 0
    for judgement in scores.unreadable():
        assert "trace" not in judgement.detail, judgement.expectation.entry_id


def test_the_spans_and_the_loop_agree_about_what_was_called(
    golden: GoldenSet, shipped: Dataset
) -> None:
    """Two readings of one turn, and they have to be the same reading.

    ``chip_chat.eval.golden.slice`` reads the tools off the conversation the
    loop sent; this package reads them off the ``tool.<tool_name>`` spans. If
    those two ever disagree, one of the numbers in ``eval/`` is about something
    other than the turn -- and the span one is the one every dashboard, monitor
    and online eval will be reading.
    """
    by_case = {
        observation.case_id: observation.tools for observation in golden_ceiling(golden)
    }

    for trajectory in ceiling(golden, shipped):
        case_id = trajectory.entry_id.removeprefix(GOLDEN_PREFIX)
        if trajectory.error is not None:
            continue
        assert tuple(tool.value for tool in trajectory.tools) == by_case[case_id]


def test_the_knowledge_lane_is_at_its_ceiling(
    golden: GoldenSet, shipped: Dataset, rows: tuple[Expectation, ...]
) -> None:
    """Every knowledge tool is registered, so perfect routing gets a perfect lane."""
    scores = score(rows, ceiling(golden, shipped))
    knowledge = next(lane for lane in scores.lanes if lane.lane is Lane.KNOWLEDGE)

    assert knowledge.tool_selection == 1.0
    assert knowledge.shapes[Shape.CORRECT] == knowledge.total


def test_a_tool_the_slice_does_not_register_comes_back_as_no_tool(
    golden: GoldenSet, shipped: Dataset, rows: tuple[Expectation, ...]
) -> None:
    """The shape that is a wiring fact rather than a model choice.

    ``cancel_order`` is not among the tools the week-one slice offers, so the
    oracle -- which consults the registration precisely so it cannot reach past
    it -- calls nothing. A span tree cannot tell that apart from a model that
    chose not to, which is why ``eval/trajectory/BASELINE.md`` says it in prose.
    """
    scores = score(rows, ceiling(golden, shipped))
    cancel = [
        judgement
        for judgement in scores.judgements
        if judgement.expectation.tool is ToolName.CANCEL_ORDER
    ]

    assert cancel
    for judgement in cancel:
        assert judgement.shape is Shape.NO_TOOL
