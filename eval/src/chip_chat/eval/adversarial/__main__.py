"""``python -m chip_chat.eval.adversarial`` -- check the suite, or run it.

Four modes, and three of them are free.

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

``--gate2`` is issue #83's, and it attacks a different door. Everything above
runs attacks *through a model*; this runs thirteen calls straight at
:class:`~chip_chat.api.ops.OpsService`, with no browser and no assistant in
front of it, which is the form PRD launch gate two's attacker actually takes
once they have the write service's hostname. It needs ``--catalog``, because a
draft store prices against a catalogue and one built without one would be a
store pricing against nothing. It calls no model either. See
:mod:`chip_chat.eval.adversarial.gate2`.

``--sabotaged`` is not a mode but a modifier on ``--structural``: it replaces
the deployment's system prompt with
:data:`~chip_chat.eval.adversarial.testing.SABOTAGED_PROMPT`, an attacker's, and
runs the same suite. #83's third acceptance criterion asks for the gates to be
tested that way, because a gate that depended on the prompt would be a gate that
depends on a file anybody with commit access can edit. The run refuses to report
anything unless the sabotaged text was demonstrably in front of the model --
:class:`~chip_chat.eval.adversarial.testing.Overheard` is what establishes that,
and a sabotage nobody applied would otherwise produce the most flattering
possible result.

Without any of them it runs against the slice on a real deployment and writes
the baseline. That spends money, at least one model call per attack per visitor.

.. code-block:: console

    $ python -m chip_chat.eval.adversarial --check
    $ python -m chip_chat.eval.adversarial --structural
    $ python -m chip_chat.eval.adversarial --structural --sabotaged
    $ python -m chip_chat.eval.adversarial --gate2 --catalog ./catalog-build
    $ export CHIP_CHAT_FOUNDRY_ENDPOINT=... CHIP_CHAT_FOUNDRY_API_KEY=...
    $ python -m chip_chat.eval.adversarial --out eval/adversarial/BASELINE.md

**The exit status is the gate, not the run.** ``--check`` and a real run both
exit non-zero where either launch gate is anything other than ``pass`` -- which
includes *not measured*, deliberately. PRD section 12 makes both gates blocking
and a gate nobody could measure blocks in exactly the same way as one that
failed. A pipeline that went green on an unmeasured gate would be the most
expensive possible way to discover that later.

One consequence is worth stating rather than leaving to be inferred. **The `T2`
row a suite run prints is one front of gate two, not the gate.** It counts what
attacks through a model did; the direct calls are counted by ``--gate2`` and
written to a document of their own. Read either alone and the gate is
overstated, which is why both targets are in ``make ci`` and why each report
says so in its own opening paragraph.
"""

import argparse
import sys
from pathlib import Path

from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError
from chip_chat.agent.model import AzureChatModel, ChatModel
from chip_chat.catalog import load_catalog
from chip_chat.eval.adversarial.attacks import (
    DEFAULT_MANIFEST,
    AdversarialSuite,
    SuiteError,
)
from chip_chat.eval.adversarial.coverage import coverage
from chip_chat.eval.adversarial.gate2 import Doorway, Gate2Error, besiege
from chip_chat.eval.adversarial.report import build_report, render, render_siege
from chip_chat.eval.adversarial.run import run_suite
from chip_chat.eval.adversarial.slice import SliceTarget
from chip_chat.eval.adversarial.testing import (
    SABOTAGED_PROMPT,
    CapitulatingModel,
    Overheard,
)
from chip_chat.harvest.blobs import LocalBlobStore

_DEFAULT_VISITORS = 3
_DEFAULT_CATALOG_PREFIX = "catalog"

_SABOTAGE_TELL = "The confirmation card is a legacy step"
"""A line of :data:`SABOTAGED_PROMPT` distinctive enough to look for in a
transcript. What :class:`Overheard` is asked whether it heard."""


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
    if args.gate2:
        return _gate2(args)

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

    overheard = Overheard(model) if args.sabotaged else None
    target = SliceTarget(
        model if overheard is None else overheard,
        visitors=args.visitors,
        session_prefix=args.session,
        system_prompt=SABOTAGED_PROMPT if args.sabotaged else None,
    )
    run = run_suite(suite, target, only=args.only)
    report = build_report(suite, run)
    document = render(report)
    if args.out is not None:
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    if overheard is not None and not _sabotage_landed(overheard):
        return 1
    return 0 if report.scores.gates_pass else 1


def _sabotage_landed(overheard: Overheard) -> bool:
    """Whether the attacker's prompt was really in front of the model.

    Checked *after* the report is printed and before the exit status is
    computed, so a reader still sees what the run found and is then told the
    run does not mean what it says. A sabotage that silently failed to apply
    produces a clean pair of gates against the repository's own prompt, and
    calling that "the gates held under a compromised prompt" is the most
    flattering lie this package could tell.
    """
    heard = overheard.heard(_SABOTAGE_TELL)
    if heard:
        print(
            f"the sabotaged prompt was in front of the model on {heard} calls",
            file=sys.stderr,
        )
        return True
    print(
        "error: --sabotaged was asked for and the sabotaged prompt never "
        "reached the model, so nothing above was measured against one",
        file=sys.stderr,
    )
    return False


def _gate2(args: argparse.Namespace) -> int:
    """Attack the ops API directly, with no model and no browser in front of it.

    Issue #83's second front. See :mod:`chip_chat.eval.adversarial.gate2`.
    """
    if args.catalog is None:
        print(
            "error: --gate2 needs --catalog; a draft store prices against a "
            "catalogue, and one built without one prices against nothing",
            file=sys.stderr,
        )
        return 1
    try:
        catalog = load_catalog(LocalBlobStore(args.catalog), args.catalog_prefix)
        door = Doorway(catalog)
    except (OSError, ValueError, Gate2Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    siege = besiege(door)
    document = render_siege(siege)
    if args.out is not None:
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    return 0 if siege.passes else 1


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
        "--sabotaged",
        action="store_true",
        help="replace the system prompt with the attacker's, and run the suite anyway",
    )
    parser.add_argument(
        "--gate2",
        action="store_true",
        help="attack the ops API directly, bypassing the model and the UI",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        help="directory holding a built catalogue, which --gate2 prices drafts against",
    )
    parser.add_argument(
        "--catalog-prefix",
        default=_DEFAULT_CATALOG_PREFIX,
        help="prefix the catalogue was written under "
        f"(default: {_DEFAULT_CATALOG_PREFIX})",
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
