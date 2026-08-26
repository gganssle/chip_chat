"""One row per thing that can be asked, and what is expected of it.

Issue #72 promotes the golden set into a dataset, and a dataset is flat where a
set is a tree. :class:`DatasetEntry` is that flattening, and three of its
decisions are the ones worth reading before adding a column.

**Every entry carries its expected lane and its PRD requirement ids.** That is
#72's third acceptance criterion, and it is stated as a property of *every*
entry rather than of the golden ones, which is why :attr:`DatasetEntry.
expected_lane` has no ``None``: a labeled photograph is ground truth for the
vision lane whether or not any model ever chose to route to it.

**Not every entry can be scored on lane selection, and the row says which.**
``eval/README.md`` draws the line: the labeled photo set runs the vision lane
*directly*, from a blob reference through stages 4 and 5, so no model chose to
call the tool and routing is invisible to it. An entry from that set therefore
names no expected tool, and :attr:`DatasetEntry.scores_routing` is what
separates that from the golden set's *"this turn should reach for nothing"* --
two very different empty cells that a single blank column would collapse.

**A photograph's ground truth stays one object.** :class:`FrameTruth` is carried
whole rather than flattened into eleven columns that are blank on four rows in
five. The two halves of this dataset are read by different scorers -- a per-lane
pass rate over the golden rows, component precision and recall over the frames
-- and the one that wants the slots wants all of them at once.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from chip_chat.eval.golden.cases import JUDGED, GoldenCase, GoldenSet
from chip_chat.eval.golden.lanes import Lane
from chip_chat.eval.photos.labels import LabeledSet, PhotoLabel

__all__ = [
    "DIGEST_COLUMN",
    "DIGEST_LENGTH",
    "GOLDEN_PREFIX",
    "ID_COLUMN",
    "PHOTOS_PREFIX",
    "DatasetEntry",
    "FrameTruth",
    "InputKind",
    "Origin",
    "digest_of",
    "golden_entries",
    "photo_entries",
]

ID_COLUMN: Final = "entry_id"
"""The column a published row is matched back to its entry by."""

DIGEST_COLUMN: Final = "entry_digest"
"""The column a published row's content is compared by.

See :attr:`DatasetEntry.digest`.
"""

DIGEST_LENGTH: Final = 12
"""Hex characters kept of an entry's digest, and of a dataset's version.

Twelve, for the reason git picks a similar number: long enough that a collision
is not a thing that happens to a few hundred rows, short enough to sit in a
chart axis and a commit message without wrapping. These are identifiers rather
than signatures -- nobody here is defending against somebody who *wants* two
entries to collide.
"""

GOLDEN_PREFIX: Final = "golden/"
"""What a golden case's id becomes in the dataset."""

PHOTOS_PREFIX: Final = "photos/"
"""What a labeled photograph's id becomes in the dataset.

Two sets that have never heard of each other are being put in one namespace, so
the ids are prefixed rather than trusted to stay distinct. It also means a row
can be read back to the file it came from without consulting a column.
"""


class Origin(StrEnum):
    """Which set an entry was promoted from.

    Attributes:
        GOLDEN: ``eval/golden/cases.json`` -- a visitor message, and the lane
            it should take.
        PHOTOS: ``eval/photos/labels.json`` -- a photograph, and what a person
            says is in it.
    """

    GOLDEN = "golden"
    PHOTOS = "photos"


class InputKind(StrEnum):
    """What the input column holds, so a runner knows what to do with it.

    Attributes:
        MESSAGE: Text the visitor typed. Sent as a turn.
        IMAGE: A path to an image file, relative to the manifest it was
            labeled in. A runner resolves it; the dataset carries the path
            rather than the bytes, because a dataset row is not a place to put
            a photograph.
    """

    MESSAGE = "message"
    IMAGE = "image"


@dataclass(frozen=True, slots=True)
class FrameTruth:
    """What a person read off one photograph.

    A transcription of :class:`~chip_chat.eval.photos.labels.PhotoLabel` into
    plain strings, so that the dataset carries no enum a consumer would need
    this package to interpret.

    Attributes:
        is_chipotle_style: Whether the food is the kind this restaurant serves.
            PRD V4 is measured over the frames where this is false.
        meals_visible: Orderable meal-sized compositions in the frame. Two or
            more means the correct outcome is a question rather than a draft --
            ``docs/decisions/multi-meal-photos.md``.
        conditions: What this frame is in the set *for*, sorted.
        slots: The components a person read, by slot name, each a sorted tuple
            of terms. Empty on any frame that is not one orderable meal,
            because such a frame has no per-meal truth to hold.
        unreadable: Slots the photograph does not answer, sorted. Scored in
            neither direction, by the argument in
            :mod:`chip_chat.eval.photos.scoring`.
        photographer: Who took it.
        license: The terms it is here under.
    """

    is_chipotle_style: bool
    meals_visible: int
    conditions: tuple[str, ...]
    slots: Mapping[str, tuple[str, ...]]
    unreadable: tuple[str, ...]
    photographer: str
    license: str

    def as_json(self) -> str:
        """The truth as one canonical JSON object, for the row.

        Sorted keys and no incidental whitespace, because this string is fed to
        :func:`~chip_chat.eval.dataset.versions.fingerprint` and a version that
        moved because a dict iterated differently would be a version that means
        nothing.
        """
        return _canonical(
            {
                "is_chipotle_style": self.is_chipotle_style,
                "meals_visible": self.meals_visible,
                "conditions": list(self.conditions),
                "slots": {slot: list(terms) for slot, terms in self.slots.items()},
                "unreadable": list(self.unreadable),
                "photographer": self.photographer,
                "license": self.license,
            }
        )


