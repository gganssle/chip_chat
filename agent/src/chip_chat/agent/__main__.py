"""``python -m chip_chat.agent`` — which lane is down, from a shell.

The one command to run when somebody says the demo is broken. It builds the
lanes this process can assemble, probes each of them through
:func:`chip_chat.agent.health.probe`, prints
:meth:`~chip_chat.agent.health.HealthReport.render` and exits non-zero if any
wired lane is not answering — so it is equally usable by a person at the stand
and by a readiness check that only reads exit codes.

WHAT IT CAN SEE, WHICH IS THE HONEST PART. Lanes are assembled by the request
path and handed to the agent as a value; nothing in this package builds a
retriever, a pool or a vision client, and the argument for that is in
:mod:`chip_chat.agent.lanes`. So run bare, this reports the deployment's
configured wiring, which today is the week-one slice: no lane wired, every tool
that needs one withdrawn, and the three hardcoded reads still answering. That is
not a placeholder output — it is the correct answer to "is the account lane
down", and the answer is that there is no account lane on this deployment.

Run inside a process that *has* assembled lanes — a REPL in the container, or a
test — :func:`report` takes them and probes for real:

.. code-block:: python

    from chip_chat.agent.__main__ import report
    print(report(lanes, session_id=session_id).render())

The HTTP form of the same thing is ``GET /healthz/lanes``, and mounting it is
four lines in :func:`chip_chat.api.app.create_app`, where the assembled
:class:`~chip_chat.agent.lanes.Lanes` already lives.
"""

import json
import sys
from collections.abc import Sequence

from chip_chat.agent.health import HealthReport, probe
from chip_chat.agent.lanes import NO_LANES, Lanes

__all__ = ["main", "report"]

_PROBE_SESSION = "lane-health-probe"
"""The session id the probe checks out under.

Not a visitor: the Snowflake-backed lanes take a session id and hand it to #44's
pool, and the pool resolves it against the server-side store. A session the
store does not know is refused, which is the pool doing its job -- so a bare
run reports those lanes as down for a reason that names the store rather than
the warehouse, and that is the truth about a probe run from outside a request.
"""


def report(lanes: Lanes = NO_LANES, *, session_id: str = _PROBE_SESSION) -> HealthReport:
    """Probe ``lanes`` and return the report.

    Args:
        lanes: The backing services to probe. The default is what this process
            can assemble on its own, which is nothing -- see the module
            docstring.
        session_id: A session the visitor store knows, for the lanes that check
            out a connection.

    Returns:
        The report.
    """
    return probe(lanes, session_id=session_id)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the report and return an exit code.

    Args:
        argv: Command-line arguments. ``--json`` renders
            :meth:`~chip_chat.agent.health.HealthReport.as_dict` instead of the
            table, for a check that parses rather than reads.

    Returns:
        ``0`` if every wired lane answers, ``1`` otherwise. Staleness is not a
        failure here: a stale mart is a lane that is up and a nightly job that
        is not, and a readiness probe that failed on it would take a working
        deployment out of rotation. It is printed, loudly, either way.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    result = report()
    if "--json" in arguments:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print(result.render())
    return 0 if result.healthy else 1


if __name__ == "__main__":  # pragma: no cover - the entry point itself
    raise SystemExit(main())
