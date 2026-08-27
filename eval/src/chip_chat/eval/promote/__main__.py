"""``python -m chip_chat.eval.promote`` -- a trace to a dataset entry, in two steps.

#77's first acceptance criterion is a stopwatch, so the command is shaped around
it. Two invocations, one edit between them, and everything a trace can supply
already filled in:

.. code-block:: console

    $ python -m chip_chat.eval.promote --draft trace.json > case.json
    $ $EDITOR case.json          # three fields: tool+lane, requirements, why
    $ python -m chip_chat.eval.promote --apply case.json
    $ make dataset               # the version moves, once, visibly

``--check`` is the free one and the one CI runs. It reads the ledger, reports how
many entries came from real traffic, and holds every permanent source to its
manifest -- so an attack added to the adversarial suite without being recorded as
a permanent regression entry is a build failure rather than a promise nobody
kept.

``--drafts`` does the same as ``--draft`` for a whole capture of live turns,
emitting one draft per turn the monitors flagged. That is #77's *selection driven
by the monitors* as a command: the interesting traces select themselves, and the
person doing the labelling starts from a shortlist rather than from a trace
viewer.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from chip_chat.eval.adversarial.attacks import DEFAULT_MANIFEST as ATTACKS_MANIFEST
from chip_chat.eval.adversarial.attacks import AdversarialSuite, SuiteError
from chip_chat.eval.golden.cases import DEFAULT_MANIFEST as GOLDEN_MANIFEST
from chip_chat.eval.online.monitors import evaluate
from chip_chat.eval.promote.apply import PromotionError, apply_draft, traffic_entries
from chip_chat.eval.promote.candidates import draft, from_alerts
from chip_chat.eval.promote.ledger import (
    DEFAULT_LEDGER,
    LedgerError,
    check,
    load,
)
from chip_chat.eval.trajectory.trees import TraceSpan

ADVERSARIAL_SOURCE = "adversarial-suite"
"""What the ledger calls the adversarial suite. #77's third criterion joins here."""

TRAFFIC_TARGET = 10
"""#77's second acceptance criterion: at least ten entries from real traffic."""


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` on success; ``1`` where a permanent source has
        drifted from its manifest, a draft cannot become a case, or a capture
        cannot be read. A drifted permanent source exits non-zero deliberately:
        it is exactly the state #77's third criterion exists to prevent.
    """
    args = _parser().parse_args(argv)
    if args.draft is not None:
        return _draft_one(args)
    if args.drafts is not None:
        return _draft_many(args)
    if args.apply is not None:
        return _apply(args)
    return _check(args)


def _check(args: argparse.Namespace) -> int:
    """Report the ledger and hold every permanent source to its manifest."""
    try:
        provenance = load(args.ledger)
    except LedgerError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    from_traffic = traffic_entries(provenance)
    print(
        f"{len(provenance.promotions)} recorded promotion(s), "
        f"{from_traffic} from real traffic (target {TRAFFIC_TARGET})"
    )
    if from_traffic < TRAFFIC_TARGET:
        print(
            f"  note: {TRAFFIC_TARGET - from_traffic} short of #77's second "
            "criterion. The path exists and is measured; what is missing is "
            "traffic, and PRD §12 puts online evals before the URL is shared."
        )

    try:
        suite = AdversarialSuite.load(args.attacks)
    except SuiteError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    sources = {ADVERSARIAL_SOURCE: [attack.attack_id for attack in suite.attacks]}
    problems = check(provenance, sources)
    recorded = provenance.source(ADVERSARIAL_SOURCE)
    if recorded is not None:
        print(
            f"permanent: {recorded.name} — {len(recorded.ids)} case(s), "
            f"run by `{recorded.runs_in}`"
        )
    for line in problems:
        print(f"  MISSING   {line}")
    return 1 if problems else 0


def _draft_one(args: argparse.Namespace) -> int:
    """Emit one draft from one captured trace."""
    try:
        turns = _capture(args.draft)
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"error: cannot read {args.draft}: {error}", file=sys.stderr)
        return 1
    if not turns:
        print(f"error: {args.draft} holds no turns", file=sys.stderr)
        return 1
    turn = turns[0]
    body = draft(from_alerts(turn, evaluate(turn)))
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0


def _draft_many(args: argparse.Namespace) -> int:
    """Emit one draft per flagged turn in a capture."""
    try:
        turns = _capture(args.drafts)
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        print(f"error: cannot read {args.drafts}: {error}", file=sys.stderr)
        return 1
    drafts: list[dict[str, Any]] = []
    for turn in turns:
        alerts = evaluate(turn)
        if alerts or args.all:
            drafts.append(draft(from_alerts(turn, alerts)))
    print(json.dumps({"cases": drafts}, indent=2, ensure_ascii=False))
    return 0


def _apply(args: argparse.Namespace) -> int:
    """Add a labelled draft to the set and record where it came from."""
    try:
        body = json.loads(args.apply.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: cannot read {args.apply}: {error}", file=sys.stderr)
        return 1
    bodies = body["cases"] if isinstance(body, dict) and "cases" in body else [body]
    added: list[str] = []
    for one in bodies:
        try:
            added.append(
                apply_draft(
                    one,
                    manifest=args.golden,
                    ledger_path=args.ledger,
                    source=args.source,
                )
            )
        except PromotionError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    print(f"added {len(added)} case(s) to {args.golden}: {', '.join(added)}")
    print(f"recorded provenance in {args.ledger}")
    print("next: make dataset   # the dataset version moves, once, visibly")
    return 0


def _capture(path: Path) -> tuple[Any, ...]:
    """Read a capture of live turns. Same shape as the online runner's."""
    from chip_chat.eval.online.signals import read_turn

    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        read_turn(
            _spans(entry.get("spans", ())),
            message=str(entry.get("message", "")),
            reply=str(entry.get("reply", "")),
        )
        for entry in payload["turns"]
    )


def _spans(entries: Sequence[Any]) -> tuple[TraceSpan, ...]:
    return tuple(
        TraceSpan(
            name=str(entry["name"]),
            span_id=str(entry["span_id"]),
            parent_id=entry.get("parent_id") or None,
            trace_id=str(entry["trace_id"]),
            attributes=dict(entry.get("attributes") or {}),
            service=entry.get("service"),
            started=int(entry.get("started", 0)),
        )
        for entry in entries
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.eval.promote",
        description="Promote a production trace into a golden-set entry.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "report the ledger and hold every permanent source to its "
            "manifest; the default"
        ),
    )
    parser.add_argument(
        "--draft",
        type=Path,
        metavar="CAPTURE",
        help="emit a case draft from the first turn in a capture",
    )
    parser.add_argument(
        "--drafts",
        type=Path,
        metavar="CAPTURE",
        help="emit one draft per flagged turn in a capture",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="with --drafts, emit a draft for every turn rather than the flagged ones",
    )
    parser.add_argument(
        "--apply",
        type=Path,
        metavar="DRAFT",
        help="add a labelled draft to the golden set",
    )
    parser.add_argument(
        "--source",
        default="production",
        help="where the applied entries came from, for the ledger (default: production)",
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=GOLDEN_MANIFEST,
        help=f"the golden case manifest (default: {GOLDEN_MANIFEST})",
    )
    parser.add_argument(
        "--attacks",
        type=Path,
        default=ATTACKS_MANIFEST,
        help=f"the adversarial manifest (default: {ATTACKS_MANIFEST})",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=DEFAULT_LEDGER,
        help=f"the provenance ledger (default: {DEFAULT_LEDGER})",
    )
    return parser


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    raise SystemExit(main())
