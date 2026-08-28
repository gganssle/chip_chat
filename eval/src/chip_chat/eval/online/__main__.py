"""``python -m chip_chat.eval.online`` -- the policy, the drill, or live traffic.

Three modes, and the first two are free.

``--check`` prints the sampling policy, every monitor with the sentence from #76
it implements, and the judge budget read from the environment. It calls nothing.
Its exit status is the one thing in this package that is a gate: it exits
non-zero when ``CHIP_CHAT_DAILY_TOKEN_CEILING`` is unset, because a monitoring
loop whose token spend is unaccounted is the hole #76's last acceptance criterion
names, and a check that shrugged at it would be the criterion satisfied by a
paragraph.

``--drill`` produces every feared condition deliberately and reports which
monitor caught it. Free, no credentials, no model. This is #76's *each monitor
tested by producing the condition* as a command rather than as a claim, and it
exits non-zero if any monitor fails to fire on its own condition — a monitor that
stopped working is indistinguishable from quiet traffic until somebody produces
the condition.

Without either flag it runs the loop over live turns, from one of two sources.
``--phoenix`` reads a window of production traffic straight out of the deployed
observability backend, which is what the scheduled job in
``infra/terraform/observability.tf`` does every quarter of an hour. ``--capture``
reads a file somebody produced earlier, which is what a laptop does when it wants
yesterday's traffic and today's monitors. Both arrive as the same
:class:`~chip_chat.eval.online.signals.LiveTurn` and nothing downstream can tell
which one it was.

``--fail-on`` is where an alert becomes something a machine can notice. This
package deliberately does not *deliver* alerts — the route is somebody's action
group and putting a delivery mechanism inside an eval makes the eval untestable
and the delivery unowned — but the *caller* routes them, and for the scheduled
job the exit status is the route: a failed job execution is visible in
``az containerapp job execution list`` and is a thing Azure Monitor can alert on
without this package knowing anything about Azure Monitor.

.. code-block:: console

    $ python -m chip_chat.eval.online --check
    $ python -m chip_chat.eval.online --drill
    $ python -m chip_chat.eval.online --capture eval/online/captures/day-one.json --judge
    $ python -m chip_chat.eval.online --phoenix http://phoenix.internal:6006 \
        --lookback-minutes 20 --judge --fail-on page
"""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from chip_chat.agent.foundry import FoundryConfig, FoundryConfigError
from chip_chat.agent.model import AzureChatModel
from chip_chat.eval.grounding.judge import ModelJudge
from chip_chat.eval.online.budget import CEILING_VARIABLE, budget_from_env
from chip_chat.eval.online.monitors import MONITORS, Alert, Severity, evaluate
from chip_chat.eval.online.phoenix import (
    DEFAULT_PROJECT as PHOENIX_PROJECT,
)
from chip_chat.eval.online.phoenix import (
    PhoenixError,
    read_live_turns,
)
from chip_chat.eval.online.run import run_online
from chip_chat.eval.online.sampling import DEFAULT_RATE, SamplingPolicy
from chip_chat.eval.online.signals import LiveTurn
from chip_chat.eval.online.testing import drills
from chip_chat.eval.trajectory.trees import TraceSpan

PROJECTED_TURNS = 500
"""Turns a day the budget line is projected over.

A demo that is genuinely being looked at. Not a forecast -- the projection is
arithmetic on a number somebody chose, and the number is here so it can be
argued with rather than buried in a spreadsheet.
"""


def main(argv: list[str] | None = None) -> int:
    """Run the command.

    Args:
        argv: Arguments, for a test that drives this without a subprocess.

    Returns:
        The exit status. ``0`` on success; ``1`` where the judge budget is
        unaccounted, a drill's monitor did not fire, a capture cannot be read,
        the backend cannot be reached, or an alert at or above ``--fail-on``
        fired.
    """
    args = _parser().parse_args(argv)
    policy = SamplingPolicy(rate=args.rate)

    if args.drill:
        return _drill(policy)
    if args.check or (args.capture is None and args.phoenix is None):
        return _check(policy, args.measured_tokens)
    return _live(args, policy)


