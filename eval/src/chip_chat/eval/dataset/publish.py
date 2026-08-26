"""Create the dataset, or add a version to it. There is no third thing.

#72 asks for *versioning discipline: adding entries creates a new version rather
than mutating the old one, so a score from three weeks ago still means
something*. :func:`publish` is where that sentence becomes a program, and it
does three things:

**It never sends a row the dataset already has.** A publish that re-uploaded the
whole set every time would fill the dataset with copies and make "which examples
were in version 4" unanswerable. So the new rows are the difference, computed
here, against what the store says it holds.

**It refuses a changed entry rather than uploading it.** This is the part worth
arguing for, because the alternative looks so reasonable. If ``k1-ingredients-
bowl`` asks a different question this week than last week, then every score
recorded against ``k1-ingredients-bowl`` before this week measured a question
that no longer exists -- and nothing in the dataset, the experiment or the chart
will say so. The fix is not a warning: it is that a changed question is a new
question and gets a new id. :class:`PublishError` says exactly that, and names
the entries.

**It will not publish a set with an uncovered PRD requirement.** #29's first
acceptance criterion, enforced at the boundary #72 introduced. An uploaded
dataset is the thing #73, #74 and #75 will run experiments against and quote
numbers from; a requirement nothing covers and nothing delegates is a hole those
numbers cannot see, and the moment to notice it is before the upload rather than
during a demo.

Publishing the same build twice is a no-op that says so, rather than an error.
Re-running a command after a network failure is a thing people do, and a store
that has already got the rows has already got them.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from chip_chat.eval.dataset.build import Dataset
from chip_chat.eval.dataset.entries import DIGEST_COLUMN, ID_COLUMN, DatasetEntry
from chip_chat.eval.dataset.store import DatasetStore, Row
from chip_chat.eval.dataset.versions import rows

__all__ = ["Publication", "PublishError", "publish"]


class PublishError(RuntimeError):
    """A build that must not be uploaded on top of what is already there."""


@dataclass(frozen=True, slots=True)
class Publication:
    """What a publish did.

    Attributes:
        dataset: The dataset's name.
        version: The build's fingerprint -- what
            :attr:`~chip_chat.eval.dataset.build.Dataset.version` said.
        store: What the store calls itself, for the report.
        store_version: What the store called what it created -- a version on a
            later publish, and the dataset itself on the first one, where the
            first version is the dataset. Empty where nothing was created.
        created: Whether the dataset itself was made by this publish.
        added: The entry ids uploaded, in build order.
        already_present: How many entries the dataset already held.
    """

    dataset: str
    version: str
    store: str
    store_version: str
    created: bool
    added: tuple[str, ...]
    already_present: int

    @property
    def changed_anything(self) -> bool:
        """Whether this publish put anything in the store."""
        return bool(self.added)


def publish(dataset: Dataset, store: DatasetStore) -> Publication:
    """Put ``dataset`` in ``store``, creating it or adding a version.

    Args:
        dataset: The built dataset.
        store: Where to put it.

    Returns:
        What happened. A build already fully present produces a
        :class:`Publication` with no ``added`` and an empty ``store_version``.

    Raises:
        PublishError: If a PRD requirement is uncovered, if the dataset is
            empty, or if an entry already in the store would have to change to
            accommodate this build.
    """
    if not dataset.entries:
        raise PublishError("the dataset is empty; there is nothing to publish")
    if not dataset.full_requirement_coverage:
        uncovered = ", ".join(item.id for item in dataset.golden.uncovered)
        raise PublishError(
            f"{uncovered} covered by nothing and delegated nowhere; a published "
            "version is what experiments quote numbers from, and a hole in it "
            "is a hole those numbers cannot see"
        )

    held = store.existing(dataset.name)
    if held is None:
        return Publication(
            dataset=dataset.name,
            version=dataset.version,
            store=store.name,
            store_version=store.create(
                dataset.name, rows(dataset.entries, dataset.version)
            ),
            created=True,
            added=tuple(entry.entry_id for entry in dataset.entries),
            already_present=0,
        )

    digests = _digests(held)
    _unchanged(dataset.entries, digests)
    fresh = tuple(entry for entry in dataset.entries if entry.entry_id not in digests)
    return Publication(
        dataset=dataset.name,
        version=dataset.version,
        store=store.name,
        store_version=(
            "" if not fresh else store.add(dataset.name, rows(fresh, dataset.version))
        ),
        created=False,
        added=tuple(entry.entry_id for entry in fresh),
        already_present=len(dataset.entries) - len(fresh),
    )


def _digests(held: Sequence[Row]) -> Mapping[str, str]:
    """What the store holds, as entry id to content digest.

    A row missing either column is skipped rather than refused. The dataset may
    legitimately hold examples this package did not put there -- an interesting
    production trace promoted into the set, which is exactly what the system
    design's Phase 9 asks for -- and those are none of a publish's business.
    """
    return {
        str(row[ID_COLUMN]): str(row[DIGEST_COLUMN])
        for row in held
        if ID_COLUMN in row and DIGEST_COLUMN in row
    }


def _unchanged(entries: Sequence[DatasetEntry], digests: Mapping[str, str]) -> None:
    """Refuse any entry whose content differs from the published one.

    Raises:
        PublishError: Naming every changed entry rather than the first. A set
            edited in bulk produces a list, and finding out about the second
            one only after fixing the first is a way to spend an evening.
    """
    changed = tuple(
        entry.entry_id
        for entry in entries
        if entry.entry_id in digests and digests[entry.entry_id] != entry.digest
    )
    if not changed:
        return
    raise PublishError(
        f"{', '.join(changed)}: already published with different content. A "
        "published entry is what old scores were taken against, so it cannot "
        "be edited in place -- give the changed entry a new id, and the old "
        "scores keep meaning what they meant"
    )
