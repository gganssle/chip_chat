"""The agent image's entrypoint.

Decision D8 made Cilantro's agent a *hosted* agent -- our container, run by
Foundry Agent Service -- and this module is what that container runs. The agent's
own loop and its eleven tools arrive with issue #60; what exists here is the part
that has to be right before there is anything to put in it.

.. code-block:: bash

    python -m chip_chat.agent.container check
    python -m chip_chat.agent.container agent-half

``check``
    What is in this image and where its spans go. Run it against a freshly built
    image before registering an agent version around it: an image whose exporter
    is unconfigured registers just as happily as one whose exporter is right, and
    the environment variables are immutable per agent version, so the mistake
    costs a version to correct.

``agent-half``
    Emit the agent's half of one turn, joined to a turn that is open in *another
    process*. The carrier arrives in ``TRACEPARENT`` / ``TRACESTATE`` / ``BAGGAGE``
    or in ``--header`` arguments. This is issue #103's acceptance criterion made
    runnable: with the app half in one process and this in a container, one
    visitor turn produces one connected trace across two ``service.name`` values,
    and you read it out of the backend rather than out of a unit test.

    .. code-block:: bash

        CHIP_CHAT_AGENT_COMMAND='docker run --rm --network host \\
            -e OTEL_EXPORTER_OTLP_ENDPOINT -e TRACEPARENT -e TRACESTATE -e BAGGAGE \\
            chip-chat-agent:dev agent-half' make trace-boundary

**On ``service.name``.** Foundry forces it to the agent resource's name and
ignores ``OTEL_SERVICE_NAME``, so this process does not get to choose it either.
:func:`~chip_chat.otel.service.agent_service_name` reads the name from
``CHIP_CHAT_AGENT_NAME`` so that what the container reports and what the platform
stamps are the same string -- and ``check`` prints it, because a dashboard
filtered on the wrong one shows half a turn and looks healthy doing it.
"""

import argparse
import sys
from collections.abc import Mapping, Sequence

from chip_chat.otel.boundary import agent_configuration, agent_half
from chip_chat.otel.propagation import (
    ENVIRONMENT_CARRIER,
    TurnContextError,
    carrier_from_environment,
)
from chip_chat.otel.service import turn_service_names
from chip_chat.otel.tracing import configure_tracing, shutdown_tracing

__all__ = ["main"]


def _parse_headers(pairs: Sequence[str]) -> dict[str, str]:
    """Parse repeated ``--header name=value`` arguments."""
    headers: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator:
            raise SystemExit(f"--header expects name=value, got {pair!r}")
        headers[name.strip().lower()] = value.strip()
    return headers


def _report(carrier: Mapping[str, str]) -> int:
    """Print what this image is and where its spans go."""
    from chip_chat.agent import __version__

    config = agent_configuration()
    app_service, agent_service = turn_service_names()

    print(f"chip-chat agent image · package {__version__}")
    print(f"  service.name    {agent_service}")
    print(f"  the other one   {app_service}")
    print(f"  environment     {config.environment}")
    print(f"  otlp            {config.otlp_endpoint or '(unset)'}")
    print(
        "  app insights    "
        + ("(platform-injected)" if config.azure_monitor_connection_string else "(unset)")
    )
    print(f"  carrier         {carrier or '(none in the environment)'}")

    if not config.exports_anywhere:
        print(
            "\nNothing is configured to export to. Spans are still built and still "
            "schema-checked,\nbut they go nowhere -- and OTEL_EXPORTER_OTLP_* is "
            "immutable per agent version, so\nregistering a version around this "
            "image would cost a version to correct.",
            file=sys.stderr,
        )
    return 0


def _emit_agent_half(carrier: Mapping[str, str], *, pause: float) -> int:
    """Emit the agent's half of a turn that is open in another process."""
    configure_tracing(agent_configuration())
    try:
        agent_half(carrier, pause=pause)
    except TurnContextError as error:
        print(f"the turn did not reach this container: {error}", file=sys.stderr)
        return 1
    finally:
        # A container that exits before the batch processor flushes looks exactly
        # like a boundary that did not work.
        shutdown_tracing()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one entrypoint action. Returns a process exit code."""
    parser = argparse.ArgumentParser(
        prog="python -m chip_chat.agent.container",
        description="The Cilantro agent image's entrypoint.",
    )
    parser.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help=(
            "one carrier header. Repeatable. Defaults to reading "
            + "/".join(ENVIRONMENT_CARRIER.values())
            + " from the environment."
        ),
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=0.0,
        help="seconds to spend inside each span, so the waterfall is legible",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="check",
        choices=["check", "agent-half"],
        help="what to do; defaults to check",
    )
    options = parser.parse_args(argv)

    carrier = _parse_headers(options.header) or carrier_from_environment()
    if options.action == "check":
        return _report(carrier)
    return _emit_agent_half(carrier, pause=options.pause)


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
