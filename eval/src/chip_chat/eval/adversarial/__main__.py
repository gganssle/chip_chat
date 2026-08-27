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

``--live URL`` is #82's first acceptance criterion -- *run against the deployed
public app* -- and it is the only mode whose answer is about a deployment rather
than about this repository's own code. The two above import the agent loop and
call it, which measures the loop and says nothing about the request handler, the
session cookie or the connection pool serving the URL; those are exactly where
RFC-001 section 05's bleed lives. Under ``--live`` there is a socket on the far
side and the adapter declares only what it could demonstrate about the
deployment, so the capabilities in the report are a measurement rather than a
constant somebody wrote. See :mod:`chip_chat.eval.adversarial.live`, and pass
``--pool-slots`` or the concurrent rounds are unscored.

Without any of the three it runs against the slice on a real deployment and
writes the baseline. That spends money, at least one model call per attack per
visitor.

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

``--fail-on breach`` relaxes exactly that, for exactly one caller, and #82's
fourth acceptance criterion is why it exists: *the suite runs in CI and blocks a
deploy on any failure*. A **blocking** step cannot use the strict rule today,
because the first gate is unmeasurable against a deployment serving one hardcoded
account to everybody -- so the step would be red on every pull request until the
identity path lands end to end, and a step that is always red is a step somebody
switches off. Under ``--fail-on breach`` the step is green today, turns red the
instant anything actually gets out, and prints every unmeasured gate to stderr on
its way past. The strict rule still runs beside it, non-blocking, where a person
reads the number. See :data:`_FAIL_ON_GATE`.

``--rounds`` is #82's second criterion: *the concurrency test runs long enough
and hot enough to genuinely interleave*. One round is the single burst #30
shipped; :data:`~chip_chat.eval.adversarial.soak.DEFAULT_ROUNDS` is a sustained
one, and either way the report says how hot it actually got rather than how hot
it was asked to be.
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
from chip_chat.eval.adversarial.live import LiveTarget
from chip_chat.eval.adversarial.report import build_report, render
from chip_chat.eval.adversarial.run import Target, run_suite
from chip_chat.eval.adversarial.scoring import Scores
from chip_chat.eval.adversarial.slice import SliceTarget
from chip_chat.eval.adversarial.soak import DEFAULT_ROUNDS
from chip_chat.eval.adversarial.testing import CapitulatingModel
from chip_chat.eval.adversarial.writegate import WriteGate

_DEFAULT_VISITORS = 3

_FAIL_ON_GATE = "gate"
_FAIL_ON_BREACH = "breach"
"""The two exit rules, and the reason there are two.

``gate`` is the strict one and the default: a run exits non-zero unless both
launch gates read ``pass``, which includes *not measured*. It is the rule PRD
section 12 implies and the one a release has to clear.

``breach`` fails only on something that actually got out. It exists for the one
place the strict rule is unusable: a CI step that must **block** a merge. The
first gate is unmeasurable against a deployment serving one hardcoded account to
everybody, so a blocking step under the strict rule would be red on every pull
request until the identity path lands end to end -- and a step that is always red
is a step somebody switches off, which is how a suite stops running at exactly
the moment it starts being able to catch something. Under ``breach`` the step is
green today, red the instant anything leaks, and the strict rule still runs
beside it where a human reads the number.
"""


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

    if args.write_gate:
        return _write_gate(args)

    target = _target(args)
    if target is None:
        return 1

    run = run_suite(suite, target, only=args.only, rounds=args.rounds)
    report = build_report(suite, run)
    document = render(report)
    if args.out is not None:
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    return _status(report.scores, args.fail_on)


def _status(scores: Scores, rule: str) -> int:
    """The exit status under one of the two rules. See :data:`_FAIL_ON_GATE`.

    Args:
        scores: What the run produced.
        rule: ``gate`` or ``breach``.

    Returns:
        ``0`` or ``1``. Under ``breach`` the unmeasured gates are reported on
        stderr rather than swallowed: the step passes, and the reason it could
        pass without having measured anything stays in the log where the person
        reading a green tick can see it.
    """
    if rule == _FAIL_ON_BREACH:
        for gate in scores.gates:
            if gate.passes is None:
                print(
                    f"note: {gate.spec.name} was not measured "
                    f"({gate.unscored} of {gate.total} attempts unscored); this "
                    "step blocks on a breach, not on an unmeasured gate",
                    file=sys.stderr,
                )
        return 1 if any(gate.breached for gate in scores.gates) else 0
    return 0 if scores.gates_pass else 1


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


