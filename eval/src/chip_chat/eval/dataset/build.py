"""Two manifests in, one versioned dataset out.

This is the whole of #72's *"reproducible from the repo, not a one-off manual
import"*: the dataset is a pure function of two committed files, so the same
clone produces the same version and a version can be traced back to a commit.
Nothing here reads the network, calls a model, or looks at a clock.

**The coverage travels with the dataset.** #72's first acceptance criterion asks
for *full requirement coverage visible*, and the honest reading of that is not
"go and run the coverage command as well" -- it is that the artifact says, of
itself, which PRD requirements its entries cover and which are measured
somewhere else. So :class:`Dataset` carries both sets' coverage and
:func:`document` writes it above the entries.

**Two refusals, both about a row that could not be evidence for anything.** An
entry with no requirement id would break #72's third acceptance criterion one
row at a time, and a duplicate entry id would silently make an experiment's
join ambiguous. The golden set's loader already forbids the first for cases;
the photo set has no requirement field at all -- a frame's requirements are
derived from what the frame *is* -- so the guarantee has to be made here, where
both halves are in the same room.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from chip_chat.eval.dataset.entries import (
    DatasetEntry,
    golden_entries,
    photo_entries,
)
from chip_chat.eval.dataset.versions import fingerprint
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.golden.coverage import Coverage as GoldenCoverage
from chip_chat.eval.golden.coverage import coverage as golden_coverage
from chip_chat.eval.photos.coverage import MINIMUM_PHOTOS
from chip_chat.eval.photos.coverage import Coverage as PhotoCoverage
from chip_chat.eval.photos.coverage import coverage as photo_coverage
from chip_chat.eval.photos.labels import LabeledSet

__all__ = [
    "DEFAULT_BUILD",
    "DEFAULT_DATASET_NAME",
    "Dataset",
    "DatasetError",
    "build_dataset",
    "document",
]

DEFAULT_DATASET_NAME: Final = "cilantro-golden-set"
"""What the dataset is called wherever it is uploaded.

One name, versioned underneath, rather than a name per version. A second
dataset called ``...-v2`` is how versioning dies: the comparison #73 exists to
make needs both runs pointed at one moving thing whose versions are ordered,
not at two unrelated things that happen to share a prefix.
"""

DEFAULT_BUILD: Final = Path("eval/dataset/DATASET.json")
"""Where the built dataset is committed, relative to the repository root.