def _check(policy: SamplingPolicy, measured_tokens: int) -> int:
    """Print the policy, the monitors and the budget. Calls nothing.

    Args:
        policy: The sampling policy in force.
        measured_tokens: What one judged turn cost, from a real run. Passed
            in rather than estimated: the whole argument of
            :mod:`chip_chat.eval.online.budget` is that a projection is what
            left the hole, so a check with no measurement reports the share
            as unaccounted rather than computing one from a guess.

    Returns:
        ``0``, or ``1`` where the daily ceiling is unset.
    """
    print(f"sampling: {policy.describe()}")
    print(f"monitors: {len(MONITORS)}")
    for item in MONITORS:
        kind = "judged" if item.judged else "deterministic"
        print(f"  {item.severity.value:<9} {item.name} ({kind})")
        print(f"            {item.fear}")
    budget = budget_from_env(policy, tokens_per_judged_turn=measured_tokens)
    print(f"budget: {budget.describe(PROJECTED_TURNS)}")
    affordable = budget.conversations_affordable()
    if affordable is not None:
        print(
            f"        the judges alone leave room for {affordable:,} conversations a day"
        )
    if budget.daily_ceiling is None:
        print(
            f"error: {CEILING_VARIABLE} is unset, so judge spend is not "
            "accounted inside the daily cap (#76). Export it — the same value "
            "the request path enforces.",
            file=sys.stderr,
        )
        return 1
    return 0


def _drill(policy: SamplingPolicy) -> int:
    """Produce every feared condition and report which monitor caught it."""
    status = 0
    for drill in drills():
        alerts = evaluate(drill.turn, grounded=drill.grounded, declined=drill.declined)
        caught = [alert for alert in alerts if alert.monitor == drill.monitor.name]
        decision = policy.decide(drill.turn, flagged=bool(alerts))
        mark = "ok      " if caught else "MISSED  "
        if not caught:
            status = 1
        print(f"  {mark}  {drill.name}")
        print(f"            condition: {drill.why}")
        for alert in caught:
            print(f"            fired:     [{alert.severity.value}] {alert.detail}")
        if not caught:
            print("            fired:     nothing")
        print(
            f"            sampling:  "
            f"{'judged' if decision.judged else 'not judged'} ({decision.reason.value})"
        )
    if status:
        print(
            "error: a monitor did not fire on its own condition; see above",
            file=sys.stderr,
        )
    return status


def _live(args: argparse.Namespace, policy: SamplingPolicy) -> int:
    """Run the loop over live turns, from a backend or from a capture."""
    if args.phoenix:
        try:
            turns = read_live_turns(
                args.phoenix,
                lookback_minutes=args.lookback_minutes,
                project=args.project,
            )
        except PhoenixError as error:
            # Loudly, and non-zero. A monitoring loop that cannot reach its
            # trace source and reports "0 turns, 0 alerts" is worse than one
            # that fails: the first is indistinguishable from quiet traffic,
            # which is the state these monitors exist to tell trouble apart
            # from.
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"source: {args.phoenix} project={args.project}")
        print(f"window: the last {args.lookback_minutes:g} minute(s)")
    else:
        try:
            turns = _capture(args.capture)
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
            print(f"error: cannot read {args.capture}: {error}", file=sys.stderr)
            return 1

    judge = None
    if args.judge is not None:
        try:
            config = FoundryConfig.from_env()
        except FoundryConfigError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        if args.judge:
            config = replace(config, chat_deployment=args.judge)
        judge = ModelJudge(AzureChatModel(config))

    run = run_online(turns, policy=policy, judge=judge)
    print(f"{run.turns} turn(s), {run.judged} judged, {run.unreadable} unreadable")
    print(f"sampling: {run.sampling_reasons()}")
    for alert in run.alerts:
        print(
            f"  [{alert.severity.value}] {alert.monitor} {alert.trace_id}: {alert.detail}"
        )
    budget = budget_from_env(policy, tokens_per_judged_turn=run.tokens_per_judged_turn)
    print(f"budget: {budget.describe(PROJECTED_TURNS)}")
    return _route(run.alerts, args.fail_on)


