"""``python -m chip_chat.eval.dietary`` -- check the set, or run it at a target.

Three modes, and two of them are free.

``--check`` loads the probes, holds them to #84's scope, and -- where
``--catalog`` names a build -- walks every probe's premise back to the published
allergen record it was written against. It calls no model. That is the one to
put in CI, because it catches the two failures that matter most and costs
nothing: a red team that has quietly stopped covering one of #84's attacks, and
a probe whose premise about the published record has stopped being true.

``--ceiling`` runs the probes through the week-one slice with the model replaced
by the corpus: the lane is opened for real and the reply is what came back out of
it. Free, needs no credentials, and **not a score for the agent** --
:mod:`chip_chat.eval.dietary.testing` says at length what a run against a stub is
worth. What it is worth here is one line, and it is the line at the top of the
document: this deployment serves no published allergen record, so most of #84
cannot be asked of it at all.

Without either flag, the probes run against the slice on a real model and the
baseline is written. That spends money: at least one model call per probe.

**What a run exits non-zero on, and what it does not.** A *measured* breach of
the gate, and an unmet scope clause. Not an unmeasured gate. PRD section 10
makes this eval launch-blocking and today almost nothing here can be measured --
``chip_chat.agent.hardcoded`` serves no published allergen record and
``chip_chat.agent.envelope`` has no caller -- so a build that went red about
that would be a build that is red about wiring rather than about the product,
and a gate that is red by construction is a gate somebody switches off. The
report says *not measured* at the top of the document instead, which is what an
unmeasured launch gate is: not a pass.

.. code-block:: console

    $ python -m chip_chat.eval.dietary --check
    $ python -m chip_chat.eval.dietary --check --catalog ./catalog-build
    $ export CHIP_CHAT_FOUNDRY_ENDPOINT=... CHIP_CHAT_FOUNDRY_API_KEY=...
    $ python -m chip_chat.eval.dietary --out eval/dietary/BASELINE.md
"""

import argparse
import sys
from pathlib import Path

from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError
from chip_chat.agent.model import AzureChatModel
from chip_chat.catalog import load_catalog
from chip_chat.catalog.records import MenuCatalog
from chip_chat.eval.dietary.coverage import Coverage, coverage
from chip_chat.eval.dietary.hand import DEFAULT_HAND_CHECK, HandCheck, HandCheckError
from chip_chat.eval.dietary.probes import DEFAULT_MANIFEST, ProbeError, ProbeSet
from chip_chat.eval.dietary.report import build_report, render
from chip_chat.eval.dietary.run import run_probes
from chip_chat.eval.dietary.scoring import DietaryScores
from chip_chat.eval.dietary.slice import SliceTarget
from chip_chat.eval.dietary.testing import CEILING_CAVEAT, CEILING_SOURCE, ceiling
from chip_chat.harvest.blobs import LocalBlobStore

DEFAULT_BASELINE = Path("eval/dietary/BASELINE.md")
DEFAULT_CATALOG_PREFIX = "catalog"


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` on success; ``1`` where the manifest or the hand
        check cannot be believed, where a probe's premise disagrees with the
        published record, where the set does not meet #84's scope, where a run
        could not be started, or where a run found a *measured* gate breach.
    """
    args = _parser().parse_args(argv)
    try:
        probes = ProbeSet.load(args.probes)
        hand = HandCheck.load(args.hand)
        hand.against(probes)
        catalog = _catalog(args)
        if catalog is not None:
            probes.against(catalog)
    except (ProbeError, HandCheckError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    cover = coverage(probes)
    if args.check:
        return _check(probes, cover, catalog, hand)

    if args.ceiling:
        turns = ceiling(probes.probes)
        source = CEILING_SOURCE
        caveat = CEILING_CAVEAT
    else:
        try:
            model = AzureChatModel(FoundryConfig.from_env())
        except FoundryConfigError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        target = SliceTarget(model)
        turns = run_probes(probes.probes, target, only=args.only)
        source = target.name
        caveat = _CAVEAT
    report = build_report(
        probes,
        turns,
        cover,
        source=source,
        hand=hand,
        caveat=caveat,
    )
    document = render(report)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    return _exit_status(report.scores)


_CAVEAT = (
    "> This run is against the week-one slice, which serves three invented menu "
    "items and no published allergen record. Every probe leaning on a published "
    "status is unscored here by construction, and the gate reads *not measured* "
    "rather than *met*. See `eval/dietary/README.md`."
)


def _check(
    probes: ProbeSet,
    cover: Coverage,
    catalog: MenuCatalog | None,
    hand: HandCheck,
) -> int:
    """Report what the set can support, without running anything."""
    print(f"{probes.source}: {cover.probes} probes")
    if catalog is None:
        print(
            "  note: no --catalog, so no probe's premise was checked against the "
            "published allergen record; a probe can be stale and this run cannot "
            "see it"
        )
    else:
        grounded = sum(1 for probe in probes if probe.grounds)
        print(
            f"  ok        {grounded} probe premise(s) agree with the built "
            f"catalogue, {catalog.content_version()}"
        )
    if hand.empty:
        print(
            f"  note: {hand.source} holds no verdicts, so every judgement is "
            "unscored until somebody reads a run's transcripts"
        )
    else:
        print(f"  ok        {len(hand)} hand verdict(s), read by {hand.checked_by}")
    for clause, ids in cover.met:
        print(f"  ok        {clause.name}: {len(ids)}/{clause.minimum}")
    for clause, ids in cover.unmet:
        print(f"  MISSING   {clause.name}: {len(ids)}/{clause.minimum} ({clause.source})")
    for shape in cover.shapes_without_a_probe:
        print(f"  MISSING   no probe makes the {shape.value} attack")
    for item in cover.uncovered:
        print(f"  MISSING   nothing in the set is evidence about {item.id}")
    return 0 if cover.complete else 1


def _exit_status(scores: DietaryScores) -> int:
    """What a run exits with. See the module docstring on why this is narrow."""
    if scores.gate is False:
        print(
            f"error: the allergen and dietary gate is breached — "
            f"{scores.breaches} gated failure(s); see the report",
            file=sys.stderr,
        )
        return 1
    return 0


def _catalog(args: argparse.Namespace) -> MenuCatalog | None:
    """The built catalogue, where one was given.

    Optional for the reason the golden set's is: ``--check`` has to be runnable
    on a laptop that has never built a catalogue, and a check that cannot be run
    is a check nobody runs. What it costs is said out loud in :func:`_check`.
    """
    if args.catalog is None:
        return None
    return load_catalog(LocalBlobStore(args.catalog), args.catalog_prefix)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.eval.dietary",
        description="Red-team the allergen and dietary boundary (#84).",
    )
    parser.add_argument(
        "--probes",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"the probe manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--hand",
        type=Path,
        default=DEFAULT_HAND_CHECK,
        help=f"the hand-verification record (default: {DEFAULT_HAND_CHECK})",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        help=(
            "directory holding a built catalogue, to check every probe's premise against"
        ),
    )
    parser.add_argument(
        "--catalog-prefix",
        default=DEFAULT_CATALOG_PREFIX,
        help=(
            f"prefix the catalogue was written under (default: {DEFAULT_CATALOG_PREFIX})"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what the set can support, without running anything",
    )
    parser.add_argument(
        "--ceiling",
        action="store_true",
        help="run against the slice with the corpus as the model -- free, not a score",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        help="run only these probe ids",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help=f"write the report here instead of to stdout (baseline: {DEFAULT_BASELINE})",
    )
    return parser


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    raise SystemExit(main())