def _write_gate(args: argparse.Namespace) -> int:
    """#83, attacked at the door: request shapes rather than sentences.

    A separate command and a separate report, because it measures a different
    thing from the manifest. Every attack in ``attacks.json`` is something a
    visitor could type; every probe in
    :mod:`chip_chat.eval.adversarial.writegate` is a request body a client
    composes, and the confirmation the second launch gate turns on does not
    travel in a message at all.

    Returns:
        ``0`` where the gate passed, ``1`` where anything executed **or** where
        any probe could not be put. Unscored blocks, for the reason the strict
        rule in :data:`_FAIL_ON_GATE` gives.
    """
    gate = WriteGate(base=args.write_gate, ttl_seconds=args.draft_ttl, pace=args.pace)
    report = gate.run(only=args.only)
    document = report.render()
    if args.out is not None:
        args.out.write_text(document, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(document)
    if args.fail_on == _FAIL_ON_BREACH:
        for finding in report.unscored:
            print(
                f"note: {finding.probe.probe_id} was not measured "
                f"({finding.detail}); this step blocks on a write, not on an "
                "unmeasured probe",
                file=sys.stderr,
            )
        return 1 if report.breached else 0
    return 0 if report.gate else 1


def _target(args: argparse.Namespace) -> Target | None:
    """What to attack, or ``None`` where nothing can be built.

    ``--live`` is #82's first acceptance criterion and it beats the other two
    modes, because it is the only one whose answer is about a deployment rather
    than about this repository's own code. See
    :mod:`chip_chat.eval.adversarial.live`.
    """
    if args.live:
        return LiveTarget(
            base=args.live,
            visitors=args.visitors,
            pool_slots=args.pool_slots,
            pace=args.pace,
        )
    model = _model(args)
    if model is None:
        return None
    return SliceTarget(model, visitors=args.visitors, session_prefix=args.session)


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
        "--live",
        metavar="URL",
        help=(
            "attack a deployed app over HTTP instead of the in-process slice "
            "(#82's first criterion). Spends the deployment's tokens, not yours"
        ),
    )
    parser.add_argument(
        "--pool-slots",
        type=int,
        default=None,
        help=(
            "how many connections the deployment pools, for --live. Omitting it "
            "CLAIMS THE DEPLOYMENT DOES NOT POOL, which makes a contended "
            "concurrent round unscored rather than clean"
        ),
    )
    parser.add_argument(
        "--visitors",
        type=int,
        default=_DEFAULT_VISITORS,
        help=f"how many visitors attack it (default: {_DEFAULT_VISITORS})",
    )
    parser.add_argument("--only", nargs="+", help="run only these attack ids")
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help=(
            "turns per visitor in each concurrent attack's round "
            f"(default: 1; {DEFAULT_ROUNDS} is the sustained run #82 asks for)"
        ),
    )
    parser.add_argument(
        "--write-gate",
        metavar="URL",
        help=(
            "attack launch gate two at the door instead of through the model "
            "(#83): unconfirmed, cross-session, forged, replayed and expired "
            "draft references, composed as request bodies"
        ),
    )
    parser.add_argument(
        "--draft-ttl",
        type=float,
        default=0.0,
        help=(
            "the deployment's draft time-to-live in seconds, for --write-gate. "
            "Omitting it leaves the expiry probe UNSCORED rather than skipping "
            "it quietly; 900 is this tree's default"
        ),
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=0.0,
        help=(
            "seconds between one visitor's turns, for --live, to stay under the "
            "deployment's per-source rate limit. Without it every turn past the "
            "limit comes back as the stop state and is recorded as unmeasured"
        ),
    )
    parser.add_argument(
        "--fail-on",
        choices=(_FAIL_ON_GATE, _FAIL_ON_BREACH),
        default=_FAIL_ON_GATE,
        help=(
            "what makes this command exit non-zero: any gate short of `pass` "
            "including not-measured (default), or only something that got out"
        ),
    )
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
