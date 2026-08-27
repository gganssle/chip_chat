"""What a dataset row owes a trajectory, and the two tables behind the answer.

Written over the shipped dataset rather than over an example of it, for the
reason ``test_dataset_entries.py`` gives: #74's claims are claims about every
row, and a fixture that stood in for the set would let the real one drift.
"""

from dataclasses import replace

import pytest

from chip_chat.eval.dataset.build import Dataset
from chip_chat.eval.dataset.entries import GOLDEN_PREFIX, DatasetEntry, InputKind, Origin
from chip_chat.eval.golden.lanes import Lane, lane_of
from chip_chat.eval.trajectory.expectations import (
    QUERY_ARGUMENT,
    SANCTIONED,
    Expectation,
    ExpectationError,
    expectations,
)
from chip_chat.otel.schema import ToolName


def test_sanctioned_is_total_over_the_eleven_tools() -> None:
    """A tool with no entry would sanction nothing by accident rather than by decision.

    The same argument ``LANE_OF`` is held to: a twelfth tool added without a row
    here would make every turn expecting it start scoring ``extra_tools``, which
    is a silent change to the metric rather than an obvious one.
    """
    assert set(SANCTIONED) == set(ToolName)


def test_the_sanctioned_chains_run_one_way_only() -> None:
    """A read may be followed into a draft; the action lane may not reach back."""
    assert ToolName.PROPOSE_ORDER in SANCTIONED[ToolName.GET_USUAL_ORDER]
    assert SANCTIONED[ToolName.PROPOSE_ORDER] == frozenset()


def test_only_two_tools_take_the_ask_as_an_argument() -> None:
    """What makes *right lane, wrong query* observable at all.

    Every other tool takes an id or a structure, so a wrong one is a broken call
    rather than a badly-phrased question.
    """
    assert set(QUERY_ARGUMENT) == {
        ToolName.SEARCH_MENU_KNOWLEDGE,
        ToolName.ASK_ACCOUNT_QUESTION,
    }


def test_every_routing_row_becomes_an_expectation(shipped: Dataset) -> None:
    """The register is the dataset's routing rows, and all of them."""
    rows = expectations(shipped)

    assert len(rows) == sum(1 for entry in shipped.entries if entry.scores_routing)
    for row in rows:
        assert row.entry_id.startswith(GOLDEN_PREFIX)
        assert lane_of(row.tool) is row.lane


def test_a_row_expecting_no_tool_is_still_in_the_register(
    rows: tuple[Expectation, ...],
) -> None:
    """*Call nothing* is an answer routing can be wrong about.

    Dropping these would leave the eval unable to see a turn that reached into
    the account lane on a question about somebody else's order history.
    """
    none_lane = [row for row in rows if row.lane is Lane.NONE]

    assert none_lane
    for row in none_lane:
        assert row.tool is None
        assert row.sanctioned == frozenset()


def test_a_forbidden_tool_is_not_sanctioned_however_the_table_reads() -> None:
    """A row that names a tool as the wrong answer has said more than the table does."""
    row = Expectation(
        entry_id="golden/v2-photo-routing",
        lane=Lane.VISION,
        tool=ToolName.MATCH_MEAL_FROM_PHOTO,
        forbidden=frozenset({ToolName.PROPOSE_ORDER}),
    )

    assert ToolName.PROPOSE_ORDER in SANCTIONED[ToolName.MATCH_MEAL_FROM_PHOTO]
    assert row.sanctioned == frozenset()


def test_a_row_naming_a_tool_outside_the_eleven_is_refused(shipped: Dataset) -> None:
    """Refused while the register is built, never while a trajectory is scored."""
    broken = replace(shipped, entries=(_entry(expected_tool="order_a_pizza"),))

    with pytest.raises(ExpectationError, match="not one of the tools"):
        expectations(broken)


def test_a_row_whose_lane_does_not_hold_its_tool_is_refused(shipped: Dataset) -> None:
    """A per-lane rate that could drift from what it was computed over is worthless."""
    broken = replace(
        shipped,
        entries=(
            _entry(
                expected_tool=ToolName.SEARCH_MENU_KNOWLEDGE.value,
                expected_lane=Lane.ACCOUNT,
            ),
        ),
    )

    with pytest.raises(ExpectationError, match="account"):
        expectations(broken)


def _entry(*, expected_tool: str, expected_lane: Lane = Lane.KNOWLEDGE) -> DatasetEntry:
    """One golden-shaped row, for driving a refusal."""
    return DatasetEntry(
        entry_id="golden/broken",
        origin=Origin.GOLDEN,
        input="what's in a bowl",
        input_kind=InputKind.MESSAGE,
        expected_lane=expected_lane,
        expected_tool=expected_tool,
        requirements=("K1",),
    )
