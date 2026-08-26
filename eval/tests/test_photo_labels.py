"""What a label is allowed to say, and the four things it is not.

The manifest is the half of the measurement no model produced, so every refusal
here is a way the set could quietly stop being ground truth: a term the menu
does not publish, a per-meal label on a frame with two meals in it, a required
slot that is neither read nor admitted to be unreadable, and a slot claimed as
both.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from chip_chat.catalog.records import Slot
from chip_chat.eval.photos.labels import Capture, Condition, LabeledSet, LabelError
from chip_chat.eval.photos.testing import synthetic_set
from chip_chat.vision.testing import DEFAULT_TERMS, generated_vocabulary


def _manifest(tmp_path: Path, *entries: dict[str, Any]) -> Path:
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"photos": list(entries)}), encoding="utf-8")
    return path


def _entry(**overrides: Any) -> dict[str, Any]:
    """One well-formed single-meal entry, before a test breaks it."""
    entry: dict[str, Any] = {
        "id": "a-bowl",
        "image": "images/a-bowl.jpg",
        "capture": {"photographer": "someone", "license": "CC0-1.0"},
        "conditions": ["clean"],
        "is_chipotle_style": True,
        "meals_visible": 1,
        "slots": {
            "vessel": "bowl",
            "protein": "chicken",
            "rice": "white_rice",
            "beans": "black_beans",
            "toppings": ["cheese"],
        },
        "unreadable": [],
    }
    entry.update(overrides)
    return entry


# --- what a well-formed manifest produces -----------------------------------


def test_a_label_reads_back_as_the_pairs_the_scorer_counts(tmp_path: Path) -> None:
    labels = LabeledSet.load(_manifest(tmp_path, _entry()))
    (label,) = labels.photos

    assert label.pairs() == frozenset(
        {
            (Slot.VESSEL, "bowl"),
            (Slot.PROTEIN, "chicken"),
            (Slot.RICE, "white_rice"),
            (Slot.BEANS, "black_beans"),
            (Slot.TOPPINGS, "cheese"),
        }
    )
    assert label.orderable
    assert label.capture == Capture(photographer="someone", license="CC0-1.0")


def test_a_multi_valued_slot_keeps_every_term(tmp_path: Path) -> None:
    entry = _entry(
        slots={
            **_entry()["slots"],
            "toppings": ["cheese", "guacamole"],
        }
    )
    (label,) = LabeledSet.load(_manifest(tmp_path, entry)).photos

    assert (Slot.TOPPINGS, "cheese") in label.pairs()
    assert (Slot.TOPPINGS, "guacamole") in label.pairs()


def test_an_unreadable_slot_is_not_a_labeled_pair(tmp_path: Path) -> None:
    """The wrapped burrito. There is rice in it and the photograph does not say."""
    slots = dict(_entry()["slots"])
    del slots["rice"]
    entry = _entry(conditions=["contents_hidden"], slots=slots, unreadable=["rice"])
    (label,) = LabeledSet.load(_manifest(tmp_path, entry)).photos

    assert Slot.RICE not in {slot for slot, _ in label.pairs()}
    assert label.unreadable_required() == (Slot.RICE,)


# --- the refusals -----------------------------------------------------------


def test_a_term_the_catalogue_does_not_publish_is_refused(tmp_path: Path) -> None:
    """D3's property, applied to the truth: a label may not invent a food either."""
    entry = _entry(slots={**_entry()["slots"], "protein": "carnitas"})
    labels = LabeledSet.load(_manifest(tmp_path, entry))

    with pytest.raises(LabelError, match="publishes no protein term"):
        labels.against(generated_vocabulary(DEFAULT_TERMS))


def test_a_multi_meal_frame_may_not_carry_per_meal_labels(tmp_path: Path) -> None:
    """``docs/decisions/multi-meal-photos.md``: the slots describe the picture.

    A label filling them would assert a meal that may be on nobody's tray -- a
    chicken bowl and a steak burrito in frame make a plausible, orderable, and
    entirely fictional chicken burrito.
    """
    entry = _entry(conditions=["multi_meal"], meals_visible=3)

    with pytest.raises(LabelError, match="scored on whether it was detected"):
        LabeledSet.load(_manifest(tmp_path, entry))


