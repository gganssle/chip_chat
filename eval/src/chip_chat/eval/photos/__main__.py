"""``python -m chip_chat.eval.photos`` -- check the set, or run and score it.

Two modes, and the cheap one is the default for a reason.

``--check`` reads the manifest, validates every label against itself and
against the generated vocabulary where one is configured, reports which of
issue #56's scope requirements the set meets, and says which frames are not on
disk. It calls no model and costs nothing, so it is the thing to run in CI and
the thing to run after adding a photograph.

Without ``--check`` it runs every frame through a real lane and writes the
baseline. That spends money, one vision call per frame, which is why the run
refuses to start on a set whose files are missing or whose labels do not match
the catalogue: an experiment should fail on the free check rather than after
the twenty-ninth call.

.. code-block:: console

    $ python -m chip_chat.eval.photos --check
    $ export CHIP_CHAT_VISION_VOCABULARY=chip_chat.vision_vocabulary
    $ export CHIP_CHAT_FOUNDRY_ENDPOINT=... CHIP_CHAT_FOUNDRY_API_KEY=...
    $ python -m chip_chat.eval.photos --catalog ./catalog-build --out BASELINE.md

The floors come from ``CHIP_CHAT_MATCHER_*`` and are recorded in the report, so
two runs at two settings produce two documents that can be diffed. That is the
whole of "a prompt or model change can be scored rather than eyeballed": change
one thing, run it again, diff the baseline.
"""

import argparse
import os
import sys
from pathlib import Path

from chip_chat.catalog import load_catalog
from chip_chat.eval.photos.coverage import MINIMUM_PHOTOS, coverage
from chip_chat.eval.photos.labels import LabeledSet, LabelError
from chip_chat.eval.photos.report import build_report, render
from chip_chat.eval.photos.run import DEFAULT_SESSION, PhotoSetImages, run_set
from chip_chat.harvest.blobs import LocalBlobStore
from chip_chat.vision.describe import AzureVisionModel, MealDescriber
from chip_chat.vision.lane import PhotoLane
from chip_chat.vision.matcher import MealMatcher, SlotRules
from chip_chat.vision.vocabulary import MODULE_VARIABLE, Vocabulary, VocabularyError

DEFAULT_MANIFEST = Path("eval/photos/labels.json")
DEFAULT_CATALOG_PREFIX = "catalog"


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` on success; ``1`` where the set is not yet the
        set the ticket asks for, or a run could not be started. A ``--check``
        that finds an incomplete set exits non-zero deliberately: an
        under-covered set is a build failure, not a warning, or it stays
        under-covered.
    """
    args = _parser().parse_args(argv)
    try:
        labels = LabeledSet.load(args.manifest)
    except LabelError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    vocabulary = _vocabulary()
    if vocabulary is not None:
        try:
            labels.against(vocabulary)
        except LabelError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.check:
        return _check(labels, vocabulary is not None)

    if vocabulary is None:
        print(
            f"error: a run needs the generated vocabulary; set {MODULE_VARIABLE}",
            file=sys.stderr,
        )
        return 1
    missing = labels.missing_files()
    if missing:
        print(
            "error: these frames are labeled and not on disk: "
            + ", ".join(label.photo_id for label in missing),
            file=sys.stderr,
        )
        return 1
    if not labels.photos:
        print("error: the set is empty; there is nothing to run", file=sys.stderr)
        return 1
    if args.catalog is None:
        print(
            "error: a run needs a built catalogue for the matcher; pass --catalog",
            file=sys.stderr,
        )
        return 1

    catalog = load_catalog(LocalBlobStore(args.catalog), args.catalog_prefix)
    rules = SlotRules.from_env()
    model = AzureVisionModel.from_env()
    lane = PhotoLane(
        MealDescriber(model, images=PhotoSetImages(labels), vocabulary=vocabulary),
        MealMatcher(catalog, rules=rules),
    )
    runs = run_set(labels, lane, only=args.only, session_id=args.session)
    document = render(
        build_report(
            labels,
            runs,
            deployment=model.deployment,
            content_version=vocabulary.content_version,
            rules=rules,
        )
    )
    if args.out is not None:
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    return 0


def _check(labels: LabeledSet, checked_terms: bool) -> int:
    """Report the set's coverage without calling anything."""
    cover = coverage(labels)
    print(f"{cover.photos} labeled frames (need {MINIMUM_PHOTOS})")
    if not checked_terms:
        print(
            f"note: {MODULE_VARIABLE} is unset, so the labeled terms were not "
            "checked against the catalogue"
        )
    for requirement, ids in cover.met:
        print(f"  ok      {requirement.name}: {len(ids)}/{requirement.minimum}")
    for requirement, ids in cover.unmet:
        print(
            f"  MISSING {requirement.name}: {len(ids)}/{requirement.minimum} "
            f"({requirement.source})"
        )
    missing = labels.missing_files()
    for label in missing:
        print(f"  MISSING file for {label.photo_id}: {labels.path(label)}")
    return 0 if cover.complete and not missing else 1


def _vocabulary() -> Vocabulary | None:
    """The configured vocabulary, or ``None`` where none is configured.

    ``--check`` is meant to be runnable on a laptop that has never built a
    catalogue, so an unset variable is not an error here -- but the check then
    says so, because a manifest whose terms nobody verified is exactly the
    thing this package refuses to score.
    """
    if not os.environ.get(MODULE_VARIABLE, "").strip():
        return None
    try:
        return Vocabulary.from_env()
    except VocabularyError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.eval.photos",
        description="Check or score the labeled photo set.",
    )
    parser.add_argument(
        "--set",
        dest="manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"the label manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and report coverage without calling a model",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        help="directory holding a built catalogue, for the matcher",
    )
    parser.add_argument(
        "--catalog-prefix",
        default=DEFAULT_CATALOG_PREFIX,
        help=f"key prefix within it (default: {DEFAULT_CATALOG_PREFIX})",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        help="run only these photo ids",
    )
    parser.add_argument(
        "--session",
        default=DEFAULT_SESSION,
        help="session id to group the run's turns under",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="write the report here instead of to stdout",
    )
    return parser


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    raise SystemExit(main())
