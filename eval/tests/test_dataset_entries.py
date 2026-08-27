"""What a promoted row carries, and the two empties it keeps apart.

#72's third acceptance criterion is *every entry carries its expected lane and
its PRD requirement id*, which is a claim about every row rather than about most
of them -- so these are written over the whole shipped set rather than over an
example of it.
"""

import json
from dataclasses import replace

from chip_chat.eval.dataset.entries import (
    GOLDEN_PREFIX,
    PHOTOS_PREFIX,
    InputKind,
    Origin,
    golden_entries,
    photo_entries,
)
from chip_chat.eval.golden.cases import JUDGED, GoldenSet
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.photos.labels import LabeledSet


def test_every_entry_carries_a_lane_and_a_requirement(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """#72's third acceptance criterion, over both halves of the dataset."""
    entries = golden_entries(golden) + photo_entries(labels)

    assert entries
    for entry in entries:
        assert isinstance(entry.expected_lane, Lane)
        assert entry.requirements, entry.entry_id


def test_a_golden_entry_keeps_its_tool_and_its_lane(golden: GoldenSet) -> None:
    """The pair the whole five-lane architecture is scored on."""
    by_id = {entry.entry_id: entry for entry in golden_entries(golden)}

    for case in golden:
        entry = by_id[f"{GOLDEN_PREFIX}{case.case_id}"]
        assert entry.expected_lane is case.lane
        assert entry.expected_tool == ("" if case.tool is None else case.tool.value)


def test_a_golden_entry_expecting_no_tool_is_still_scored_on_routing(
    golden: GoldenSet,
) -> None:
    """The first of the two empty tool cells.

    A turn that should reach for nothing has a right answer, and routing can be
    wrong about it. Reading its blank tool as "not scoreable" would drop the
    cases that keep the model honest about *not* calling something.
    """
    none_lane = [
        entry for entry in golden_entries(golden) if entry.expected_lane is Lane.NONE
    ]

    assert none_lane
    for entry in none_lane:
        assert entry.expected_tool == ""
        assert entry.scores_routing


def test_a_photograph_is_not_scored_on_routing(labels: LabeledSet) -> None:
    """The second one, and the reason the column exists.

    The photo set runs the vision lane directly. No model chose to call the
    tool, so a tool-selection number computed over these rows would be computed
    over turns in which no tool was selected.
    """
    entries = photo_entries(labels)

    assert entries
    for entry in entries:
        assert entry.expected_lane is Lane.VISION
        assert entry.expected_tool == ""
        assert not entry.scores_routing
        assert entry.input_kind is InputKind.IMAGE
        assert entry.origin is Origin.PHOTOS


def test_a_photograph_carries_the_ground_truth_a_scorer_needs(
    labels: LabeledSet,
) -> None:
    """The frame's slots survive the flattening, and stay one object."""
    by_id = {entry.entry_id: entry for entry in photo_entries(labels)}
    labeled = next(label for label in labels if label.slots)

    entry = by_id[f"{PHOTOS_PREFIX}{labeled.photo_id}"]

    assert entry.frame is not None
    truth = json.loads(str(entry.row()["frame_truth"]))
    assert truth["slots"] == {
        slot.value: sorted(terms) for slot, terms in labeled.slots.items()
    }
    assert truth["meals_visible"] == labeled.meals_visible


def test_a_golden_entry_has_no_frame_truth(golden: GoldenSet) -> None:
    """A blank cell rather than a fabricated one: a question is not a frame."""
    for entry in golden_entries(golden):
        assert entry.frame is None
        assert entry.row()["frame_truth"] == ""


def test_the_allergen_category_rides_on_the_row(golden: GoldenSet) -> None:
    """#75 reports it apart, so an experiment has to be able to group on it.

    Carried as a column rather than derived from ``requirements`` at read time,
    because the requirement ids do not settle it: K3 covers halal *and*
    cross-contact, K5 the two allergen ones, and *"what's vegetarian here"* is a
    K4 case and a dietary question.
    """
    entries = golden_entries(golden)
    dietary = [entry for entry in entries if entry.dietary]

    assert len(dietary) >= 4
    assert all(entry.row()["dietary"] is entry.dietary for entry in entries)
    assert {entry.dietary for entry in entries} == {True, False}


def test_a_photograph_is_never_in_the_allergen_category(labels: LabeledSet) -> None:
    """A frame is scored in ``eval/photos``; there is no response to hold to K2."""
    assert all(not entry.dietary for entry in photo_entries(labels))


def test_the_judged_checks_are_carried_apart(golden: GoldenSet) -> None:
    """An online eval attaches a judge to exactly these and to nothing else."""
    for entry in golden_entries(golden):
        judged = set(entry.judged_checks)
        assert judged <= set(entry.checks)
        assert judged == {check for check in entry.checks if check in JUDGED}


def test_a_digest_is_the_same_for_the_same_entry(golden: GoldenSet) -> None:
    """The unit the no-mutation rule is enforced in, and it has to be stable.

    A digest that moved between two builds of the same manifest would refuse
    every second publish, which is a rule nobody would keep for long.
    """
    assert [entry.digest for entry in golden_entries(golden)] == [
        entry.digest for entry in golden_entries(golden)
    ]


def test_a_digest_moves_when_any_field_does(golden: GoldenSet) -> None:
    """And the other direction, which is the one the rule needs."""
    entry = golden_entries(golden)[0]

    assert replace(entry, input="something else").digest != entry.digest
    assert replace(entry, requirements=("K9",)).digest != entry.digest
    assert replace(entry, why="a different reason").digest != entry.digest
    assert replace(entry, dietary=not entry.dietary).digest != entry.digest


def test_every_row_is_flat_scalars(golden: GoldenSet, labels: LabeledSet) -> None:
    """The far side of the seam is a table.

    A cell holding a list is a cell every consumer has to guess at, so the
    composite fields are JSON strings and this is the check that keeps them
    that way.
    """
    for entry in golden_entries(golden) + photo_entries(labels):
        for column, value in entry.row().items():
            assert isinstance(value, str | int | bool), (entry.entry_id, column)


def test_the_composite_columns_are_json(golden: GoldenSet) -> None:
    """And the guess a consumer would have to make is made once, here."""
    entry = next(entry for entry in golden_entries(golden) if entry.requirements)
    row = entry.row()

    assert json.loads(str(row["requirements"])) == list(entry.requirements)
    assert json.loads(str(row["checks"])) == list(entry.checks)
    assert json.loads(str(row["menu_terms"])) == list(entry.menu_terms)
