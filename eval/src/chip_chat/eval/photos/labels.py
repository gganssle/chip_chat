"""The ground truth: what a person says is in each photograph.

Issue #56 opens with the sentence this module exists to make true -- *without
it, "the photo matcher works well" is an opinion*. A label is the half of the
measurement that no model produced, so the shapes here are deliberately
unforgiving about three things that would each quietly turn the set back into
an opinion.

**A label may not name a term the catalogue does not publish.** That is D3's
property, applied to the truth rather than to the model: stage 4's enums are
generated from the live catalogue and the model cannot say ``carnitas`` when
the menu does not sell it, so a *label* saying ``carnitas`` would score the
model wrong for being right. :meth:`LabeledSet.against` checks every term
against the same :class:`~chip_chat.vision.vocabulary.Vocabulary` the describer
is constrained by, and refuses the set rather than the photograph.

**A slot a person could not read has no ground truth, and is not scored as
absent.** A foil-wrapped burrito has rice in it, and no one looking at the
photograph knows which. Scoring that slot as "the model should have said white
rice" measures clairvoyance; scoring it as "the model should have said nothing"
rewards a describer that gives up. Both are wrong, so :attr:`PhotoLabel.
unreadable` names those slots and :mod:`chip_chat.eval.photos.scoring` drops
them from the component tally in both directions -- while still counting, and
reporting separately, how often the model filled one anyway.

**A frame with two meals in it has no per-meal ground truth at all.**
``docs/decisions/multi-meal-photos.md`` is explicit that on a table of four
bowls the stage-4 slots describe *the picture* rather than any one meal, which
is the whole argument for declining. A label that filled them would be asserting
a meal nobody in the frame is eating -- so this module refuses one.

The manifest is JSON, one file, hand-edited::

    {
      "photos": [
        {
          "id": "clean-chicken-bowl-01",
          "image": "images/clean-chicken-bowl-01.jpg",
          "capture": {"photographer": "...", "license": "CC0-1.0"},
          "conditions": ["clean"],
          "is_chipotle_style": true,
          "meals_visible": 1,
          "slots": {
            "vessel": "bowl",
            "protein": "chicken",
            "rice": "white_rice",
            "beans": "black_beans",
            "salsas": ["fresh_tomato_salsa"],
            "toppings": ["cheese"]
          },
          "unreadable": [],
          "notes": "Overhead, daylight."
        }
      ]
    }

JSON rather than YAML because the repository has no YAML parser in its runtime
dependencies and a labeling format is not worth acquiring one for; hand-edited
rather than generated because the entire value of the file is that a person
looked at each photograph.
"""

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from chip_chat.catalog.records import Slot
from chip_chat.vision.matcher import REQUIRED_SLOTS
from chip_chat.vision.vocabulary import Vocabulary

__all__ = [
    "MULTI_VALUED_SLOTS",
    "SINGLE_VALUED_SLOTS",
    "Capture",
    "Condition",
    "LabelError",
    "LabeledSet",
    "PhotoLabel",
    "slot_terms",
]

SINGLE_VALUED_SLOTS: Final = (Slot.VESSEL, Slot.PROTEIN, Slot.RICE, Slot.BEANS)
"""Slots a meal has at most one of, spelled as the stage-4 schema spells them."""

MULTI_VALUED_SLOTS: Final = (Slot.SALSAS, Slot.TOPPINGS)
"""Slots a meal may have several of. A label lists them; order is not scored."""


class Condition(StrEnum):
    """What makes one frame worth having in the set.

    A closed vocabulary rather than free tags, because
    :mod:`chip_chat.eval.photos.coverage` turns issue #56's prose scope --
    *"deliberately include the hard cases, not just clean overhead shots"* --
    into checks, and a check cannot be written against a tag somebody spells
    two ways.

    Attributes:
        CLEAN: Well lit, one meal, contents visible. The easy case, and the set
            needs enough of them to say what the ceiling is.
        LOW_LIGHT: Underexposed, or lit by something orange.
        PARTIALLY_EATEN: Half of it is gone, which is what a photograph taken
            at a table actually looks like.
        CONTENTS_HIDDEN: A wrapped burrito, or a lid. Whatever is inside is a
            fact about the meal that the photograph does not carry, so such a
            frame necessarily names :attr:`PhotoLabel.unreadable` slots.
        ANGLED: Shot from the side rather than overhead, so the layers occlude
            each other.
        CLUTTERED: A tray, a table, a phone, a drink -- one meal among objects.
        MULTI_MEAL: Two or more orderable meals in frame. PRD V7.
        MEAL_WITH_SIDE: One meal and something that is not one -- a bowl next
            to a bag of chips. ``docs/decisions/multi-meal-photos.md`` calls
            this "the likeliest false positive, and it fires on the most
            ordinary photo anyone will send", which is why it is its own
            condition rather than a note.
        NOT_CHIPOTLE: Food this restaurant does not serve. PRD V4.
    """

    CLEAN = "clean"
    LOW_LIGHT = "low_light"
    PARTIALLY_EATEN = "partially_eaten"
    CONTENTS_HIDDEN = "contents_hidden"
    ANGLED = "angled"
    CLUTTERED = "cluttered"
    MULTI_MEAL = "multi_meal"
    MEAL_WITH_SIDE = "meal_with_side"
    NOT_CHIPOTLE = "not_chipotle"


