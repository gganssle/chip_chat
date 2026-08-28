"""``python -m chip_chat.eval.trajectory`` -- check, run the ceiling, or score.

Three modes, and two of them are free. The cheap ones are the default for the
same reason they are in the other three sets: an experiment should fail on the
free check rather than after the thirty-fourth model call.

``--check`` builds the dataset from the two committed manifests, turns its
routing rows into expectations, and reports whether they can support the numbers
#74 asks for -- a row in every lane, boundary rows that make *wrong lane*
nameable, rows where a wrong query is observable at all. It calls nothing.

``--ceiling`` runs those rows through the week-one slice with lane selection
handed to it and writes the report. Free, needs no credentials, and **not a
score for the agent** -- :mod:`chip_chat.eval.trajectory.testing` says at length
what a run against an oracle is worth. It is the one to put in CI.

**A run exits non-zero on a split trace and on nothing else.** A split trace
means the turn arrived as two unrelated traces, which is issue #103's
propagation rather than anything a model did, and it is the one failure that
makes every other number in the document meaningless. The accuracy itself is
never gated here: the week-one slice registers six of the eleven tools, so its
ceiling is under the target by construction, and a gate that is red by
construction is a gate somebody switches off.

Without either flag it runs the rows against a real deployment and writes the
baseline. That spends money: at least one model call per row.

**``--lanes wired`` matters more here than anywhere else in ``eval/``.** This is
the eval that reports tool-selection accuracy, and a tool that is not registered
cannot be routed to — so an unwired run scores the account, personalization and
vision rows at zero for a reason that is about ``offered_tools`` rather than
about the model, and a span tree cannot tell that apart from a model that chose
not to call. The report's **Traces from** line names the wiring on every run.
:mod:`chip_chat.eval.wiring` is the long version.

.. code-block:: console

    $ python -m chip_chat.eval.trajectory --check
    $ python -m chip_chat.eval.trajectory --ceiling --out eval/trajectory/BASELINE.md
    $ export CHIP_CHAT_FOUNDRY_ENDPOINT=... CHIP_CHAT_FOUNDRY_API_KEY=...
    $ python -m chip_chat.eval.trajectory --out eval/trajectory/BASELINE.md
    $ python -m chip_chat.eval.trajectory --lanes wired
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError
from chip_chat.agent.model import AzureChatModel
from chip_chat.eval.dataset.build import Dataset, DatasetError, build_dataset
from chip_chat.eval.golden.cases import DEFAULT_MANIFEST as GOLDEN_MANIFEST
from chip_chat.eval.golden.cases import CaseError, GoldenSet
from chip_chat.eval.golden.run import DEFAULT_SESSION
from chip_chat.eval.golden.slice import SLICE_PERSONA
from chip_chat.eval.photos.labels import LabeledSet, LabelError
from chip_chat.eval.trajectory.coverage import RATE_NEEDS, coverage
from chip_chat.eval.trajectory.expectations import (
    Expectation,
    ExpectationError,
    expectations,
)
from chip_chat.eval.trajectory.report import build_report, render
from chip_chat.eval.trajectory.run import run_trajectories
from chip_chat.eval.trajectory.slice import SliceTraceSource
from chip_chat.eval.trajectory.testing import (
    CEILING_CAVEAT,
    CEILING_SOURCE,
    ceiling,
)
from chip_chat.eval.wiring import LaneWiringError, add_lanes_option, run_lanes

PHOTOS_MANIFEST = Path("eval/photos/labels.json")
DEFAULT_BASELINE = Path("eval/trajectory/BASELINE.md")


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` on success; ``1`` where a manifest cannot be
        believed, the rows cannot support #74's numbers, a run could not be
        started, or a run found a split trace. An unmet scope clause exits
        non-zero deliberately -- a gap in what can be measured is a build
        failure, or it stays a gap.
    """
    args = _parser().parse_args(argv)
    try:
        golden = GoldenSet.load(args.golden)
        labels = LabeledSet.load(args.photos)
        dataset = build_dataset(golden, labels)
        rows = expectations(dataset)
    except (CaseError, LabelError, DatasetError, ExpectationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.check:
        return _check(dataset, rows)

    if not rows:
        print("error: the dataset holds no rows that score routing", file=sys.stderr)
        return 1

    if args.ceiling:
        trajectories = ceiling(golden, dataset, only=args.only)
        source = CEILING_SOURCE
        caveat = CEILING_CAVEAT
    else:
        try:
            model = AzureChatModel(FoundryConfig.from_env())
        except FoundryConfigError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        try:
            with run_lanes(args.lanes, SLICE_PERSONA) as wired:
                print(wired.note)
                adapter = SliceTraceSource(
                    golden=golden,
                    model=model,
                    lanes=wired.lanes,
                    session_prefix=args.session,
                )
                trajectories = run_trajectories(rows, adapter, only=args.only)
                source = adapter.name
        except LaneWiringError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        caveat = ""

    report = build_report(
        rows,
        trajectories,
        source=source,
        dataset=dataset.name,
        version=dataset.version,
        caveat=caveat,
    )
    document = render(report)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    return _exit_status(report.scores.split_traces)


def _check(dataset: Dataset, rows: Sequence[Expectation]) -> int:
    """Report what the rows can support, without running anything."""
    cover = coverage(rows)
    print(f"{dataset.name} {dataset.version}: {cover.rows} rows that score routing")
    for lane, held in cover.per_lane:
        thin = "  (thin)" if 0 < held < RATE_NEEDS else ""
        print(f"  {lane.value:<16} {held}{thin}")
    for clause, ids in cover.met:
        print(f"  ok        {clause.name}: {len(ids)}/{clause.minimum}")
    for clause, ids in cover.unmet:
        print(f"  MISSING   {clause.name}: {len(ids)}/{clause.minimum} ({clause.source})")
    return 0 if cover.complete else 1


def _exit_status(split_traces: int) -> int:
    """What a run exits with. See the module docstring on why this is narrow."""
    if not split_traces:
        return 0
    print(
        f"error: {split_traces} turn(s) arrived as more than one trace; "
        "trace context is not propagating (#103). Check it with "
        "`make trace-boundary`.",
        file=sys.stderr,
    )
    return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.eval.trajectory",
        description=(
            "Score tool selection and trajectories over the dataset's span trees."
        ),
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=GOLDEN_MANIFEST,
        help=f"the golden case manifest (default: {GOLDEN_MANIFEST})",
    )
    parser.add_argument(
        "--photos",
        type=Path,
        default=PHOTOS_MANIFEST,
        help=f"the labeled photo manifest (default: {PHOTOS_MANIFEST})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what the rows can support, without running anything",
    )
    parser.add_argument(
        "--ceiling",
        action="store_true",
        help="run against the slice with routing handed to it -- free, and not a score",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        help="run only these dataset entry ids",
    )
    parser.add_argument(
        "--session",
        default=DEFAULT_SESSION,
        help="prefix for the session id each row is run under",
    )
    add_lanes_option(parser)
    parser.add_argument(
        "--out",
        type=Path,
        help=f"write the report here instead of to stdout (baseline: {DEFAULT_BASELINE})",
    )
    return parser


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    raise SystemExit(main())
