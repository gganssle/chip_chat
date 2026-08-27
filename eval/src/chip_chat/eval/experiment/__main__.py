"""``python -m chip_chat.eval.experiment`` -- run an arm, or compare two.

Four modes, and two of them are free. The cheap ones are the default for the same
reason they are in the five sets below this one: an experiment should fail on the
free check rather than after the sixty-eighth model call.

``--check`` loads the configurations, refuses one that contradicts itself, builds
the dataset, and prints each arm's fingerprint beside the prompt version it
resolves to. It calls nothing and needs no credentials, so it is the thing to run
in CI and the thing to run after editing a configuration.

``--ceiling`` runs an arm against the week-one slice with lane selection **handed
to it** and writes the result. Free, needs no credentials, and worth exactly what
``chip_chat.eval.golden.testing`` says a ceiling run is worth -- with one thing
more that matters here and nowhere else: the routing oracle answers from the
golden set and never reads the system prompt, so the *prompt axis is inert* on a
ceiling run and the document says so above its own table. Two arms compared under
the oracle produce two identical results, which is what nothing-read-the-change
looks like and is not what no-difference looks like.

``--run <name>`` runs one arm against a real deployment and records the result.
That spends money: one model call per row, at least, plus two per scoreable row
if ``--judge`` is given.

``--compare <a> <b>`` runs both arms and renders the comparison. This is #73's
demo criterion, and it is one command because a comparison assembled by hand from
two runs somebody did on different days is the thing the harness exists to
replace. ``--compare-recorded`` does the same from two files, which is how a
candidate is checked against a baseline recorded weeks ago.

.. code-block:: console

    $ python -m chip_chat.eval.experiment --check
    $ python -m chip_chat.eval.experiment --ceiling --run shipped
    $ export CHIP_CHAT_FOUNDRY_ENDPOINT=... CHIP_CHAT_FOUNDRY_CHAT_DEPLOYMENT=...
    $ python -m chip_chat.eval.experiment --run shipped --judge \\
          --record eval/experiments/results/shipped.json \\
          --out eval/experiments/BASELINE.md
    $ python -m chip_chat.eval.experiment --compare shipped lean-lanes \\
          --out eval/experiments/COMPARISON.md
"""

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError
from chip_chat.agent.model import AzureChatModel
from chip_chat.eval.dataset.build import Dataset, DatasetError, build_dataset
from chip_chat.eval.experiment.compare import compare
from chip_chat.eval.experiment.configurations import (
    DEFAULT_MANIFEST,
    ConfigurationError,
    ExperimentConfiguration,
    configurations,
    named,
)
from chip_chat.eval.experiment.report import render_comparison, render_result
from chip_chat.eval.experiment.results import (
    DEFAULT_RESULTS_DIR,
    ExperimentResult,
    ResultError,
    load_result,
    write_result,
)
from chip_chat.eval.experiment.run import Experiment, run_experiment
from chip_chat.eval.golden.cases import DEFAULT_MANIFEST as GOLDEN_MANIFEST
from chip_chat.eval.golden.cases import CaseError, GoldenSet
from chip_chat.eval.golden.slice import SliceDeployment
from chip_chat.eval.golden.testing import RoutingOracle
from chip_chat.eval.grounding.judge import ModelJudge
from chip_chat.eval.photos.labels import LabeledSet, LabelError
from chip_chat.eval.trajectory.trees import TraceSpan

PHOTOS_MANIFEST = Path("eval/photos/labels.json")
DEFAULT_BASELINE = Path("eval/experiments/BASELINE.md")

CEILING_CAVEAT = (
    "> **This is not a score for the agent, and it is not a score for the "
    "prompt.** Lane selection was handed to the deployment: the routing oracle "
    "calls, for each message, exactly the tool the row expects. Nothing about "
    "model quality survives a model that was told the answer — and the oracle "
    "answers from the golden set without reading the system prompt at all, so "
    "two arms differing only in their prompt produce the same numbers here. "
    "What this run measures is the harness and the wiring at their ceiling."
)