class LabelError(ValueError):
    """A manifest that cannot be believed as ground truth.

    Raised at load, never at score time. A set that is wrong about itself
    produces numbers that look exactly like numbers, and the point of failing
    here is that nobody gets to read one.
    """


@dataclass(frozen=True, slots=True)
class Capture:
    """Where a photograph came from, and on what terms it may be here.

    Issue #56's licensing note is a requirement rather than advice: *"a labeled
    dataset of someone else's photographs is an avoidable problem"* in a public
    repository. Recording provenance per photograph is what makes that
    auditable later, when whoever added frame 27 is no longer in the room.

    Attributes:
        photographer: Who took it. A name or handle; the person who can say
            yes.
        license: The terms it is here under, as an SPDX identifier where one
            fits (``CC0-1.0``) or a plain phrase where none does.
        taken: ISO date, where it is known. Not scored; useful when a menu
            changes and an old frame stops matching the catalogue.
    """

    photographer: str
    license: str
    taken: str | None = None


@dataclass(frozen=True, slots=True)
class PhotoLabel:
    """One photograph, and what a person says is in it.

    Attributes:
        photo_id: Stable identifier, unique in the set. Appears in the report,
            so it should read like a description of the frame.
        image: Path to the file, relative to the manifest.
        capture: Provenance. See :class:`Capture`.
        conditions: What this frame is in the set *for*. See :class:`Condition`.
        is_chipotle_style: Whether the food is the kind this restaurant serves.
        meals_visible: Orderable meal-sized compositions in the frame. A side
            is not one; ``docs/decisions/multi-meal-photos.md``.
        slots: The components a person read off the photograph, by slot. Single
            valued slots hold a term; :data:`MULTI_VALUED_SLOTS` hold a tuple.
            Empty on any frame that is not one orderable Chipotle-style meal,
            because such a frame has no per-meal truth to hold -- see the module
            docstring.
        unreadable: Slots the photograph does not answer. Scored in neither
            direction. A required slot here means the correct outcome for this
            frame is a question rather than a draft, which is what makes
            :attr:`~chip_chat.vision.matcher.Outcome.CLARIFY` measurable rather
            than merely permitted.
        notes: For the person reading the report. Never parsed.
    """

    photo_id: str
    image: str
    capture: Capture
    conditions: frozenset[Condition] = frozenset()
    is_chipotle_style: bool = True
    meals_visible: int = 1
    slots: Mapping[Slot, tuple[str, ...]] = field(default_factory=dict)
    unreadable: frozenset[Slot] = frozenset()
    notes: str = ""

    @property
    def orderable(self) -> bool:
        """Whether this frame is one Chipotle-style meal, and so has components.

        The only frames :mod:`chip_chat.eval.photos.scoring` tallies components
        over. The other two kinds are scored on whether they were *detected*,
        which is a different measurement and has its own section of the report.
        """
        return self.is_chipotle_style and self.meals_visible == 1

    @property
    def several_meals(self) -> bool:
        """Whether the frame holds more than one orderable meal."""
        return self.meals_visible >= 2

    def pairs(self) -> frozenset[tuple[Slot, str]]:
        """Every labeled component as ``(slot, term)``.

        The unit of component-level scoring, and the reason a wrong protein
        costs twice: it is one pair the model missed and one it invented.
        Slots in :attr:`unreadable` contribute nothing, here or on the
        prediction side.
        """
        return frozenset(
            (slot, term)
            for slot, terms in self.slots.items()
            if slot not in self.unreadable
            for term in terms
        )

    def unreadable_required(self) -> tuple[Slot, ...]:
        """The required slots this photograph does not answer, in schema order.

        Non-empty means a draft cannot be right, so the outcome that should be
        measured against this frame is a clarifying question.
        """
        return tuple(slot for slot in REQUIRED_SLOTS if slot in self.unreadable)