@dataclass(frozen=True, slots=True)
class DatasetEntry:
    """One dataset row: what is asked, where it belongs, and what is expected.

    Attributes:
        entry_id: Unique in the dataset, and stable across versions. The join
            key: an experiment result three weeks old is matched back to what
            it was run against by this and by nothing else.
        origin: Which set it came from. See :class:`Origin`.
        input: The visitor's message, or the image path. See :class:`InputKind`.
        input_kind: Which of those it is.
        expected_lane: The lane this entry belongs to. Never absent -- a turn
            that should reach for nothing belongs to
            :attr:`~chip_chat.eval.golden.lanes.Lane.NONE`, which is a lane.
        expected_tool: The tool the turn should reach for, where lane selection
            is a thing this entry can be scored on. Empty otherwise, and
            :attr:`scores_routing` is how to tell the two empties apart.
        requirements: The PRD identifiers this entry covers, sorted. At least
            one, guaranteed by the sets this is built from.
        checks: What has to be observed, sorted. Golden entries only.
        judged_checks: The subset of :attr:`checks` that no data structure can
            settle, carried out separately because an online eval attaches a
            judge to exactly these and to nothing else.
        persona: Which persona the entry presumes, or ``any``.
        context: Prior assistant turns the input presupposes, in order.
        confirmed: Whether the visitor has already pressed Confirm on the draft
            this turn acts on. PRD T2 reads differently on each side of it.
        forbidden_tools: Tools that must not be called, sorted. The confusable
            half of a boundary case.
        menu_terms: Published menu terms the entry leans on, in set order.
        frame: A photograph's ground truth, or ``None`` on a golden entry.
        why: What this entry is for, printed beside a failure.
    """

    entry_id: str
    origin: Origin
    input: str
    input_kind: InputKind
    expected_lane: Lane
    expected_tool: str = ""
    requirements: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()
    judged_checks: tuple[str, ...] = ()
    persona: str = ""
    context: tuple[str, ...] = ()
    confirmed: bool = False
    forbidden_tools: tuple[str, ...] = ()
    menu_terms: tuple[str, ...] = ()
    frame: FrameTruth | None = None
    why: str = ""

    @property
    def scores_routing(self) -> bool:
        """Whether lane selection is a thing this entry can be scored on.

        True for every golden entry, including the ones expecting no tool at
        all: *"call nothing"* is an answer routing can be wrong about. False
        for every photograph, because the photo set runs the vision lane
        directly and no model ever chose to enter it -- see the section of
        ``eval/README.md`` that draws the line between the two sets.
        """
        return self.origin is Origin.GOLDEN

    @property
    def digest(self) -> str:
        """A hash of everything this entry says, as :data:`DIGEST_LENGTH` hex.

        The unit of the no-mutation rule.
        :func:`~chip_chat.eval.dataset.publish.publish` compares this against
        what a dataset already holds for the same
        :attr:`entry_id`, and a difference is refused rather than uploaded:
        editing a question in place makes every score taken before the edit a
        measurement of something nobody can see any more.

        A digest rather than a field-by-field comparison because the round trip
        through a store is not type-preserving -- a boolean can come back as
        the string ``"true"`` -- and a version that thought every row had
        changed on every publish would be a version nobody believed.
        """
        return digest_of(_canonical(dict(self._columns())))

    def row(self) -> Mapping[str, str | int | bool]:
        """The entry as a flat row of scalars, carrying its own digest.

        Composite fields become canonical JSON strings rather than nested
        values, because the far side of
        :class:`~chip_chat.eval.dataset.store.DatasetStore` is a table and a
        table cell holding a list is a cell every consumer has to guess at.
        JSON is the guess made once, here, in the shape ``json.loads`` undoes.

        The dataset version is deliberately *not* a column here.
        :func:`~chip_chat.eval.dataset.versions.rows` adds it, because the
        version is computed from these rows and a row that carried it could not
        be part of computing it.
        """
        return {**self._columns(), DIGEST_COLUMN: self.digest}

    def _columns(self) -> Mapping[str, str | int | bool]:
        """Everything the entry says, and nothing derived from it."""
        return {
            ID_COLUMN: self.entry_id,
            "origin": self.origin.value,
            "input": self.input,
            "input_kind": self.input_kind.value,
            "expected_lane": self.expected_lane.value,
            "expected_tool": self.expected_tool,
            "scores_routing": self.scores_routing,
            "requirements": _canonical(list(self.requirements)),
            "checks": _canonical(list(self.checks)),
            "judged_checks": _canonical(list(self.judged_checks)),
            "persona": self.persona,
            "context": _canonical(list(self.context)),
            "confirmed": self.confirmed,
            "forbidden_tools": _canonical(list(self.forbidden_tools)),
            "menu_terms": _canonical(list(self.menu_terms)),
            "frame_truth": "" if self.frame is None else self.frame.as_json(),
            "why": self.why,
        }


