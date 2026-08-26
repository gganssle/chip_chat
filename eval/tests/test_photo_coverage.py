"""Whether the set is the set the ticket asked for.

The scorer says how well the pipeline did on the frames it was given. Nothing
in it can notice that those frames were thirty clean overhead bowls -- that set
scores beautifully and proves nothing, and the failure is invisible to any
precision figure. These are the checks that see it.
"""

import json
from pathlib import Path

from chip_chat.eval.photos.coverage import MINIMUM_PHOTOS, REQUIREMENTS, coverage
from chip_chat.eval.photos.labels import Condition, LabeledSet
from chip_chat.eval.photos.testing import synthetic_set


def _without(tmp_path: Path, condition: Condition) -> LabeledSet:
    """The synthetic set with every frame carrying ``condition`` removed."""
    manifest = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    manifest["photos"] = [
        entry
        for entry in manifest["photos"]
        if condition.value not in entry["conditions"]
    ]
    (tmp_path / "labels.json").write_text(json.dumps(manifest), encoding="utf-8")
    return LabeledSet.load(tmp_path / "labels.json")


def test_a_complete_set_reads_as_complete(labels: LabeledSet) -> None:
    cover = coverage(labels)

    assert cover.enough_photos
    assert cover.unmet == ()
    assert cover.complete


def test_every_requirement_is_named_with_the_frames_that_meet_it(
    labels: LabeledSet,
) -> None:
    """ "Two of three" is more useful with the two named."""
    cover = coverage(labels)
    by_name = {requirement.name: ids for requirement, ids in cover.met}

    assert set(by_name) == {requirement.name for requirement in REQUIREMENTS}
    assert "frame-26" in by_name["several meals in one frame"]


def test_a_set_of_only_clean_frames_is_reported_as_incomplete(tmp_path: Path) -> None:
    """The failure this module exists for: a good score on the wrong set."""
    synthetic_set(tmp_path)
    manifest = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    manifest["photos"] = [
        entry for entry in manifest["photos"] if entry["conditions"] == ["clean"]
    ]
    (tmp_path / "labels.json").write_text(json.dumps(manifest), encoding="utf-8")

    cover = coverage(LabeledSet.load(tmp_path / "labels.json"))

    assert not cover.complete
    unmet = {requirement.name for requirement, _ in cover.unmet}
    assert "several meals in one frame" in unmet
    assert "food that is not Chipotle at all" in unmet
    assert "poor lighting" in unmet


def test_dropping_the_multi_meal_frames_fails_the_decision_records_requirement(
    tmp_path: Path,
) -> None:
    """#58's own acceptance criterion, carried here because #56 inherited it."""
    synthetic_set(tmp_path)
    cover = coverage(_without(tmp_path, Condition.MULTI_MEAL))
    unmet = {requirement.name: requirement for requirement, _ in cover.unmet}

    assert "several meals in one frame" in unmet
    assert "multi-meal-photos.md" in unmet["several meals in one frame"].source


def test_dropping_the_meal_with_a_side_fails_too(tmp_path: Path) -> None:
    """The likeliest false positive is a requirement, not a nicety."""
    synthetic_set(tmp_path)
    cover = coverage(_without(tmp_path, Condition.MEAL_WITH_SIDE))

    assert "one meal beside a side" in {
        requirement.name for requirement, _ in cover.unmet
    }


def test_a_set_under_thirty_frames_is_incomplete_however_varied(
    tmp_path: Path,
) -> None:
    synthetic_set(tmp_path)
    manifest = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    manifest["photos"] = manifest["photos"][:5]
    (tmp_path / "labels.json").write_text(json.dumps(manifest), encoding="utf-8")

    cover = coverage(LabeledSet.load(tmp_path / "labels.json"))

    assert cover.photos < MINIMUM_PHOTOS
    assert not cover.enough_photos
    assert not cover.complete


def test_an_empty_set_is_incomplete_rather_than_vacuously_complete(
    tmp_path: Path,
) -> None:
    """The state the real set ships in, and it must not read as a pass."""
    manifest = tmp_path / "labels.json"
    manifest.write_text(json.dumps({"photos": []}), encoding="utf-8")

    cover = coverage(LabeledSet.load(manifest))

    assert cover.photos == 0
    assert not cover.complete
    assert len(cover.unmet) == len(REQUIREMENTS)
