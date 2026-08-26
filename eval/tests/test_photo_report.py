"""The baseline document, and the four ways it is written not to lie.

Coverage above the scores, both stages always printed, an em dash where a
number does not exist, and every failing frame named. Each of those is asserted
here rather than left to the renderer's good intentions, because a report is
read once by somebody deciding whether to ship.
"""

import json
from pathlib import Path

import pytest

from chip_chat.catalog.records import Slot
from chip_chat.eval.photos.labels import LabeledSet
from chip_chat.eval.photos.report import build_report, render
from chip_chat.eval.photos.run import run_set
from chip_chat.eval.photos.testing import (
    ScriptedVisionModel,
    lane_over,
    synthetic_set,
    truthful_answers,
)
from chip_chat.vision.describe import DescribeUnavailableError
from chip_chat.vision.matcher import SlotRule, SlotRules


def _document(
    labels: LabeledSet, answers: list[str], *, rules: SlotRules | None = None
) -> str:
    rules = rules if rules is not None else SlotRules.defaults()
    runs = run_set(labels, lane_over(labels, ScriptedVisionModel(answers), rules=rules))
    return render(
        build_report(
            labels,
            runs,
            deployment="gpt-4.1-mini-eval-fixture",
            content_version="c0ffee",
            rules=rules,
        )
    )


@pytest.fixture
def document(labels: LabeledSet, spans: object) -> str:
    return _document(labels, truthful_answers(labels))


def test_the_report_records_what_it_measured(document: str) -> None:
    """A report that did not say which deployment and which build is not comparable."""
    assert "gpt-4.1-mini-eval-fixture" in document
    assert "c0ffee" in document


def test_the_floors_are_recorded_because_they_are_the_thing_being_tuned(
    document: str,
) -> None:
    assert "## Floors in force" in document
    assert "| protein | 0.75 | yes |" in document


def test_coverage_comes_before_the_scores(document: str) -> None:
    """A set missing its hard cases produces a good number and a false conclusion."""
    assert document.index("## Coverage") < document.index("## Components")


def test_both_stages_are_printed(document: str) -> None:
    assert "as described (before the floors)" in document
    assert "as believed (after the floors)" in document


def test_the_target_verdict_names_the_believed_figure(document: str) -> None:
    assert "Believed F1 is **1.000**: target **met**." in document


def test_an_incomplete_set_is_flagged_above_its_own_scores(
    tmp_path: Path, spans: object
) -> None:
    synthetic_set(tmp_path)
    manifest = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    manifest["photos"] = manifest["photos"][:6]
    (tmp_path / "labels.json").write_text(json.dumps(manifest), encoding="utf-8")
    labels = LabeledSet.load(tmp_path / "labels.json")

    document = _document(labels, truthful_answers(labels))

    assert "NOT met" in document
    assert "not yet a baseline for the pipeline as a whole" in document
    assert "several meals in one frame" in document


def test_a_number_that_does_not_exist_is_an_em_dash(
    tmp_path: Path, spans: object
) -> None:
    """Never a nought: zero reads as a slot that always failed."""
    synthetic_set(tmp_path)
    manifest = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    # Keep only frames whose labels name no salsa at all.
    manifest["photos"] = [
        entry for entry in manifest["photos"] if not entry["slots"].get("salsas")
    ]
    (tmp_path / "labels.json").write_text(json.dumps(manifest), encoding="utf-8")
    labels = LabeledSet.load(tmp_path / "labels.json")

    document = _document(labels, truthful_answers(labels))
    # Eight pipes is a component row; the floors table's salsa row has four.
    rows = [
        line
        for line in document.splitlines()
        if line.startswith("| salsas |") and line.count("|") == 8
    ]

    assert rows
    assert all(row.endswith("| — | — | — |") for row in rows)


def test_the_expensive_wrong_path_gets_a_sentence_of_its_own(
    labels: LabeledSet, spans: object
) -> None:
    """A draft where the lane should have declined is not a row in a table."""
    answers = truthful_answers(labels)
    index = [label.photo_id for label in labels].index("frame-26")
    payload = json.loads(answers[index])
    payload["meals_visible"] = 1
    payload["vessel"] = {"value": "bowl", "confidence": 0.95}
    payload["protein"] = {"value": "chicken", "confidence": 0.88}
    payload["rice"] = {"value": "white_rice", "confidence": 0.72}
    payload["beans"] = {"value": "black_beans", "confidence": 0.66}
    answers[index] = json.dumps(payload)

    document = _document(labels, answers)

    assert "produced a draft where the correct behaviour" in document
    assert "`frame-26`" in document


def test_a_declining_deployment_is_reported_apart_from_the_accuracy(
    labels: LabeledSet, spans: object
) -> None:
    runs = run_set(
        labels,
        lane_over(
            labels,
            ScriptedVisionModel(
                truthful_answers(labels),
                errors={0: DescribeUnavailableError("the deployment is down")},
            ),
        ),
    )
    document = render(
        build_report(
            labels,
            runs,
            deployment="d",
            content_version=None,
            rules=SlotRules.defaults(),
        )
    )

    assert "## Frames the lane could not answer for" in document
    assert "a deployment that is down is not a model that is wrong" in document
    assert "frame-00" in document


def test_the_calibration_check_runs_against_the_set(document: str) -> None:
    """Issue #53's fourth criterion, on photographs rather than in miniature."""
    assert "`is_meaningfully_distributed()`: **True**." in document


def test_a_describer_pinned_at_one_fails_the_calibration_section(
    labels: LabeledSet, spans: object
) -> None:
    pinned = dict.fromkeys(Slot, 1.0)
    document = _document(labels, truthful_answers(labels, confidences=pinned))

    assert "`is_meaningfully_distributed()`: **False**." in document
    assert "makes every floor above arbitrary" in document


def test_a_report_over_no_frames_says_unverified_rather_than_unmet(
    tmp_path: Path,
) -> None:
    """The state the real set ships in. Nothing was measured, so nothing failed."""
    manifest = tmp_path / "labels.json"
    manifest.write_text(json.dumps({"photos": []}), encoding="utf-8")
    labels = LabeledSet.load(manifest)

    document = render(
        build_report(
            labels,
            [],
            deployment="none",
            content_version=None,
            rules=SlotRules.defaults(),
        )
    )

    assert "The target is **unverified**, which is not the same as unmet." in document


def test_raising_a_floor_moves_only_the_believed_table(
    labels: LabeledSet, spans: object
) -> None:
    """What a tuning run looks like: two documents that differ in one table."""
    strict = SlotRules(
        rules={
            **SlotRules.defaults().rules,
            Slot.BEANS: SlotRule(floor=0.99, required=True),
        }
    )
    document = _document(labels, truthful_answers(labels), rules=strict)
    described, believed = document.split("## Components — as believed")

    assert "| beans | 0 |" not in described
    assert "| beans | 0 |" in believed
