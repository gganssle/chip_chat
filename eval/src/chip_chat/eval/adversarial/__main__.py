"""``python -m chip_chat.eval.adversarial`` -- check the suite, or run it.

Three modes, and two of them are free.

``--check`` loads the manifest, refuses one that could not detect what it claims
to, and reports which of #30's scope clauses the suite meets. It calls no model
and costs nothing, so it is the thing to run in CI and the thing to run after
adding an attack.

``--structural`` runs the whole suite against the week-one slice driven by
:class:`~chip_chat.eval.adversarial.testing.CapitulatingModel` -- a model that
does whatever the attack asks. It calls no real model either, and what it
measures is the claim RFC-001 actually makes: that both gates are properties of
the design rather than of the model's good behaviour. A gate that fails here
fails against an adversary who has already won the argument with the prompt,
which is the adversary the design says it does not need to win.

Without either it runs against the slice on a real deployment and writes the
baseline. That spends money, at least one model call per attack per visitor.

.. code-block:: console

    $ python -m chip_chat.eval.adversarial --check
    $ python -m chip_chat.eval.adversarial --structural
    $ export CHIP_CHAT_FOUNDRY_ENDPOINT=... CHIP_CHAT_FOUNDRY_API_KEY=...
    $ python -m chip_chat.eval.adversarial --out eval/adversarial/BASELINE.md

**The exit status is the gate, not the run.** ``--check`` and a real run both
exit non-zero where either launch gate is anything other than ``pass`` -- which
includes *not measured*, deliberately. PRD section 12 makes both gates blocking
and a gate nobody could measure blocks in exactly the same way as one that
failed. A pipeline that went green on an unmeasured gate would be the most
expensive possible way to discover that later.
"""

import argparse
import sys
from pathlib import Path

from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError
from chip_chat.agent.model import AzureChatModel, ChatModel
from chip_chat.eval.adversarial.attacks import (
    DEFAULT_MANIFEST,
    AdversarialSuite,
    SuiteError,
)
from chip_chat.eval.adversarial.coverage import coverage
from chip_chat.eval.adversarial.report import build_report, render
from chip_chat.eval.adversarial.run import run_suite
from chip_chat.eval.adversarial.slice import SliceTarget
from chip_chat.eval.adversarial.testing import CapitulatingModel

_DEFAULT_VISITORS = 3


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` where the suite is the suite #30 asks for and
        both gates pass; ``1`` otherwise, including where a gate could not be
        measured. See the module docstring on why that is not a warning.
    """
    args = _parser().parse_args(argv)
    try:
        suite = AdversarialSuite.load(args.manifest)
    except SuiteError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.check:
        return _check(suite)

    model = _model(args)
    if model is None:
        return 1

    target = SliceTarget(model, visitors=args.visitors, session_prefix=args.session)
    run = run_suite(suite, target, only=args.only)
    report = build_report(suite, run)
    document = render(report)
    if args.out is not None:
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    return 0 if report.scores.gates_pass else 1


def _check(suite: AdversarialSuite) -> int:
    """Report the suite's coverage without attacking anything."""
    cover = coverage(suite)
    print(f"{cover.attacks} attacks in {suite.source}")
    print(
        f"  {len(cover.concurrent)} run from every visitor at once: "
        + ", ".join(cover.concurrent)
    )
    for item in cover.undelivered:
        print(f"  UNDELIVERED {item.id}: delegated here by the golden set, no attack")
    for family in cover.families_without_an_attack:
        print(f"  NO ATTACK   in family {family.value}")
    for tool in cover.write_tools_without_an_attack:
        print(f"  NO ATTACK   aims at {tool.value}")
    for clause, ids in cover.met:
        print(f"  ok          {clause.name}: {len(ids)}/{clause.minimum}")
    for clause, ids in cover.unmet:
        print(
            f"  MISSING     {clause.name}: {len(ids)}/{clause.minimum} ({clause.source})"
        )
    return 0 if cover.complete else 1


def _model(args: argparse.Namespace) -> ChatModel | None:
    """The model to drive the slice with, or ``None`` where none can be built."""
    if args.structural:
        return CapitulatingModel()
    try:
        return AzureChatModel(FoundryConfig.from_env())
    except FoundryConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.eval.adversarial",
        description="Check the adversarial suite, or run it against the week-one slice.",
    )
    parser.add_argument(
        "--suite",
        dest="manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"the attack manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and report coverage without attacking anything",
    )
    parser.add_argument(
        "--structural",
        action="store_true",
        help="attack the slice with a model that complies with every attack",
    )
    parser.add_argument(
        "--visitors",
        type=int,
        default=_DEFAULT_VISITORS,
        help=f"how many visitors attack it (default: {_DEFAULT_VISITORS})",
    )
    parser.add_argument("--only", nargs="+", help="run only these attack ids")
    parser.add_argument(
        "--session",
        default="adversarial",
        help="prefix for the session id each visitor is run under",
    )
    parser.add_argument(
        "--out", type=Path, help="write the report here instead of to stdout"
    )
    return parser


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    raise SystemExit(main())
