"""Running the set: the image the model is sent, and the command that sends it.

Two things worth asserting that no score would catch. The first is that a frame
reaches the model as stage 2 would have written it -- issue #63 measured this
deployment's accuracy collapsing below about 512 pixels, so a run that skipped
the downscale would score a pipeline nobody deploys, and would flatter it. The
second is that ``--check`` fails on a set that is not yet the set, because a
check that only warns is a check that gets ignored.
"""

import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from chip_chat.eval.photos.__main__ import main
from chip_chat.eval.photos.labels import LabeledSet
from chip_chat.eval.photos.run import EVAL_CONTAINER, PhotoSetImages, ref_for, run_set
from chip_chat.eval.photos.testing import (
    ScriptedVisionModel,
    lane_over,
    synthetic_set,
    truthful_answers,
)
from chip_chat.vision.limits import DEFAULT_MAX_EDGE
from chip_chat.vision.store import BlobRef
from chip_chat.vision.testing import solid_image

# --- what the model is actually shown ---------------------------------------


def test_a_frame_reaches_the_model_downscaled_the_way_production_sends_it(
    labels: LabeledSet,
) -> None:
    label = labels.photos[0]
    labels.path(label).write_bytes(solid_image((4032, 3024), fmt="JPEG"))

    data = PhotoSetImages(labels).read(ref_for(label))

    with Image.open(BytesIO(data)) as image:
        assert max(image.size) == DEFAULT_MAX_EDGE
        assert image.format == "JPEG"


def test_the_bytes_are_read_once_and_reused(labels: LabeledSet) -> None:
    """Two runs at two floors must be two runs over identical bytes."""
    images = PhotoSetImages(labels)
    ref = ref_for(labels.photos[0])

    assert images.read(ref) is images.read(ref)


def test_a_file_that_would_be_refused_at_upload_is_refused_here(
    labels: LabeledSet,
) -> None:
    """A frame the pipeline would not accept is not a frame it can be scored on."""
    label = labels.photos[0]
    labels.path(label).write_bytes(b"this is not a photograph")

    with pytest.raises(ValueError, match="would be refused at upload"):
        PhotoSetImages(labels).read(ref_for(label))


def test_a_ref_from_another_container_is_refused(labels: LabeledSet) -> None:
    with pytest.raises(ValueError, match=EVAL_CONTAINER):
        PhotoSetImages(labels).read(BlobRef(container="uploads", name="x.jpg"))


def test_a_missing_frame_reads_as_missing_rather_than_empty(
    labels: LabeledSet,
) -> None:
    label = labels.photos[0]
    labels.path(label).unlink()

    with pytest.raises(KeyError):
        PhotoSetImages(labels).read(ref_for(label))


# --- one frame's failure is one frame's failure -----------------------------


def test_only_the_named_frames_are_run(labels: LabeledSet, spans: object) -> None:
    runs = run_set(
        labels,
        lane_over(labels, ScriptedVisionModel(truthful_answers(labels))),
        only=["frame-00", "frame-01"],
    )

    assert [run.photo_id for run in runs] == ["frame-00", "frame-01"]


def test_a_frame_whose_file_vanished_is_recorded_and_the_run_continues(
    labels: LabeledSet, spans: object
) -> None:
    labels.path(labels.photos[0]).unlink()

    runs = run_set(
        labels, lane_over(labels, ScriptedVisionModel(truthful_answers(labels)))
    )

    assert runs[0].error is not None
    assert "DescribeUnavailableError" in runs[0].error
    assert not runs[0].answered
    assert all(run.answered for run in runs[1:])


# --- the command ------------------------------------------------------------


def test_check_passes_on_a_complete_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    synthetic_set(tmp_path)

    assert main(["--set", str(tmp_path / "labels.json"), "--check"]) == 0
    assert "MISSING" not in capsys.readouterr().out


def test_check_fails_on_a_set_that_is_not_yet_the_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    synthetic_set(tmp_path)
    manifest = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    manifest["photos"] = manifest["photos"][:4]
    (tmp_path / "labels.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert main(["--set", str(tmp_path / "labels.json"), "--check"]) == 1
    assert "MISSING several meals in one frame" in capsys.readouterr().out


def test_check_names_a_labeled_frame_whose_file_is_gone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels = synthetic_set(tmp_path)
    labels.path(labels.photos[0]).unlink()

    assert main(["--set", str(tmp_path / "labels.json"), "--check"]) == 1
    assert "MISSING file for frame-00" in capsys.readouterr().out


def test_a_manifest_that_contradicts_itself_fails_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = tmp_path / "labels.json"
    manifest.write_text(
        json.dumps(
            {
                "photos": [
                    {
                        "id": "x",
                        "image": "x.jpg",
                        "capture": {"photographer": "a", "license": "CC0-1.0"},
                        "conditions": ["multi_meal"],
                        "meals_visible": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert main(["--set", str(manifest), "--check"]) == 1
    assert "disagree" in capsys.readouterr().err


def test_the_shipped_set_is_where_the_command_looks_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The repository's own set, checked by the same command CI would run.

    It is empty today, so this asserts the honest failure rather than a pass:
    an empty set is not vacuously complete, and the command says so.
    """
    shipped = Path(__file__).resolve().parents[1] / "photos" / "labels.json"

    assert main(["--set", str(shipped), "--check"]) == 1
    assert "0 labeled frames (need 30)" in capsys.readouterr().out
