"""Doubles for exercising the scorer without photographs -- and what they are not.

**Nothing in here is a labeled photo set.** :func:`synthetic_set` writes plain
coloured rectangles and a manifest describing them as meals, which is a fixture
for the *arithmetic* and would be a fraud as a dataset: no vision model can
read a bowl off a rectangle, so a score computed over one measures the stub
that produced it. Issue #56's set is thirty real photographs, and there is no
version of this module that substitutes for them.

What it is for is the other half of the ticket -- *"runs as a repeatable
experiment"*. A scorer is a small pile of arithmetic that is easy to get subtly
wrong and impossible to notice being wrong, because every version of it returns
a plausible number between nought and one. So the tests drive it at the size it
will really run at, with a describer whose answers are known exactly, and check
the numbers that come out against numbers computed by hand. That needs a set of
thirty labeled things and a model that answers to a script, and neither of them
needs to be a photograph.

.. code-block:: python

    labels = synthetic_set(tmp_path)                  # 30 frames, all the cases
    model = ScriptedVisionModel(truthful_answers(labels))
    runs = run_set(labels, lane_over(labels, model))
    assert score(labels, runs).believed.overall.f1 == 1.0   # by construction
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chip_chat.catalog.records import Slot
from chip_chat.eval.photos.labels import LabeledSet, PhotoLabel
from chip_chat.eval.photos.run import PhotoSetImages
from chip_chat.vision.describe import (
    DescribeUnavailableError,
    MealDescriber,
    VisionAnswer,
)
from chip_chat.vision.lane import PhotoLane
from chip_chat.vision.matcher import MealMatcher, SlotRules
from chip_chat.vision.testing import (
    DEFAULT_TERMS,
    STUB_VISION_USAGE,
    generated_vocabulary,
    menu_catalog,
    solid_image,
)

__all__ = [
    "FIXTURE_CONFIDENCES",
    "ScriptedVisionModel",
    "lane_over",
    "synthetic_set",
    "truthful_answers",
]

FIXTURE_CONFIDENCES: Mapping[Slot, float] = {
    Slot.VESSEL: 0.95,
    Slot.PROTEIN: 0.88,
    Slot.RICE: 0.72,
    Slot.BEANS: 0.66,
    Slot.SALSAS: 0.61,
    Slot.TOPPINGS: 0.79,
}
"""One confidence per slot, above every default floor and all six different.

