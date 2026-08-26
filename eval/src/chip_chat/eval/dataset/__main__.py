"""``python -m chip_chat.eval.dataset`` -- build it, write it, upload it.

#72's fourth scope clause is *a documented path from a local ``eval/`` file to an
uploaded dataset version, runnable by one command*. This is the command, and the
path is three steps long:

.. code-block:: console

    $ python -m chip_chat.eval.dataset --check          # free, and CI runs it
    $ python -m chip_chat.eval.dataset --write          # refresh DATASET.json
    $ export ARIZE_API_KEY=... ARIZE_SPACE_ID=...
    $ python -m chip_chat.eval.dataset --upload         # create, or add a version

``--check`` builds the dataset from the two committed manifests, prints the
version and the requirement coverage, and fails if the committed
``eval/dataset/DATASET.json`` is not what the manifests currently build. That
last check is the one that earns its keep: adding a golden case changes the
version, and a version that only changes when somebody remembers to regenerate a
file is not a version. It calls nothing and costs nothing.

``--upload`` needs a space and a key, and does exactly one of two things: create
the dataset, or add a version holding the entries the dataset does not have yet.
It refuses to edit an entry that is already published -- see
:mod:`chip_chat.eval.dataset.publish` for why that refusal is the whole point --
and it refuses to publish a set with an uncovered PRD requirement.

``--catalog`` is optional and does here what it does for both sets separately:
checks the menu terms the golden cases lean on against a build the deployment
actually serves, so a dataset is not uploaded against a menu that has moved.
"""

import argparse
import sys
from pathlib import Path

from chip_chat.catalog import load_catalog
from chip_chat.eval.dataset.build import (
    DEFAULT_BUILD,
    DEFAULT_DATASET_NAME,
    Dataset,
    DatasetError,
    build_dataset,
    document,
)
from chip_chat.eval.dataset.publish import PublishError, publish
from chip_chat.eval.dataset.store import StoreError, arize_store_from_env
from chip_chat.eval.golden.cases import DEFAULT_MANIFEST as GOLDEN_MANIFEST
from chip_chat.eval.golden.cases import CaseError, GoldenSet
from chip_chat.eval.photos.coverage import MINIMUM_PHOTOS
from chip_chat.eval.photos.labels import LabeledSet, LabelError
from chip_chat.harvest.blobs import LocalBlobStore

PHOTOS_MANIFEST = Path("eval/photos/labels.json")
DEFAULT_CATALOG_PREFIX = "catalog"


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` on success; ``1`` where a manifest cannot be
        believed, the committed build is stale, a PRD requirement is
        uncovered, or an upload was refused. A stale committed build exits
        non-zero deliberately: it means the version in the repository is not
        the version the sets describe, and a wrong version is worse than none.
    """
    args = _parser().parse_args(argv)
    try:
        golden = GoldenSet.load(args.golden)
        labels = LabeledSet.load(args.photos)
    except (CaseError, LabelError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.catalog is not None:
        catalog = load_catalog(LocalBlobStore(args.catalog), args.catalog_prefix)
        try:
            golden.against(catalog)
        except CaseError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    try:
        dataset = build_dataset(golden, labels, name=args.name)
    except DatasetError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    _summary(dataset, checked_terms=args.catalog is not None)

    if args.write:
        args.build.parent.mkdir(parents=True, exist_ok=True)
        args.build.write_text(document(dataset), encoding="utf-8")
        print(f"wrote {args.build}")
    elif not _current(dataset, args.build):
        return 1

    if not args.upload:
        return 0 if dataset.full_requirement_coverage else 1

    try:
        done = publish(dataset, arize_store_from_env())
    except (PublishError, StoreError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    verb = "created" if done.created else "added a version to"
    if not done.changed_anything:
        print(f"{done.store} already holds {done.dataset} at {done.version}")
        return 0
    print(
        f"{verb} {done.dataset} in {done.store}: {len(done.added)} entries "
        f"uploaded at version {done.version} ({done.store_version}), "
        f"{done.already_present} already there"
    )
    return 0


def _summary(dataset: Dataset, *, checked_terms: bool) -> None:
    """Print what was built, and what it does and does not cover."""
    print(f"{dataset.name} {dataset.version}: {len(dataset)} entries")
    print(
        f"  {len(dataset) - dataset.frames} from {dataset.golden_source}, "
        f"{dataset.frames} from {dataset.photos_source} "
        f"(need {MINIMUM_PHOTOS})"
    )
    if not checked_terms:
        print(
            "note: no --catalog, so the menu terms the set leans on were not "
            "checked against a build"
        )
    cover = dataset.golden
    print(
        f"  {len(cover.covered)} requirements covered by an entry, "
        f"{len(cover.delegated)} measured elsewhere, "
        f"{len(cover.uncovered)} uncovered"
    )
    for item in cover.uncovered:
        print(f"  UNCOVERED {item.id}: {item.text}")
    for tool in cover.tools_without_a_case:
        print(f"  NO ENTRY  expects {tool.value}")
    for shape, ids in cover.unmet:
        print(f"  MISSING   {shape.name}: {len(ids)}/{shape.minimum}")
    for clause, frames in dataset.photos.unmet:
        print(f"  MISSING   frames -- {clause.name}: {len(frames)}/{clause.minimum}")


def _current(dataset: Dataset, build: Path) -> bool:
    """Whether the committed build is what the manifests currently produce.

    The staleness gate. Compared as text rather than by version, so that a
    change to the document's shape is caught as well as a change to its
    content -- both make the committed file stop describing what is in the
    repository.
    """
    wanted = document(dataset)
    try:
        found = build.read_text(encoding="utf-8")
    except OSError:
        print(
            f"error: {build} is missing; run --write to build it",
            file=sys.stderr,
        )
        return False
    if found == wanted:
        return True
    print(
        f"error: {build} is stale -- the manifests now build version "
        f"{dataset.version}. Run --write and commit the result",
        file=sys.stderr,
    )
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.eval.dataset",
        description="Build the versioned evaluation dataset, and upload it.",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=GOLDEN_MANIFEST,
        help=f"the golden set's manifest (default: {GOLDEN_MANIFEST})",
    )
    parser.add_argument(
        "--photos",
        type=Path,
        default=PHOTOS_MANIFEST,
        help=f"the labeled photo set's manifest (default: {PHOTOS_MANIFEST})",
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_DATASET_NAME,
        help=f"what the dataset is called (default: {DEFAULT_DATASET_NAME})",
    )
    parser.add_argument(
        "--build",
        type=Path,
        default=DEFAULT_BUILD,
        help=f"the committed build (default: {DEFAULT_BUILD})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="build and report; the default, and named so a command can say so",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the committed build instead of checking it",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="create the dataset, or add a version holding the new entries",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        help="directory holding a built catalogue, to check the menu terms against",
    )
    parser.add_argument(
        "--catalog-prefix",
        default=DEFAULT_CATALOG_PREFIX,
        help=f"key prefix within it (default: {DEFAULT_CATALOG_PREFIX})",
    )
    return parser


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    raise SystemExit(main())