LIVE_CAVEAT = (
    "> **The week-one slice registers six of the eleven tools.** A tool that is "
    "not registered cannot be routed to, so its rows come back `no_tool` "
    "however good the model is, and a span tree cannot tell that apart from a "
    "model that chose not to call. Read `eval/trajectory/BASELINE.md` beside "
    "this document."
)


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` on success; ``1`` where a manifest cannot be
        believed, an arm cannot be found, a run could not be started, or a
        comparison found a regression. A regression exits non-zero deliberately:
        the point of running a comparison from CI is that a change which breaks
        a lane fails the build rather than producing a document nobody opens.
    """
    args = _parser().parse_args(argv)
    try:
        arms = configurations(args.manifest)
    except ConfigurationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.compare_recorded:
        return _compare_recorded(args)

    try:
        golden = GoldenSet.load(args.golden)
        labels = LabeledSet.load(args.photos)
        dataset = build_dataset(golden, labels)
    except (CaseError, LabelError, DatasetError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.check:
        return _check(arms, dataset)

    try:
        if args.compare:
            return _compare_arms(args, arms, golden, dataset)
        return _run_one(args, arms, golden, dataset)
    except (ConfigurationError, FoundryConfigError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _check(arms: Sequence[ExperimentConfiguration], dataset: Dataset) -> int:
    """Report the arms and the dataset they would be scored against."""
    print(f"{dataset.name} {dataset.version}: {len(dataset)} entries")
    for arm in arms:
        try:
            print(f"  ok        {arm.describe()}")
        except ConfigurationError as error:
            print(f"  BROKEN    {error}")
            return 1
        print(f"            {arm.why}")
    if len(arms) < 2:
        print(
            "  MISSING   at least two configurations, or there is nothing to "
            "compare and #73's demo criterion cannot be met"
        )
        return 1
    return 0


def _run_one(
    args: argparse.Namespace,
    arms: Sequence[ExperimentConfiguration],
    golden: GoldenSet,
    dataset: Dataset,
) -> int:
    """Run one arm and write its result."""
    arm = named(arms, args.run)
    experiment = _execute(args, arm, golden, dataset)
    document = render_result(experiment.result)
    _emit(document, args.out)
    _record(experiment.result, args.record, args.results)
    _capture(experiment, golden, args.capture)
    return 0


def _compare_arms(
    args: argparse.Namespace,
    arms: Sequence[ExperimentConfiguration],
    golden: GoldenSet,
    dataset: Dataset,
) -> int:
    """Run two arms against the same dataset and render the comparison."""
    first, second = args.compare
    baseline = _execute(args, named(arms, first), golden, dataset)
    candidate = _execute(args, named(arms, second), golden, dataset)
    _capture(candidate, golden, args.capture)
    comparison = compare(baseline.result, candidate.result)
    _emit(render_comparison(comparison), args.out)
    if args.record or args.results:
        _record(baseline.result, None, args.results)
        _record(candidate.result, None, args.results)
    for line in comparison.regressions:
        print(f"regression: {line}", file=sys.stderr)
    return 1 if comparison.regressions and args.fail_on_regression else 0


def _compare_recorded(args: argparse.Namespace) -> int:
    """Compare two recorded results without running anything."""
    first, second = args.compare_recorded
    try:
        baseline: ExperimentResult = load_result(first)
        candidate: ExperimentResult = load_result(second)
    except ResultError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    comparison = compare(baseline, candidate)
    _emit(render_comparison(comparison), args.out)
    for line in comparison.regressions:
        print(f"regression: {line}", file=sys.stderr)
    return 1 if comparison.regressions and args.fail_on_regression else 0


def _execute(
    args: argparse.Namespace,
    arm: ExperimentConfiguration,
    golden: GoldenSet,
    dataset: Dataset,
) -> Experiment:
    """Run one arm, free or live depending on ``--ceiling``."""
    environment = dict(os.environ)
    if args.ceiling:
        return run_experiment(
            arm,
            golden,
            dataset,
            lambda configuration: SliceDeployment(
                RoutingOracle(golden),
                session_prefix=f"{args.session}-{configuration.name}",
                prompt=configuration.prompt(),
            ),
            environment=environment,
            prompt_read=False,
            only=args.only,
            caveat=CEILING_CAVEAT,
        )

    overlay = {**environment, **arm.environment()}
    config = FoundryConfig.from_env(overlay)
    judge = None if args.judge is None else _judge(config, args.judge)
    experiment = run_experiment(
        arm,
        golden,
        dataset,
        lambda configuration: SliceDeployment(
            AzureChatModel(config),
            session_prefix=f"{args.session}-{configuration.name}",
            prompt=configuration.prompt(),
        ),
        environment=overlay,
        judge=judge,
        judge_name="" if judge is None else judge.name,
        judge_tokens=0 if judge is None else judge.spend.total_tokens,
        only=args.only,
        caveat=LIVE_CAVEAT,
    )
    if judge is not None:
        print(f"judge spend: {judge.spend.summary()}")
        # The token count is read *after* scoring, because scoring is when the
        # judge is called; `run_experiment` recorded whatever the counter held
        # when it built the result, which was zero. Correcting it here keeps the
        # budget line honest without making the runner aware of a judge's
        # internals.
        experiment = replace(
            experiment,
            result=replace(experiment.result, judge_tokens=judge.spend.total_tokens),
        )
    return experiment


def _judge(config: FoundryConfig, deployment: str) -> ModelJudge:
    """The judge for a live run. See ``chip_chat.eval.grounding.__main__``."""
    if deployment:
        config = replace(config, chat_deployment=deployment)
    return ModelJudge(AzureChatModel(config))


def _capture(experiment: Experiment, golden: GoldenSet, out: Path | None) -> None:
    """Write the run's span trees where #77's promotion path can read them.

    The capture is the *reader's* shape --
    :class:`~chip_chat.eval.trajectory.trees.TraceSpan` as flat objects -- and
    not any backend's, for the reason #74's module docstring gives: a second
    adapter is a function, and a second reader would be a second
    implementation of the metric. A backend adapter writes this same shape.

    Args:
        experiment: The run.
        golden: The set, for the message each row asked. A recording does not
            carry the visitor's prose -- only what the app chose to put on a
            span -- so the message comes from the set rather than from the
            trace, and a real backend adapter reads it from the request log.
        out: Where to write it, or ``None`` to write nothing.
    """
    if out is None:
        return
    messages = {case.case_id: case.message for case in golden}
    payload = {
        "source": experiment.result.source,
        "dataset_version": experiment.result.dataset_version,
        "turns": [
            {
                "entry_id": row.entry_id,
                "message": messages.get(row.observation.case_id, ""),
                "reply": row.turn.reply,
                "spans": [_span(span) for span in row.spans],
            }
            for row in experiment.recorded
            if row.spans
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"captured {len(payload['turns'])} trace(s) to {out}")


def _span(span: TraceSpan) -> dict[str, Any]:
    """One span, as a capture file holds it."""
    return {
        "name": span.name,
        "span_id": span.span_id,
        "parent_id": span.parent_id,
        "trace_id": span.trace_id,
        "attributes": dict(span.attributes),
        "service": span.service,
        "started": span.started,
    }


def _emit(document: str, out: Path | None) -> None:
    if out is None:
        print(document)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(document, encoding="utf-8")
    print(f"wrote {out}")


def _record(
    result: ExperimentResult, explicit: Path | None, directory: Path | None
) -> None:
    """Write the machine-readable result, where one was asked for."""
    if explicit is not None:
        write_result(result, explicit)
        print(f"recorded {explicit}")
        return
    if directory is not None:
        path = directory / f"{result.experiment}.json"
        write_result(result, path)
        print(f"recorded {path}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.eval.experiment",
        description="Score a configuration against the versioned dataset.",
    )
    parser.add_argument(
        "--configurations",
        dest="manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"the configuration manifest (default: {DEFAULT_MANIFEST})",
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
        help="load the configurations and the dataset without running anything",
    )
    parser.add_argument(
        "--run",
        metavar="NAME",
        help="run this configuration",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BASELINE", "CANDIDATE"),
        help="run both configurations and render the comparison",
    )
    parser.add_argument(
        "--compare-recorded",
        nargs=2,
        type=Path,
        metavar=("BASELINE", "CANDIDATE"),
        help="compare two recorded results without running anything",
    )
    parser.add_argument(
        "--ceiling",
        action="store_true",
        help="answer with the routing oracle -- free, and blind to the prompt",
    )
    parser.add_argument(
        "--judge",
        nargs="?",
        const="",
        default=None,
        metavar="DEPLOYMENT",
        help="score the judged findings with a model; bare uses the chat deployment",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        help="run only these dataset entry ids",
    )
    parser.add_argument(
        "--session",
        default="experiment",
        help="prefix for the session id each row is run under",
    )
    parser.add_argument(
        "--capture",
        type=Path,
        help="write the run's span trees here, for #77's promotion path",
    )
    parser.add_argument(
        "--record",
        type=Path,
        help="write the machine-readable result here",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help=f"write results into this directory (baseline: {DEFAULT_RESULTS_DIR})",
    )
    parser.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit non-zero when a comparison finds one; for CI",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help=(
            f"write the document here instead of to stdout (baseline: {DEFAULT_BASELINE})"
        ),
    )
    return parser


def _environment() -> Mapping[str, str]:
    """The process environment, as a mapping the harness can be handed."""
    return dict(os.environ)


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    raise SystemExit(main())