def _route(alerts: Sequence[Alert], fail_on: str | None) -> int:
    """Turn the alerts into an exit status, which is the caller's whole route.

    Severity is an ordered thing here and nowhere else in the package: ``page``
    is worse than ``ticket`` is worse than ``dashboard``, and ``--fail-on
    ticket`` means "and anything worse". The order lives in this function rather
    than on :class:`~chip_chat.eval.online.monitors.Severity` deliberately —
    what *counts* as bad enough to interrupt somebody is a routing decision and
    the package does not own routing.

    Args:
        alerts: What fired.
        fail_on: The least severity that should fail the run, or ``None`` to
            report and always exit zero.

    Returns:
        ``2`` where an alert at or above ``fail_on`` fired, ``0`` otherwise.
        Two rather than one so that a run which failed *because it found
        something* is distinguishable in a job's exit status from a run that
        could not read its input.
    """
    if not fail_on:
        return 0
    order = [Severity.DASHBOARD.value, Severity.TICKET.value, Severity.PAGE.value]
    floor = order.index(fail_on)
    firing = [alert for alert in alerts if order.index(alert.severity.value) >= floor]
    if not firing:
        return 0
    print(
        f"error: {len(firing)} alert(s) at or above '{fail_on}' — "
        "this run's exit status is the route",
        file=sys.stderr,
    )
    return 2


def _capture(path: Path) -> tuple[LiveTurn, ...]:
    """Read a capture file into live turns.

    The shape is one object per turn carrying ``message``, ``reply`` and a
    ``spans`` array, each span a flat object with the fields of
    :class:`~chip_chat.eval.trajectory.trees.TraceSpan`. That is deliberately
    the *reader's* shape rather than any backend's: a backend adapter is a
    function that produces this, and #74's module docstring already says a second
    adapter is a function while a second reader would be a second implementation
    of the metric.
    """
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
        prog="python -m chip_chat.eval.online",
        description="Online evals and monitors against live traffic.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the policy, the monitors and the budget; the default",
    )
    parser.add_argument(
        "--drill",
        action="store_true",
        help="produce every feared condition and report which monitor caught it",
    )
    parser.add_argument(
        "--capture",
        type=Path,
        help="a capture of live turns to run the loop over",
    )
    parser.add_argument(
        "--phoenix",
        metavar="URL",
        help=(
            "read live turns straight from the deployed observability backend; "
            "the same base URL the exporter sends to"
        ),
    )
    parser.add_argument(
        "--lookback-minutes",
        type=float,
        default=20.0,
        help="how far back --phoenix reads (default: 20, against a 15 minute schedule)",
    )
    parser.add_argument(
        "--project",
        default=PHOENIX_PROJECT,
        help=f"the backend project --phoenix reads (default: {PHOENIX_PROJECT})",
    )
    parser.add_argument(
        "--fail-on",
        choices=[severity.value for severity in Severity],
        default=None,
        metavar="SEVERITY",
        help=(
            "exit 2 when an alert of this severity or worse fires; the exit "
            "status is the route, and the package delivers nothing itself"
        ),
    )
    parser.add_argument(
        "--judge",
        nargs="?",
        const="",
        default=None,
        metavar="DEPLOYMENT",
        help="judge the sampled turns; bare uses the configured chat deployment",
    )
    parser.add_argument(
        "--measured-tokens",
        type=int,
        default=0,
        help=("tokens one judged turn cost, from a real run, for the budget line"),
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=DEFAULT_RATE,
        help=f"fraction of ordinary turns to judge (default: {DEFAULT_RATE})",
    )
    return parser


if __name__ == "__main__":  # pragma: no cover -- the entry point itself
    raise SystemExit(main())