@dataclass(frozen=True, slots=True)
class LabeledSet:
    """Every labeled photograph, and where the files are.

    Attributes:
        photos: The labels, in manifest order.
        root: The directory :attr:`PhotoLabel.image` paths are relative to --
            the manifest's own directory. Carried so that a runner can open the
            files without being told twice.
    """

    photos: tuple[PhotoLabel, ...]
    root: Path

    def __len__(self) -> int:
        return len(self.photos)

    def __iter__(self) -> Iterator[PhotoLabel]:
        return iter(self.photos)

    def path(self, label: PhotoLabel) -> Path:
        """Where ``label``'s file is on disk."""
        return self.root / label.image

    @classmethod
    def load(cls, manifest: Path) -> "LabeledSet":
        """Read a manifest, and check that it is internally coherent.

        The terms are *not* checked against a vocabulary here, because loading
        a set should not require a built catalogue -- ``--check`` on a laptop
        with no catalogue is still worth having. :meth:`against` is that check,
        and the runner calls it before it runs anything.

        Args:
            manifest: Path to the JSON file.

        Returns:
            The set.

        Raises:
            LabelError: If the file is not readable as a manifest, or any label
                contradicts itself. See :class:`LabelError`.
        """
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError as error:
            raise LabelError(f"could not read {manifest}: {error}") from error
        except json.JSONDecodeError as error:
            raise LabelError(f"{manifest} is not valid JSON: {error}") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("photos"), list):
            raise LabelError(f"{manifest} must be an object with a `photos` array")

        photos = tuple(
            _label(entry, index) for index, entry in enumerate(payload["photos"])
        )
        seen: set[str] = set()
        for label in photos:
            if label.photo_id in seen:
                raise LabelError(f"duplicate photo id {label.photo_id!r}")
            seen.add(label.photo_id)
        return cls(photos=photos, root=manifest.parent)

    def against(self, vocabulary: Vocabulary) -> None:
        """Check every labeled term against the vocabulary stage 4 is held to.

        Args:
            vocabulary: The generated vocabulary. The same object the describer
                is built with, so that a set and a run cannot disagree about
                what the menu publishes.

        Raises:
            LabelError: If a label names a term this catalogue does not
                publish. The set is refused rather than the photograph: one bad
                term means the manifest was written against a different
                catalogue build, and the rest of it is no more trustworthy.
        """
        for label in self.photos:
            for slot, terms in label.slots.items():
                published = vocabulary.values(slot.value)
                for term in terms:
                    if term not in published:
                        raise LabelError(
                            f"{label.photo_id}: the catalogue publishes no "
                            f"{slot.value} term {term!r}"
                        )

    def missing_files(self) -> tuple[PhotoLabel, ...]:
        """Labels whose photograph is not on disk, in manifest order.

        A separate question from whether the manifest is coherent, and asked
        separately: a set can be perfectly labeled and still un-runnable
        because the images live in blob storage rather than beside it.
        """
        return tuple(label for label in self.photos if not self.path(label).is_file())


def _label(entry: object, index: int) -> PhotoLabel:
    """Build one label from its manifest entry, refusing anything incoherent."""
    if not isinstance(entry, dict):
        raise LabelError(f"photos[{index}] must be an object")
    where = entry.get("id", f"photos[{index}]")

    photo_id = _text(entry, "id", where)
    image = _text(entry, "image", where)
    capture = _capture(entry.get("capture"), where)
    conditions = frozenset(
        _condition(value, where) for value in _list(entry, "conditions")
    )
    is_chipotle_style = _flag(entry, "is_chipotle_style", where, default=True)
    meals_visible = _count(entry, "meals_visible", where)
    unreadable = frozenset(_slot(value, where) for value in _list(entry, "unreadable"))
    slots = _slots(entry.get("slots"), where)

    label = PhotoLabel(
        photo_id=photo_id,
        image=image,
        capture=capture,
        conditions=conditions,
        is_chipotle_style=is_chipotle_style,
        meals_visible=meals_visible,
        slots=slots,
        unreadable=unreadable,
        notes=str(entry.get("notes", "")),
    )
    _coherent(label, where)
    return label