Committed rather than generated on demand, because the version is the point.
Git then answers a question nothing else can: *which version was current when
this score was taken*. A test holds the file to what the sets currently build,
so it cannot drift into being a decoration.
"""


class DatasetError(ValueError):
    """A build that could not be believed as a dataset.

    Raised at build, never at upload. The same rule both sets already follow:
    a set that contradicts itself produces numbers that look exactly like
    numbers, and the point of failing early is that nobody gets to read one.
    """


@dataclass(frozen=True, slots=True)
class Dataset:
    """Every entry, the version they are, and what they do and do not cover.

    Attributes:
        name: What the dataset is called wherever it is uploaded.
        version: :func:`~chip_chat.eval.dataset.versions.fingerprint` of the
            entries. Changes when they do, and only then.
        entries: The rows, golden set first and in each set's own order.
        golden_source: The manifest the golden entries came from.
        photos_source: The manifest the frames came from.
        golden: The golden set's coverage against the PRD register and #29's
            shape clauses.
        photos: The labeled set's coverage against #56's scope.
    """

    name: str
    version: str
    entries: tuple[DatasetEntry, ...]
    golden_source: Path
    photos_source: Path
    golden: GoldenCoverage
    photos: PhotoCoverage

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def frames(self) -> int:
        """How many labeled photographs are in the dataset."""
        return sum(1 for entry in self.entries if entry.frame is not None)

    @property
    def full_requirement_coverage(self) -> bool:
        """Whether every PRD requirement is covered by an entry or delegated.

        #29's first acceptance criterion, read here as #72's: an uploaded
        version with a requirement nothing covers and nothing delegates is a
        dataset that will report a complete-looking score over an incomplete
        product.
        """
        return self.golden.every_requirement_covered


def build_dataset(
    golden: GoldenSet,
    labels: LabeledSet,
    *,
    name: str = DEFAULT_DATASET_NAME,
) -> Dataset:
    """Promote both sets into one versioned dataset.

    Args:
        golden: The golden set, already loaded.
        labels: The labeled photo set, already loaded. May be empty -- that is
            the state ``eval/photos/labels.json`` is in today, and a build that
            refused it would make the golden half hostage to photographs
            nobody has taken yet. :attr:`Dataset.photos` reports it instead.
        name: What the dataset is called where it is uploaded.

    Returns:
        The dataset, with its version already computed.

    Raises:
        DatasetError: If two entries share an id, or an entry covers no
            requirement.
    """
    entries = golden_entries(golden) + photo_entries(labels)
    _believable(entries)
    return Dataset(
        name=name,
        version=fingerprint(entries),
        entries=entries,
        golden_source=golden.source,
        photos_source=labels.root / "labels.json",
        golden=golden_coverage(golden),
        photos=photo_coverage(labels),
    )


def _believable(entries: Sequence[DatasetEntry]) -> None:
    """Refuse a set of entries that could not be joined against, or scored."""
    seen: set[str] = set()
    for entry in entries:
        if entry.entry_id in seen:
            raise DatasetError(f"duplicate entry id {entry.entry_id!r}")
        seen.add(entry.entry_id)
        if not entry.requirements:
            # A frame that is Chipotle-style, single-meal and yet not orderable
            # reaches here: the label loader permits `meals_visible: 0`, and
            # such a frame is evidence for no requirement anybody wrote down.
            # It is a labeling mistake rather than a dataset shape, so it is
            # named as one.
            raise DatasetError(
                f"{entry.entry_id}: covers no PRD requirement, so nothing it "
                "scores could be reported against one"
            )


def document(dataset: Dataset) -> str:
    """The dataset as the JSON artifact that is committed.

    The rows are the payload; everything above them is what a reader needs in
    order to know what the payload is worth. Sorted keys and a trailing
    newline, so that the file a build writes is byte-identical to the file the
    last build wrote when nothing changed -- which is what makes a committed
    artifact a staleness check rather than a source of diff noise.

    Args:
        dataset: What to write.

    Returns:
        The document, ending in a newline.
    """
    payload: dict[str, Any] = {
        "name": dataset.name,
        "version": dataset.version,
        "built_from": [
            _under_eval(dataset.golden_source),
            _under_eval(dataset.photos_source),
        ],
        "entry_count": len(dataset),
        "coverage": _coverage(dataset),
        "entries": [dict(entry.row()) for entry in dataset.entries],
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


EVAL_DIRECTORY: Final = "eval"
"""The directory every manifest a dataset is built from lives under."""


def _under_eval(source: Path) -> str:
    """``source`` from its :data:`EVAL_DIRECTORY` component onward.

    The document is committed, so a field that moved with the checkout
    directory would make two clones of one commit build two different files --
    which is precisely the drift the version exists to detect, arriving through
    the artifact that reports it. Both manifests live under ``eval/``, so
    trimming to that component gives the repository-relative path whether the
    loader was handed an absolute path or a relative one.

    A path with no such component is rendered as given. That is a manifest from
    somewhere else, which is a thing ``--golden`` permits and a thing the
    document should say plainly rather than tidy away.
    """
    parts = source.parts
    if EVAL_DIRECTORY not in parts:
        return str(source)
    return "/".join(parts[len(parts) - 1 - parts[::-1].index(EVAL_DIRECTORY) :])


def _coverage(dataset: Dataset) -> dict[str, Any]:
    """Requirement coverage, tool coverage and both sets' unmet scope clauses.

    Delegations are kept apart from covered requirements rather than folded in,
    which is the argument :mod:`chip_chat.eval.golden.coverage` makes at
    length: fold them together and the vision lane looks scored here when it is
    scored over the frames; call them uncovered and a complete set can never go
    green, so nobody reads the report.
    """
    return {
        "requirements_covered": {
            item.id: list(ids) for item, ids in dataset.golden.covered
        },
        "requirements_delegated": {
            item.id: delegation.target for item, delegation in dataset.golden.delegated
        },
        "requirements_uncovered": [item.id for item in dataset.golden.uncovered],
        "tools_without_an_entry": [
            tool.value for tool in dataset.golden.tools_without_a_case
        ],
        "golden_scope_unmet": [shape.name for shape, _ in dataset.golden.unmet],
        "photos": {
            "frames": dataset.frames,
            "minimum": MINIMUM_PHOTOS,
            "scope_unmet": [item.name for item, _ in dataset.photos.unmet],
        },
    }
