"""``python -m chip_chat.eval.golden`` -- check the set, or run and score it.

Two modes, and the cheap one is the default for the same reason it is in the
labeled photo set: an experiment should fail on the free check rather than after
the fortieth model call.

``--check`` loads the manifest, refuses one that contradicts itself, reports
which of #29's scope clauses the set meets and which PRD requirements nothing
covers, and -- where a catalogue is given -- checks every menu term the set
leans on against that build. It calls no model and costs nothing, so it is the
thing to run in CI and the thing to run after adding a case.

Without ``--check`` it runs every case through the week-one slice and writes the
baseline. That spends money, at least one model call per case.

.. code-block:: console

    $ python -m chip_chat.eval.golden --check
    $ python -m chip_chat.eval.golden --check --catalog ./catalog-build
    $ export CHIP_CHAT_FOUNDRY_ENDPOINT=... CHIP_CHAT_FOUNDRY_API_KEY=...
    $ python -m chip_chat.eval.golden --catalog ./catalog-build \\
          --out eval/golden/BASELINE.md

**Pass ``--catalog`` a build the deployment actually serves.** The term check is
the staleness detector this set has against ``cc-z1i``: the vision enums are
meant to be generated from the live catalogue and nothing wires that generation
yet, so a set scored against a stale vocabulary would keep passing while the menu
moved underneath it. A case naming a term the build does not publish is refused
here, before the run, where it costs nothing.
"""

import argparse
import sys
from pathlib import Path

from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError
from chip_chat.agent.model import AzureChatModel
from chip_chat.catalog import load_catalog
from chip_chat.catalog.records import MenuCatalog
from chip_chat.eval.golden.cases import DEFAULT_MANIFEST, CaseError, GoldenSet
from chip_chat.eval.golden.coverage import coverage
from chip_chat.eval.golden.report import build_report, render
from chip_chat.eval.golden.run import DEFAULT_SESSION, run_set
from chip_chat.eval.golden.slice import SliceDeployment
from chip_chat.harvest.blobs import LocalBlobStore

DEFAULT_CATALOG_PREFIX = "catalog"


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` on success; ``1`` where the set is not yet the
        set #29 asks for, or a run could not be started. A ``--check`` that
        finds an uncovered requirement exits non-zero deliberately: a gap in
        coverage is a build failure, not a warning, or it stays a gap.
    """
    args = _parser().parse_args(argv)
    try:
        golden = GoldenSet.load(args.manifest)
    except CaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    catalog = _catalog(args)
    if catalog is not None:
        try:
            golden.against(catalog)
        except CaseError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1

    if args.check:
        return _check(golden, catalog)

    if not golden.cases:
        print("error: the set is empty; there is nothing to run", file=sys.stderr)
        return 1
    try:
        model = AzureChatModel(FoundryConfig.from_env())
    except FoundryConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    deployment = SliceDeployment(model, session_prefix=args.session)
    observations = run_set(golden, deployment, only=args.only)
    document = render(
        build_report(
            golden,
            observations,
            deployment=deployment.name,
            catalog_version=None if catalog is None else catalog.content_version(),
        )
    )
    if args.out is not None:
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    return 0


def _check(golden: GoldenSet, catalog: MenuCatalog | None) -> int:
    """Report the set's coverage without calling anything."""
    cover = coverage(golden)
    print(f"{cover.cases} cases in {golden.source}")
    if catalog is None:
        print(
            "note: no --catalog, so the menu terms the set leans on were not "
            "checked against a build"
        )
    print(
        f"  {len(cover.covered)} requirements covered by a case, "
        f"{len(cover.delegated)} measured elsewhere, "
        f"{len(cover.uncovered)} uncovered"
    )
    for item in cover.uncovered:
        print(f"  UNCOVERED {item.id}: {item.text}")
    for tool in cover.tools_without_a_case:
        print(f"  NO CASE   expects {tool.value}")
    for shape, ids in cover.met:
        print(f"  ok        {shape.name}: {len(ids)}/{shape.minimum}")
    for shape, ids in cover.unmet:
        print(f"  MISSING   {shape.name}: {len(ids)}/{shape.minimum} ({shape.source})")
    return 0 if cover.complete else 1


def _catalog(args: argparse.Namespace) -> MenuCatalog | None:
    """The built catalogue, where one was given.

    Optional for the same reason the photo set's vocabulary is: ``--check`` has
    to be runnable on a laptop that has never built a catalogue, and a check
    that says which half it skipped is worth more than one that refuses to run.
    """
    if args.catalog is None:
        return None
    return load_catalog(LocalBlobStore(args.catalog), args.catalog_prefix)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.eval.golden",
        description="Check the golden set, or run it against the week-one slice.",
    )
    parser.add_argument(
        "--set",
        dest="manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"the case manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and report coverage without calling a model",
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
    parser.add_argument(
        "--only",
        nargs="+",
        help="run only these case ids",
    )
    parser.add_argument(
        "--session",
        default=DEFAULT_SESSION,
        help="prefix for the session id each case is run under",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="write the report here instead of to stdout",
    )
    return parser


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    raise SystemExit(main())