def _coherent(label: PhotoLabel, where: str) -> None:
    """Refuse a label that contradicts itself, or the design it is scoring.

    Every rule here is a rule about what a *photograph* can be ground truth
    for, and each of them has cost somebody a wrong number somewhere.
    """
    if (Condition.NOT_CHIPOTLE in label.conditions) != (not label.is_chipotle_style):
        raise LabelError(
            f"{where}: the not_chipotle condition and is_chipotle_style disagree"
        )
    if (Condition.MULTI_MEAL in label.conditions) != label.several_meals:
        raise LabelError(f"{where}: the multi_meal condition and meals_visible disagree")
    if Condition.MEAL_WITH_SIDE in label.conditions and label.meals_visible != 1:
        raise LabelError(
            f"{where}: a meal with a side is one orderable meal, not "
            f"{label.meals_visible}"
        )
    if not label.orderable and (label.slots or label.unreadable):
        # The multi-meal argument, as a load-time refusal. On a frame with two
        # meals in it the stage-4 slots describe the picture rather than either
        # meal, so there is nothing for a per-meal label to be true about --
        # and a set that carried one would score the describer against a meal
        # nobody in the photograph is eating.
        raise LabelError(
            f"{where}: only a single Chipotle-style meal has component labels; "
            "this frame is scored on whether it was detected"
        )
    overlap = sorted(slot.value for slot in label.unreadable if slot in label.slots)
    if overlap:
        raise LabelError(f"{where}: {', '.join(overlap)} is both labeled and unreadable")
    if label.orderable:
        for slot in REQUIRED_SLOTS:
            if slot not in label.slots and slot not in label.unreadable:
                raise LabelError(
                    f"{where}: {slot.value} is required, so the label must "
                    "either give its term or name it unreadable"
                )
    if Condition.CONTENTS_HIDDEN in label.conditions and not label.unreadable:
        raise LabelError(
            f"{where}: a frame whose contents are hidden must say which slots "
            "the photograph does not answer"
        )


def _capture(value: object, where: str) -> Capture:
    if not isinstance(value, dict):
        raise LabelError(f"{where}: capture must record photographer and license")
    photographer = value.get("photographer")
    license_ = value.get("license")
    if not isinstance(photographer, str) or not photographer:
        raise LabelError(f"{where}: capture.photographer is required")
    if not isinstance(license_, str) or not license_:
        raise LabelError(f"{where}: capture.license is required")
    taken = value.get("taken")
    return Capture(
        photographer=photographer,
        license=license_,
        taken=taken if isinstance(taken, str) else None,
    )


def _slots(value: object, where: str) -> Mapping[Slot, tuple[str, ...]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise LabelError(f"{where}: slots must be an object")
    labeled: dict[Slot, tuple[str, ...]] = {}
    for name, terms in value.items():
        slot = _slot(name, where)
        if slot in SINGLE_VALUED_SLOTS:
            if not isinstance(terms, str) or not terms:
                raise LabelError(f"{where}: {slot.value} takes one term")
            labeled[slot] = (terms,)
            continue
        if not isinstance(terms, list) or not all(
            isinstance(term, str) and term for term in terms
        ):
            raise LabelError(f"{where}: {slot.value} takes a list of terms")
        if len(set(terms)) != len(terms):
            raise LabelError(f"{where}: {slot.value} repeats a term")
        if terms:
            labeled[slot] = tuple(terms)
    return labeled


def _slot(value: object, where: str) -> Slot:
    if isinstance(value, str):
        try:
            return Slot(value)
        except ValueError:
            pass
    raise LabelError(f"{where}: {value!r} is not a slot")


def _condition(value: object, where: str) -> Condition:
    if isinstance(value, str):
        try:
            return Condition(value)
        except ValueError:
            pass
    raise LabelError(f"{where}: {value!r} is not a known condition")


def _text(entry: Mapping[str, Any], key: str, where: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise LabelError(f"{where}: {key} is required")
    return value


def _flag(entry: Mapping[str, Any], key: str, where: str, *, default: bool) -> bool:
    value = entry.get(key, default)
    if not isinstance(value, bool):
        raise LabelError(f"{where}: {key} must be true or false")
    return value


def _count(entry: Mapping[str, Any], key: str, where: str) -> int:
    value = entry.get(key, 1)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise LabelError(f"{where}: {key} must be a count")
    return value


def _list(entry: Mapping[str, Any], key: str) -> Iterable[object]:
    value = entry.get(key, [])
    if not isinstance(value, list):
        raise LabelError(f"{entry.get('id', key)}: {key} must be a list")
    return value


def slot_terms(labels: Sequence[PhotoLabel], slot: Slot) -> tuple[str, ...]:
    """Every term the set uses for ``slot``, sorted, for a coverage report."""
    return tuple(sorted({term for label in labels for term in label.slots.get(slot, ())}))
