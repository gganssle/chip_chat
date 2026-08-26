"""Issue #56's scope, as checks the set either passes or fails.

The scope is written as prose -- *"at least 30 photographs"*, *"deliberately
include the hard cases"*, *"at least a few frames containing several meals"* --
and prose in a ticket is not something a set can be measured against six months
later, when the ticket is closed and somebody has added four easy frames because
the number was looking low. So the scope lives here instead, one
:class:`Requirement` per clause, and :func:`coverage` reports which of them the
set on disk actually meets.

This is what makes the set falsifiable in the other direction. The scorer says
how well the pipeline did on the frames it was given; this says whether those
frames were the ones the ticket asked for. A set of thirty clean overhead bowls
would score beautifully and mean nothing, and that failure is invisible to any
precision figure.

Two of the requirements are not #56's own. ``docs/decisions/multi-meal-photos.md``
added them when it decided V0 declines on a multi-meal frame: at least three
such frames, and at least one bowl-next-to-a-bag-of-chips, *"the likeliest false
positive, and it fires on the most ordinary photo anyone will send"*. They are
marked with their source, because a requirement whose reason is in another
document is one somebody will otherwise delete as arbitrary.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from chip_chat.catalog.records import Slot
from chip_chat.eval.photos.labels import Condition, LabeledSet, PhotoLabel

__all__ = ["MINIMUM_PHOTOS", "REQUIREMENTS", "Coverage", "Requirement", "coverage"]

MINIMUM_PHOTOS: Final = 30
"""Issue #56's first acceptance criterion: *30+ labeled photos*."""


@dataclass(frozen=True, slots=True)
class Requirement:
    """One clause of the scope, and how to count the frames that satisfy it.

    Attributes:
        name: How the report names it.
        minimum: How many frames must satisfy it.
        source: Which document asks for it. Carried so that a reader deciding
            whether a requirement still applies can go and read the argument
            rather than guess at it.
        satisfied_by: The test one frame either passes or does not.
    """

    name: str
    minimum: int
    source: str
    satisfied_by: Callable[[PhotoLabel], bool]

    def met_by(self, labels: Sequence[PhotoLabel]) -> tuple[str, ...]:
        """The ids of the frames satisfying this requirement, in set order."""
        return tuple(label.photo_id for label in labels if self.satisfied_by(label))


def _has(condition: Condition) -> Callable[[PhotoLabel], bool]:
    """A test for one condition being present on a frame."""
    return lambda label: condition in label.conditions


def _vessel(term: str) -> Callable[[PhotoLabel], bool]:
    """A test for a frame whose labeled vessel is ``term``."""
    return lambda label: term in label.slots.get(Slot.VESSEL, ())


def _protein(term: str) -> Callable[[PhotoLabel], bool]:
    """A test for a frame whose labeled protein is ``term``."""
    return lambda label: term in label.slots.get(Slot.PROTEIN, ())


REQUIREMENTS: Final[tuple[Requirement, ...]] = (
    Requirement(
        name="clean single-meal frames",
        minimum=10,
        source="#56 scope",
        # Not itself a hard case, and the set still needs a floor of them: with
        # too few, a poor score cannot be told apart from a set made entirely of
        # frames nobody could read either.
        satisfied_by=_has(Condition.CLEAN),
    ),
    Requirement(
        name="poor lighting",
        minimum=2,
        source="#56 scope",
        satisfied_by=_has(Condition.LOW_LIGHT),
    ),
    Requirement(
        name="partially eaten meals",
        minimum=2,
        source="#56 scope",
        satisfied_by=_has(Condition.PARTIALLY_EATEN),
    ),
    Requirement(
        name="contents not visible (a wrapped burrito)",
        minimum=2,
        source="#56 scope",
        satisfied_by=_has(Condition.CONTENTS_HIDDEN),
    ),
    Requirement(
        name="food that is not Chipotle at all",
        minimum=2,
        source="#56 scope, PRD V4",
        satisfied_by=_has(Condition.NOT_CHIPOTLE),
    ),
    Requirement(
        name="several meals in one frame",
        minimum=3,
        source="docs/decisions/multi-meal-photos.md, #58 AC",
        satisfied_by=_has(Condition.MULTI_MEAL),
    ),
    Requirement(
        name="one meal beside a side",
        minimum=1,
        source="docs/decisions/multi-meal-photos.md",
        satisfied_by=_has(Condition.MEAL_WITH_SIDE),
    ),
    Requirement(
        name="a required slot the photograph does not answer",
        minimum=2,
        source="#56 scope, PRD V5",
        # The clarify path. Distinct from `contents_hidden`, which is one way to
        # get here: a salsa buried under cheese is another, and the behaviour
        # being measured is the same one either way.
        satisfied_by=lambda label: bool(label.unreadable_required()),
    ),
    Requirement(
        name="bowls",
        minimum=4,
        source="#56 per-slot breakdown",
        satisfied_by=_vessel("bowl"),
    ),
    Requirement(
        name="burritos",
        minimum=4,
        source="#56 per-slot breakdown",
        # Both vessels, because vessel is the slot the matcher pairs with
        # protein to reach a SKU at all -- a set of only bowls would score the
        # vessel slot perfectly while proving nothing about it.
        satisfied_by=_vessel("burrito"),
    ),
    Requirement(
        name="chicken",
        minimum=3,
        source="#56 per-slot breakdown",
        satisfied_by=_protein("chicken"),
    ),
    Requirement(
        name="steak",
        minimum=3,
        source="#56 per-slot breakdown",
        # Protein carries the highest floor in the matcher because a wrong one
        # is a different meal at a different price. A set that never varies it
        # cannot say whether that floor is right.
        satisfied_by=_protein("steak"),
    ),
)
"""Every clause of the scope. Order is the order the report prints them in."""


@dataclass(frozen=True, slots=True)
class Coverage:
    """Whether a set is the set the ticket asked for.

    Attributes:
        photos: How many frames the set holds.
        met: Requirements the set satisfies, with the ids that satisfy them.
        unmet: Requirements it does not, likewise -- the ids are carried for
            both, because "two of three" is more useful with the two named.
    """

    photos: int
    met: tuple[tuple[Requirement, tuple[str, ...]], ...]
    unmet: tuple[tuple[Requirement, tuple[str, ...]], ...]

    @property
    def enough_photos(self) -> bool:
        """Whether the set reaches :data:`MINIMUM_PHOTOS`."""
        return self.photos >= MINIMUM_PHOTOS

    @property
    def complete(self) -> bool:
        """Whether the set meets the count and every requirement."""
        return self.enough_photos and not self.unmet


def coverage(labels: LabeledSet) -> Coverage:
    """Check a set against :data:`REQUIREMENTS`.

    Args:
        labels: The set, already loaded.

    Returns:
        The :class:`Coverage`. Never raises: an incomplete set is a fact to
        report next to the scores, not a reason to refuse to compute them --
        a partial set scored and labeled partial is more useful than no number
        at all, so long as nobody can read the number without the label.
    """
    met: list[tuple[Requirement, tuple[str, ...]]] = []
    unmet: list[tuple[Requirement, tuple[str, ...]]] = []
    for requirement in REQUIREMENTS:
        ids = requirement.met_by(labels.photos)
        (met if len(ids) >= requirement.minimum else unmet).append((requirement, ids))
    return Coverage(photos=len(labels), met=tuple(met), unmet=tuple(unmet))
