"""The build, the version, and the committed file that has to agree with both.

The version is the point of #72, and a version is only worth having if two
things are true of it: the same manifests always produce it, and the repository
cannot disagree with itself about what it currently is. Both are checked here,
and neither costs a model call.
"""

from pathlib import Path

import pytest

from chip_chat.eval.dataset.build import (
    DEFAULT_BUILD,
    Dataset,
    DatasetError,
    build_dataset,
    document,
)
from chip_chat.eval.dataset.entries import GOLDEN_PREFIX, PHOTOS_PREFIX
from chip_chat.eval.dataset.versions import VERSION_COLUMN, fingerprint, rows
from chip_chat.eval.golden.cases import GoldenSet
from chip_chat.eval.photos.labels import LabeledSet


def test_the_build_holds_both_sets(golden: GoldenSet, labels: LabeledSet) -> None:
    built = build_dataset(golden, labels)

    assert len(built) == len(golden) + len(labels)
    assert built.frames == len(labels)
    assert sum(1 for entry in built.entries if entry.frame is None) == len(golden)


def test_the_golden_entries_come_first(golden: GoldenSet, labels: LabeledSet) -> None:
    """So that a reader of the JSON meets the set the ticket is about."""
    built = build_dataset(golden, labels)
    ids = [entry.entry_id for entry in built.entries]

    assert all(item.startswith(GOLDEN_PREFIX) for item in ids[: len(golden)])
    assert all(item.startswith(PHOTOS_PREFIX) for item in ids[len(golden) :])


def test_the_same_manifests_build_the_same_version(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """No clock, no hash seed, no dict ordering. Twice is the cheapest check."""
    assert build_dataset(golden, labels).version == build_dataset(golden, labels).version


def test_adding_an_entry_moves_the_version(
    tmp_path: Path, golden: GoldenSet, labels: LabeledSet
) -> None:
    """#72's versioning discipline, stated as arithmetic rather than as a rule."""
    before = build_dataset(golden, labels)
    grown = GoldenSet(cases=golden.cases[:-1], source=golden.source)

    assert build_dataset(grown, labels).version != before.version


def test_editing_an_entry_moves_the_version(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """The failure mode a hand-maintained ordinal has and a hash does not."""
    from dataclasses import replace

    before = build_dataset(golden, labels)
    edited = GoldenSet(
        cases=(replace(golden.cases[0], message="something else"), *golden.cases[1:]),
        source=golden.source,
    )

    assert build_dataset(edited, labels).version != before.version


def test_reordering_the_set_moves_the_version(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """A set a reader meets in a different order is a different set to read."""
    before = build_dataset(golden, labels)
    shuffled = GoldenSet(
        cases=(golden.cases[1], golden.cases[0], *golden.cases[2:]),
        source=golden.source,
    )

    assert build_dataset(shuffled, labels).version != before.version


def test_the_build_refuses_a_duplicate_entry_id(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """Two rows with one id make an experiment's join ambiguous, silently."""
    doubled = GoldenSet(cases=(*golden.cases, golden.cases[0]), source=golden.source)

    with pytest.raises(DatasetError, match="duplicate entry id"):
        build_dataset(doubled, labels)


def test_the_build_refuses_an_entry_covering_no_requirement(
    tmp_path: Path, golden: GoldenSet
) -> None:
    """A frame that is Chipotle-style, not orderable and not multi-meal.

    The label loader permits ``meals_visible: 0``, and such a frame is evidence
    for no PRD requirement anybody wrote down -- so it would land in the
    dataset as a row #72's third acceptance criterion is false of.
    """
    manifest = tmp_path / "labels.json"
    manifest.write_text(
        '{"photos": [{"id": "empty-tray", "image": "images/x.jpg", '
        '"capture": {"photographer": "someone", "license": "CC0-1.0"}, '
        '"meals_visible": 0}]}',
        encoding="utf-8",
    )

    with pytest.raises(DatasetError, match="covers no PRD requirement"):
        build_dataset(golden, LabeledSet.load(manifest))


def test_the_rows_carry_the_version(golden: GoldenSet, labels: LabeledSet) -> None:
    """Which is what makes a published example say where it came from."""
    built = build_dataset(golden, labels)

    for row in rows(built.entries, built.version):
        assert row[VERSION_COLUMN] == built.version


def test_the_version_is_not_a_column_the_version_is_computed_over(
    golden: GoldenSet, labels: LabeledSet
) -> None:
    """A row that carried its own fingerprint could not be fingerprinted."""
    built = build_dataset(golden, labels)

    assert VERSION_COLUMN not in built.entries[0].row()
    assert fingerprint(built.entries) == built.version


def test_the_document_is_stable(shipped: Dataset) -> None:
    """Byte-identical between two builds, or the committed file is diff noise."""
    assert document(shipped) == document(shipped)


def test_the_document_shows_the_requirement_coverage(shipped: Dataset) -> None:
    """#72's first acceptance criterion: full requirement coverage, visible.

    Visible *in the artifact*. "Run the coverage command as well" is not the
    same promise -- an uploaded dataset outlives the shell it was uploaded
    from.
    """
    import json

    payload = json.loads(document(shipped))
    covered = payload["coverage"]["requirements_covered"]
    delegated = payload["coverage"]["requirements_delegated"]

    assert payload["coverage"]["requirements_uncovered"] == []
    assert covered
    assert delegated
    for identifier, entry_ids in covered.items():
        assert entry_ids, identifier


def test_the_committed_build_is_what_the_manifests_build(
    repo_root: Path, shipped: Dataset
) -> None:
    """The staleness gate, and the reason ``DATASET.json`` is committed at all.

    Adding a golden case changes the version. A version that only moves when
    somebody remembers to regenerate a file is not a version, so this test is
    what turns ``make dataset`` from advice into a step.
    """
    committed = (repo_root / DEFAULT_BUILD).read_text(encoding="utf-8")

    assert committed == document(shipped), "run `make dataset` and commit the result"


def test_the_document_does_not_depend_on_where_the_clone_lives(
    tmp_path: Path, repo_root: Path, shipped: Dataset
) -> None:
    """Two clones of one commit have to build the same bytes.

    The manifests are loaded here by an absolute path and in a shell by a
    relative one, and a ``built_from`` field that recorded either verbatim
    would make the committed artifact differ between two checkouts of the same
    code -- the exact drift the version exists to detect, arriving through the
    file that reports it.
    """
    copied = tmp_path / "elsewhere"
    (copied / "eval" / "golden").mkdir(parents=True)
    (copied / "eval" / "photos").mkdir(parents=True)
    for relative in ("eval/golden/cases.json", "eval/photos/labels.json"):
        (copied / relative).write_text(
            (repo_root / relative).read_text(encoding="utf-8"), encoding="utf-8"
        )

    moved = build_dataset(
        GoldenSet.load(copied / "eval" / "golden" / "cases.json"),
        LabeledSet.load(copied / "eval" / "photos" / "labels.json"),
    )

    assert document(moved) == document(shipped)