All six different because a fixture that answered the same number everywhere
would make :func:`~chip_chat.vision.describe.confidence_profile` report a
degenerate run, and the calibration section of the report would then be
exercised only in its failing shape. Above every floor so that a test seeing a
clarification knows the floors did it and not the fixture.
"""


@dataclass
class ScriptedVisionModel:
    """A :class:`~chip_chat.vision.describe.VisionModel` answering in set order.

    Keyed by call order rather than by image, because the describer is handed
    bytes and not a reference -- and the bytes it is handed are stage 2's
    re-encoding rather than the file's, so a fixture that matched on them would
    be asserting on the JPEG encoder.

    Attributes:
        responses: The raw response bodies, one per frame, in the order
            :func:`~chip_chat.eval.photos.run.run_set` will run them.
        errors: Indices that raise instead of answering, for the case the
            runner exists to survive: a deployment that refuses one frame in
            the middle of thirty.
        calls: How many times it was asked.
    """

    responses: Sequence[str]
    errors: Mapping[int, Exception] = field(default_factory=dict)
    deployment: str = "gpt-4.1-mini-eval-fixture"
    calls: int = 0

    def describe(
        self,
        *,
        image: bytes,
        media_type: str,
        response_format: Mapping[str, Any],
        system_prompt: str,
        user_prompt: str,
    ) -> VisionAnswer:
        index = self.calls
        self.calls += 1
        error = self.errors.get(index)
        if error is not None:
            raise error
        if index >= len(self.responses):
            raise DescribeUnavailableError(
                f"the script has {len(self.responses)} answers and this is call "
                f"{index + 1}"
            )
        return VisionAnswer(content=self.responses[index], usage=STUB_VISION_USAGE)


def truthful_answers(
    labels: LabeledSet,
    *,
    confidences: Mapping[Slot, float] | None = None,
) -> list[str]:
    """One stage-4 response per frame, saying exactly what the label says.

    The baseline a test measures departures from: a run of these scores 1.0 on
    every slot by construction, so a test that introduces one mistake knows
    precisely which cell of which table should move.

    Slots the label marks unreadable are left empty, because that is what a
    describer reading the photograph honestly would do -- and a test wanting the
    other case, a model filling a slot nobody could read, edits the one answer.

    Args:
        labels: The set to answer.
        confidences: Per-slot confidence to report. Defaults to
            :data:`FIXTURE_CONFIDENCES`.

    Returns:
        The response bodies as JSON strings, in set order.
    """
    per_slot = FIXTURE_CONFIDENCES if confidences is None else confidences
    return [json.dumps(_answer(label, per_slot)) for label in labels]


def _answer(label: PhotoLabel, confidences: Mapping[Slot, float]) -> dict[str, Any]:
    """The stage-4 payload that agrees with one label."""
    payload: dict[str, Any] = {
        "is_chipotle_style": label.is_chipotle_style,
        "meals_visible": label.meals_visible,
        "notes": "",
    }
    for slot, terms in label.slots.items():
        confidence = confidences[slot]
        if slot in (Slot.SALSAS, Slot.TOPPINGS):
            payload[slot.value] = [
                {"value": term, "confidence": confidence} for term in terms
            ]
        else:
            payload[slot.value] = {"value": terms[0], "confidence": confidence}
    return payload


def lane_over(
    labels: LabeledSet,
    model: ScriptedVisionModel,
    *,
    rules: SlotRules | None = None,
) -> PhotoLane:
    """Build a photo lane that reads ``labels``' files off disk.

    One catalogue build for both stages, as
    :func:`chip_chat.vision.testing.photo_lane` does and for the same reason:
    stage 5 checks the build stage 4 was constrained to, and two fixtures
    assembled independently raise before either of them is scored.

    Args:
        labels: The set. Its ``root`` is where the frames are.
        model: The scripted describer.
        rules: The floors. Defaults to the shipped ones, which is what a
            baseline run uses.

    Returns:
        The lane.
    """
    catalog = menu_catalog(DEFAULT_TERMS)
    return PhotoLane(
        MealDescriber(
            model,
            images=PhotoSetImages(labels),
            vocabulary=generated_vocabulary(
                DEFAULT_TERMS, content_version=catalog.content_version()
            ),
        ),
        MealMatcher(catalog, rules=rules if rules is not None else SlotRules.defaults()),
    )


def synthetic_set(root: Path) -> LabeledSet:
    """Write thirty labeled rectangles and their manifest, and load it back.

    Covers every requirement in :mod:`chip_chat.eval.photos.coverage`, so a
    test can assert that a complete set reads as complete -- and, by editing
    one entry, that an incomplete one reads as incomplete.

    Loaded back through
    :meth:`~chip_chat.eval.photos.labels.LabeledSet.load` rather than
    constructed directly, so that every fixture frame also exercises the
    manifest parser and its refusals.

    Args:
        root: Directory to write ``labels.json`` and ``images/`` into.

    Returns:
        The loaded set.
    """
    images = root / "images"
    images.mkdir(parents=True, exist_ok=True)
    entries = [dict(entry) for entry in _SYNTHETIC]
    for index, entry in enumerate(entries):
        # A distinct fill per frame, so that a bug swapping two frames' bytes
        # shows up as a swapped score rather than as nothing at all.
        (root / str(entry["image"])).write_bytes(
            solid_image((320, 240), colour=(40 + 7 * index, 90, 60))
        )
    manifest = root / "labels.json"
    manifest.write_text(json.dumps({"photos": entries}, indent=2), encoding="utf-8")
    return LabeledSet.load(manifest)


def _capture(index: int) -> dict[str, str]:
    return {
        "photographer": "fixture",
        "license": "CC0-1.0",
        "taken": f"2026-08-{(index % 28) + 1:02d}",
    }


def _meal(
    index: int,
    *,
    vessel: str,
    protein: str,
    conditions: Sequence[str],
    rice: str | None = "white_rice",
    beans: str | None = "black_beans",
    salsas: Sequence[str] = ("fresh_tomato_salsa",),
    toppings: Sequence[str] = ("cheese",),
    unreadable: Sequence[str] = (),
) -> dict[str, Any]:
    """One single-meal fixture entry, in manifest shape."""
    slots: dict[str, Any] = {"vessel": vessel, "protein": protein}
    if rice is not None:
        slots["rice"] = rice
    if beans is not None:
        slots["beans"] = beans
    if salsas:
        slots["salsas"] = list(salsas)
    if toppings:
        slots["toppings"] = list(toppings)
    for slot in unreadable:
        slots.pop(slot, None)
    return {
        "id": f"frame-{index:02d}",
        "image": f"images/frame-{index:02d}.jpg",
        "capture": _capture(index),
        "conditions": list(conditions),
        "is_chipotle_style": True,
        "meals_visible": 1,
        "slots": slots,
        "unreadable": list(unreadable),
        "notes": "",
    }


def _other(index: int, *, conditions: Sequence[str], meals: int) -> dict[str, Any]:
    """One fixture entry with no component labels: a decline case."""
    return {
        "id": f"frame-{index:02d}",
        "image": f"images/frame-{index:02d}.jpg",
        "capture": _capture(index),
        "conditions": list(conditions),
        "is_chipotle_style": "not_chipotle" not in conditions,
        "meals_visible": meals,
        "slots": {},
        "unreadable": [],
        "notes": "",
    }


_SYNTHETIC: tuple[dict[str, Any], ...] = (
    # Twelve clean frames, both vessels and both proteins, varying the optional
    # slots so that salsas and toppings have something to be wrong about.
    _meal(0, vessel="bowl", protein="chicken", conditions=("clean",)),
    _meal(1, vessel="bowl", protein="steak", conditions=("clean",)),
    _meal(2, vessel="burrito", protein="chicken", conditions=("clean",)),
    _meal(3, vessel="burrito", protein="steak", conditions=("clean",)),
    _meal(4, vessel="bowl", protein="chicken", conditions=("clean",), toppings=()),
    _meal(
        5,
        vessel="bowl",
        protein="steak",
        conditions=("clean",),
        toppings=("cheese", "guacamole"),
    ),
    _meal(6, vessel="burrito", protein="chicken", conditions=("clean",), salsas=()),
    _meal(7, vessel="burrito", protein="steak", conditions=("clean",)),
    _meal(8, vessel="bowl", protein="chicken", conditions=("clean", "angled")),
    _meal(9, vessel="bowl", protein="steak", conditions=("clean", "cluttered")),
    _meal(10, vessel="burrito", protein="chicken", conditions=("clean",)),
    _meal(11, vessel="bowl", protein="chicken", conditions=("clean",)),
    # The hard single-meal cases.
    _meal(12, vessel="bowl", protein="chicken", conditions=("low_light",)),
    _meal(13, vessel="burrito", protein="steak", conditions=("low_light", "angled")),
    _meal(14, vessel="bowl", protein="steak", conditions=("partially_eaten",)),
    _meal(
        15,
        vessel="bowl",
        protein="chicken",
        conditions=("partially_eaten", "cluttered"),
        toppings=(),
    ),
    _meal(
        16,
        vessel="burrito",
        protein="chicken",
        conditions=("contents_hidden",),
        unreadable=("rice", "beans", "salsas", "toppings"),
    ),
    _meal(
        17,
        vessel="burrito",
        protein="steak",
        conditions=("contents_hidden",),
        unreadable=("rice", "beans", "salsas", "toppings"),
    ),
    _meal(
        18,
        vessel="bowl",
        protein="chicken",
        conditions=("angled",),
        unreadable=("beans",),
    ),
    _meal(19, vessel="bowl", protein="steak", conditions=("cluttered",)),
    _meal(20, vessel="burrito", protein="chicken", conditions=("angled",)),
    _meal(21, vessel="bowl", protein="chicken", conditions=("low_light", "cluttered")),
    _meal(22, vessel="bowl", protein="steak", conditions=("partially_eaten",)),
    _meal(23, vessel="burrito", protein="steak", conditions=("clean",)),
    # A bowl beside a bag of chips: one meal, and the likeliest false positive.
    _meal(24, vessel="bowl", protein="chicken", conditions=("meal_with_side",)),
    _meal(25, vessel="burrito", protein="steak", conditions=("meal_with_side",)),
    # The frames with no per-meal truth at all.
    _other(26, conditions=("multi_meal",), meals=2),
    _other(27, conditions=("multi_meal", "cluttered"), meals=4),
    _other(28, conditions=("multi_meal", "low_light"), meals=3),
    _other(29, conditions=("not_chipotle",), meals=1),
    _other(30, conditions=("not_chipotle", "cluttered"), meals=1),
)
"""Thirty-one fixture frames covering every scope requirement.

Thirty-one rather than thirty so that a test can delete one and still have a
complete set, which is what the "an incomplete set reads as incomplete" test
needs in the other direction.
"""