def test_a_non_chipotle_frame_may_not_carry_per_meal_labels(tmp_path: Path) -> None:
    entry = _entry(conditions=["not_chipotle"], is_chipotle_style=False)

    with pytest.raises(LabelError, match="scored on whether it was detected"):
        LabeledSet.load(_manifest(tmp_path, entry))


def test_a_required_slot_must_be_read_or_admitted_unreadable(tmp_path: Path) -> None:
    """The hole the whole design turns on.

    Omitting ``beans`` silently would score the pipeline as wrong for naming
    beans that are in the photograph -- the label would be asserting their
    absence when it meant only that nobody wrote it down.
    """
    slots = dict(_entry()["slots"])
    del slots["beans"]

    with pytest.raises(LabelError, match="beans is required"):
        LabeledSet.load(_manifest(tmp_path, _entry(slots=slots)))


def test_a_slot_cannot_be_both_labeled_and_unreadable(tmp_path: Path) -> None:
    with pytest.raises(LabelError, match="both labeled and unreadable"):
        LabeledSet.load(_manifest(tmp_path, _entry(unreadable=["rice"])))


def test_a_hidden_contents_frame_must_say_which_slots_are_hidden(
    tmp_path: Path,
) -> None:
    with pytest.raises(LabelError, match="does not answer"):
        LabeledSet.load(_manifest(tmp_path, _entry(conditions=["contents_hidden"])))


def test_the_condition_and_the_count_must_agree(tmp_path: Path) -> None:
    with pytest.raises(LabelError, match="multi_meal condition and meals_visible"):
        LabeledSet.load(_manifest(tmp_path, _entry(conditions=["multi_meal"], slots={})))


def test_a_meal_with_a_side_is_one_meal(tmp_path: Path) -> None:
    """The likeliest false positive in the whole lane, asserted as a count of one."""
    entry = _entry(conditions=["meal_with_side", "multi_meal"], meals_visible=2)

    with pytest.raises(LabelError):
        LabeledSet.load(_manifest(tmp_path, entry))


def test_provenance_is_required(tmp_path: Path) -> None:
    """Issue #56's licensing note, as a load failure rather than as advice."""
    entry = _entry()
    del entry["capture"]

    with pytest.raises(LabelError, match="capture must record"):
        LabeledSet.load(_manifest(tmp_path, entry))


def test_duplicate_ids_are_refused(tmp_path: Path) -> None:
    with pytest.raises(LabelError, match="duplicate photo id"):
        LabeledSet.load(_manifest(tmp_path, _entry(), _entry()))


def test_an_unknown_condition_is_refused(tmp_path: Path) -> None:
    """A closed vocabulary, because the coverage checks are written against it."""
    with pytest.raises(LabelError, match="not a known condition"):
        LabeledSet.load(_manifest(tmp_path, _entry(conditions=["blurry"])))


def test_a_manifest_that_is_not_json_says_so(tmp_path: Path) -> None:
    path = tmp_path / "labels.json"
    path.write_text("photos: []", encoding="utf-8")

    with pytest.raises(LabelError, match="not valid JSON"):
        LabeledSet.load(path)


# --- the set as a whole -----------------------------------------------------


def test_the_synthetic_set_passes_the_vocabulary_check(tmp_path: Path) -> None:
    labels = synthetic_set(tmp_path)

    labels.against(generated_vocabulary(DEFAULT_TERMS))  # does not raise

    assert len(labels) == 31
    assert labels.missing_files() == ()


def test_a_labeled_frame_with_no_file_is_reported(tmp_path: Path) -> None:
    labels = synthetic_set(tmp_path)
    labels.path(labels.photos[0]).unlink()

    assert [label.photo_id for label in labels.missing_files()] == [
        labels.photos[0].photo_id
    ]


def test_every_condition_the_synthetic_set_uses_is_a_real_one(tmp_path: Path) -> None:
    """Guards the fixture, which the coverage tests trust to be exhaustive."""
    used = {
        condition for label in synthetic_set(tmp_path) for condition in label.conditions
    }

    assert used <= set(Condition)
    assert Condition.MEAL_WITH_SIDE in used
