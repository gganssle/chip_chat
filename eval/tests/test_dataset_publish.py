"""Create it, add a version, or refuse. The three things a publish can do.

Everything #72 asks for that is not arithmetic is here, and all of it is about
the *second* publish -- which is why these run against
:class:`~chip_chat.eval.dataset.testing.RecordingStore` rather than a stub. A
store that does not remember the first publish cannot be asked what the second
one did.
"""

from dataclasses import replace

import pytest

from chip_chat.eval.dataset.build import build_dataset
from chip_chat.eval.dataset.publish import PublishError, publish
from chip_chat.eval.dataset.testing import RecordingStore, row_by_id
from chip_chat.eval.dataset.versions import VERSION_COLUMN
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.photos.labels import LabeledSet


def _dataset(golden: GoldenSet, labels: LabeledSet, cases: int | None = None):
    """The dataset built from the first ``cases`` golden cases."""
    trimmed = golden if cases is None else GoldenSet(golden.cases[:cases], golden.source)
    return build_dataset(trimmed, labels)


def test_a_first_publish_creates_the_dataset(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    built = _dataset(golden, labels)
    store = RecordingStore()

    done = publish(built, store)

    assert done.created
    assert len(done.added) == len(built)
    assert done.already_present == 0
    assert len(store.rows(built.name)) == len(built)


def test_every_published_row_carries_the_version(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """The join that makes an old score legible."""
    built = _dataset(golden, labels)
    store = RecordingStore()

    publish(built, store)

    for row in store.rows(built.name):
        assert row[VERSION_COLUMN] == built.version


def test_republishing_the_same_build_changes_nothing(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """Re-running a command after a network failure is a thing people do."""
    built = _dataset(golden, labels)
    store = RecordingStore()
    publish(built, store)

    again = publish(built, store)

    assert not again.created
    assert not again.changed_anything
    assert again.store_version == ""
    assert again.already_present == len(built)
    assert len(store.versions[built.name]) == 1


def test_adding_entries_adds_a_version_holding_only_the_new_ones(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """#72's versioning discipline, as the thing the store is actually asked.

    A publish that re-uploaded the whole set would fill the dataset with copies
    and make *which examples were in version 4* unanswerable.
    """
    store = RecordingStore()
    first = _dataset(golden, labels, cases=len(golden) - 2)
    publish(first, store)
    grown = _dataset(golden, labels)

    done = publish(grown, store)

    assert not done.created
    assert done.version == grown.version != first.version
    assert done.already_present == len(first)
    assert len(done.added) == len(grown) - len(first)
    assert len(store.versions[grown.name]) == 2
    assert len(store.versions[grown.name][1]) == len(done.added)


def test_the_new_rows_carry_the_build_they_arrived_in(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """And the old rows keep the build that first published them."""
    store = RecordingStore()
    first = _dataset(golden, labels, cases=len(golden) - 2)
    publish(first, store)
    grown = _dataset(golden, labels)
    publish(grown, store)

    published = row_by_id(store.rows(grown.name))
    unchanged = first.entries[0].entry_id
    fresh = grown.entries[len(first) - len(labels)].entry_id

    assert published[unchanged][VERSION_COLUMN] == first.version
    assert published[fresh][VERSION_COLUMN] == grown.version


def test_an_edited_entry_is_refused(golden: GoldenSet, labels: LabeledSet) -> None:
    """The refusal the whole package exists for.

    A question edited in place makes every score taken against it before the
    edit a measurement of something nobody can see any more, and nothing
    downstream would say so.
    """
    store = RecordingStore()
    built = _dataset(golden, labels)
    publish(built, store)
    edited = replace(
        built,
        entries=(
            replace(built.entries[0], input="a different question"),
            *built.entries[1:],
        ),
    )

    with pytest.raises(PublishError, match="already published with different content"):
        publish(edited, store)


def test_the_refusal_names_every_edited_entry(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """A set edited in bulk produces a list, not a first offender."""
    store = RecordingStore()
    built = _dataset(golden, labels)
    publish(built, store)
    edited = replace(
        built,
        entries=(
            *(replace(entry, why="rewritten") for entry in built.entries[:3]),
            *built.entries[3:],
        ),
    )

    with pytest.raises(PublishError) as caught:
        publish(edited, store)

    for entry in built.entries[:3]:
        assert entry.entry_id in str(caught.value)


def test_an_edited_entry_under_a_new_id_publishes(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """The way out, and the reason the refusal is not an obstruction.

    A changed question is a new question. The old id keeps the scores it
    earned, and the new one starts collecting its own.
    """
    store = RecordingStore()
    built = _dataset(golden, labels)
    publish(built, store)
    renamed = replace(built.entries[0], entry_id="golden/asked-differently")
    grown = replace(built, entries=(*built.entries, renamed))

    done = publish(grown, store)

    assert done.added == ("golden/asked-differently",)


def test_an_uncovered_requirement_refuses_the_upload(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """#29's first acceptance criterion, enforced where it now costs something.

    A published dataset is what #73, #74 and #75 quote numbers from, and a
    requirement covered by nothing and delegated nowhere is a hole those
    numbers cannot see.
    """
    thin = _dataset(golden, labels, cases=2)

    assert not thin.full_requirement_coverage
    with pytest.raises(PublishError, match="covered by nothing and delegated nowhere"):
        publish(thin, RecordingStore())


def test_an_empty_dataset_refuses_the_upload(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    built = replace(_dataset(golden, labels), entries=())

    with pytest.raises(PublishError, match="nothing to publish"):
        publish(built, RecordingStore())


def test_rows_the_publish_did_not_write_are_left_alone(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """An interesting production trace, promoted into the set by hand.

    Phase 9 of the system design asks for exactly that -- *let the interesting
    traces flow back into the dataset so the golden set grows from real usage*
    -- and a publish that choked on a row it did not recognise would make it
    impossible.
    """
    store = RecordingStore()
    built = _dataset(golden, labels)
    publish(built, store)
    store.versions[built.name].append(({"input": "something a visitor asked"},))

    again = publish(built, store)

    assert not again.changed_anything