def golden_entries(golden: GoldenSet) -> tuple[DatasetEntry, ...]:
    """Promote every golden case, in set order.

    Args:
        golden: The set, already loaded and therefore already coherent --
            :meth:`~chip_chat.eval.golden.cases.GoldenSet.load` refuses a
            manifest that contradicts itself, and nothing here re-checks it.

    Returns:
        One entry per case.
    """
    return tuple(_from_case(case) for case in golden)


def photo_entries(labels: LabeledSet) -> tuple[DatasetEntry, ...]:
    """Promote every labeled photograph, in set order.

    Args:
        labels: The labeled set, already loaded.

    Returns:
        One entry per frame. Empty where the set is, which is the state
        ``eval/photos/labels.json`` is in today and which the build reports
        rather than hides.
    """
    return tuple(_from_label(label) for label in labels)


def _from_case(case: GoldenCase) -> DatasetEntry:
    return DatasetEntry(
        entry_id=f"{GOLDEN_PREFIX}{case.case_id}",
        origin=Origin.GOLDEN,
        input=case.message,
        input_kind=InputKind.MESSAGE,
        expected_lane=case.lane,
        expected_tool="" if case.tool is None else case.tool.value,
        requirements=tuple(sorted(case.requirements)),
        checks=tuple(sorted(check.value for check in case.checks)),
        judged_checks=tuple(sorted(check.value for check in case.checks & JUDGED)),
        persona=case.persona,
        context=case.context,
        confirmed=case.confirmed,
        forbidden_tools=tuple(sorted(tool.value for tool in case.forbidden_tools)),
        menu_terms=case.menu_terms,
        why=case.why,
    )


def _from_label(label: PhotoLabel) -> DatasetEntry:
    """One frame, as ground truth for the vision lane.

    The requirements are the vision requirements the photo set is delegated,
    named on the row so that a coverage query over the dataset sees the vision
    lane covered by the entries that actually cover it. ``requirements.
    DELEGATIONS`` is where the argument for each of them lives; this is the
    same fact, carried where an experiment can read it.
    """
    return DatasetEntry(
        entry_id=f"{PHOTOS_PREFIX}{label.photo_id}",
        origin=Origin.PHOTOS,
        input=label.image,
        input_kind=InputKind.IMAGE,
        expected_lane=Lane.VISION,
        requirements=_frame_requirements(label),
        why=label.notes,
        frame=FrameTruth(
            is_chipotle_style=label.is_chipotle_style,
            meals_visible=label.meals_visible,
            conditions=tuple(sorted(condition.value for condition in label.conditions)),
            slots={
                slot.value: tuple(sorted(terms))
                for slot, terms in sorted(label.slots.items())
            },
            unreadable=tuple(sorted(slot.value for slot in label.unreadable)),
            photographer=label.capture.photographer,
            license=label.capture.license,
        ),
    )


_COMPONENT_REQUIREMENTS: Final = ("V2", "V3")
"""What every orderable frame is evidence for: the draft, and what was seen."""


def _frame_requirements(label: PhotoLabel) -> tuple[str, ...]:
    """Which vision requirements this frame is evidence for, sorted.

    Derived from the label rather than declared on it, because the photo set
    has no requirement field and inventing one would put the same fact in two
    files. Each clause below is the frame property the requirement is measured
    over, and they are the ones ``chip_chat.eval.photos.scoring`` already
    separates its tables by.
    """
    covered: set[str] = set()
    if label.orderable:
        covered.update(_COMPONENT_REQUIREMENTS)
    if not label.is_chipotle_style:
        covered.add("V4")
    if label.unreadable_required():
        covered.add("V5")
    if label.several_meals:
        covered.add("V7")
    return tuple(sorted(covered))


def _canonical(value: object) -> str:
    """JSON with sorted keys and no incidental whitespace.

    One spelling for one value, everywhere -- which is what lets a version be a
    hash of the rows rather than a number somebody remembers to increment.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(payload: str) -> str:
    """The first :data:`DIGEST_LENGTH` hex characters of ``payload``'s SHA-256.

    Shared with :mod:`chip_chat.eval.dataset.versions`, so that an entry's
    digest and a dataset's version are the same kind of thing computed the same
    way. Two spellings of "hash it and keep twelve characters" would be two
    places for one of them to drift.
    """
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]
