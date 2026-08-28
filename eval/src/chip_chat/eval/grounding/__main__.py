"""``python -m chip_chat.eval.grounding`` -- check, run the ceiling, or score.

Three modes, and two of them are free. The cheap ones are the default for the
same reason they are in the other four sets: an experiment should fail on the
free check rather than after the thirty-fourth model call.

``--check`` builds the dataset from the two committed manifests, turns its rows
into questions, and reports whether they can support the numbers #75 asks for --
questions the corpus answers *and* questions it does not, allergen rows in both
directions, answers that owe a citation. It calls nothing.

``--ceiling`` runs those rows through the week-one slice with lane selection
handed to it and writes the report. Free, needs no credentials, and **not a
score for the agent** -- :mod:`chip_chat.eval.grounding.testing` says at length
what a run against an oracle is worth, and here it says one thing more: three of
the five findings cannot be measured at all until ``chip_chat.agent.envelope``
has a caller. It is the one to put in CI.

**A run exits non-zero on a measured gate breach and on a split trace.** Never
on an unmeasured one: PRD section 12 makes the citation gate blocking, and today
nothing can count it, so a red build here would be a build that is red about the
wiring rather than about the product -- and a gate that is red by construction is
a gate somebody switches off. A split trace is issue #103's propagation, and it
is the failure that makes every other number in the document meaningless: the
retrieval is in one trace and the response in another, so nothing can show the
passages belong to the answer.

Without either flag it runs the rows against a real deployment and writes the
baseline. That spends money: at least one model call per row.

``--judge`` puts a model behind :class:`~chip_chat.eval.grounding.run.Judge`, and
is what turns the two judged findings from ``unscored`` into numbers. It is
orthogonal to how the turns were produced -- a ceiling run judged is a real
groundedness measurement of prose a scripted oracle wrote, which is worth exactly
what :mod:`chip_chat.eval.grounding.testing` says it is and no more, so the
combination is allowed and the report says which source it scored. It costs two
model calls per *scoreable* row rather than per row, and the run prints what it
spent, because #76 makes judge tokens a line in the daily budget rather than a
cost nobody attributed.

``--lanes wired`` runs the turns against the lanes the deployment has, and the
thing to know about it here is what it does **not** wire: the knowledge lane is
``cc-e1sr`` and is absent under every value of the flag, so
``search_menu_knowledge`` still answers off the three-item hardcoded menu and a
groundedness number is still a number about a fixture. What wiring changes here
is which *other* lanes a turn could have gone down instead. The report's
**Answered by** line names the configuration on every run;
:mod:`chip_chat.eval.wiring` is why it does.

.. code-block:: console

    $ python -m chip_chat.eval.grounding --check
    $ python -m chip_chat.eval.grounding --ceiling --out eval/grounding/BASELINE.md
    $ export CHIP_CHAT_FOUNDRY_ENDPOINT=... CHIP_CHAT_FOUNDRY_API_KEY=...
    $ python -m chip_chat.eval.grounding --judge --out eval/grounding/BASELINE.md
    $ python -m chip_chat.eval.grounding --judge gpt-4.1-mini
    $ python -m chip_chat.eval.grounding --judge --lanes wired
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError
from chip_chat.agent.model import AzureChatModel
from chip_chat.eval.dataset.build import Dataset, DatasetError, build_dataset
from chip_chat.eval.golden.cases import DEFAULT_MANIFEST as GOLDEN_MANIFEST
from chip_chat.eval.golden.cases import CaseError, GoldenSet
from chip_chat.eval.golden.run import DEFAULT_SESSION
from chip_chat.eval.golden.slice import SLICE_PERSONA
from chip_chat.eval.grounding.coverage import RATE_NEEDS, coverage
from chip_chat.eval.grounding.judge import ModelJudge
from chip_chat.eval.grounding.questions import Question, QuestionError, questions
from chip_chat.eval.grounding.report import Report, build_report, render
from chip_chat.eval.grounding.run import run_turns
from chip_chat.eval.grounding.slice import SliceTurnSource
from chip_chat.eval.grounding.testing import CEILING_CAVEAT, CEILING_SOURCE, ceiling
from chip_chat.eval.photos.labels import LabeledSet, LabelError
from chip_chat.eval.wiring import LaneWiringError, add_lanes_option, run_lanes

PHOTOS_MANIFEST = Path("eval/photos/labels.json")
DEFAULT_BASELINE = Path("eval/grounding/BASELINE.md")


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` on success; ``1`` where a manifest cannot be
        believed, the rows cannot support #75's numbers, a run could not be
        started, a run found a split trace, or a run found a *measured* gate
        breach. An unmet scope clause exits non-zero deliberately -- a gap in
        what can be measured is a build failure, or it stays a gap.
    """
    args = _parser().parse_args(argv)
    try:
        golden = GoldenSet.load(args.golden)
        labels = LabeledSet.load(args.photos)
        dataset = build_dataset(golden, labels)
        rows = questions(dataset)
    except (CaseError, LabelError, DatasetError, QuestionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.check:
        return _check(dataset, rows)

    if not rows:
        print("error: the dataset holds no rows this eval can score", file=sys.stderr)
        return 1

    if args.ceiling:
        turns = ceiling(golden, dataset, only=args.only)
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
                adapter = SliceTurnSource(
                    golden=golden,
                    model=model,
                    lanes=wired.lanes,
                    session_prefix=args.session,
                )
                turns = run_turns(rows, adapter, only=args.only)
                source = adapter.name
        except LaneWiringError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        caveat = ""

    try:
        judge = _judge(args.judge)
    except FoundryConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    report = build_report(
        rows,
        turns,
        source=source,
        dataset=dataset.name,
        version=dataset.version,
        judge=judge,
        judged_by="" if judge is None else judge.name,
        caveat=caveat,
    )
    document = render(report)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    if judge is not None:
        # Printed rather than only written into the document, because the person
        # who ran this is the person deciding whether to run it again. #76 makes
        # the same number a budget line; here it is a receipt.
        print(f"judge spend: {judge.spend.summary()}")
    return _exit_status(report)


def _judge(deployment: str | None) -> ModelJudge | None:
    """The judge this run scores with, or ``None`` for the unjudged default.

    ``--judge`` with no value judges on the configured chat deployment;
    ``--judge <name>`` judges on another. A judge on a *different* deployment
    from the one that answered is the arrangement worth reaching for -- a model
    grading its own prose is the one bias in this design nobody can argue away
    -- but it is not enforced here, because on a subscription with quota for two
    deployments the alternative to self-judging is often not judging.

    Args:
        deployment: ``None`` for no judge, ``""`` for the configured chat
            deployment, or a deployment name.

    Returns:
        The judge, or ``None``.

    Raises:
        FoundryConfigError: If the Foundry configuration is absent.
    """
    if deployment is None:
        return None
    config = FoundryConfig.from_env()
    if deployment:
        config = replace(config, chat_deployment=deployment)
    return ModelJudge(AzureChatModel(config))


def _check(dataset: Dataset, rows: Sequence[Question]) -> int:
    """Report what the rows can support, without running anything."""
    cover = coverage(rows)
    print(
        f"{dataset.name} {dataset.version}: {cover.rows} rows, "
        f"{cover.stated} the set states something about, "
        f"{cover.dietary} allergen or dietary"
    )
    if cover.thin_category:
        print(
            f"  note: fewer than {RATE_NEEDS} allergen and dietary rows; the "
            "category is held to counts rather than to a rate, so this does not "
            "move its verdict"
        )
    for clause, ids in cover.met:
        print(f"  ok        {clause.name}: {len(ids)}/{clause.minimum}")
    for clause, ids in cover.unmet:
        print(f"  MISSING   {clause.name}: {len(ids)}/{clause.minimum} ({clause.source})")
    return 0 if cover.complete else 1


def _exit_status(report: Report) -> int:
    """What a run exits with. See the module docstring on why this is narrow."""
    status = 0
    scores = report.scores
    if scores.split_traces:
        print(
            f"error: {scores.split_traces} turn(s) arrived as more than one "
            "trace, so the retrieval cannot be shown to belong to the response; "
            "trace context is not propagating (#103). Check it with "
            "`make trace-boundary`.",
            file=sys.stderr,
        )
        status = 1
    for name, gate in (
        ("citation gate", scores.citation_gate),
        ("allergen and dietary gate", scores.dietary_gate),
    ):
        if gate is False:
            print(f"error: the {name} is breached; see the report", file=sys.stderr)
            status = 1
    return status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.eval.grounding",
        description=(
            "Score groundedness and citation presence over the dataset's turns."
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
        "--judge",
        nargs="?",
        const="",
        default=None,
        metavar="DEPLOYMENT",
        help=(
            "score the two judged findings with a model; bare uses the "
            "configured chat deployment, or name another. Spends tokens."
        ),
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
