"""Resource naming shared by every component that emits telemetry.

``service.name`` is the attribute App Insights groups by and Arize projects on,
so it is derived in one place instead of spelled out at each export site.

**One turn now carries two of them.** Decision D8 made the agent a hosted agent —
our container, run by Foundry Agent Service — and a hosted agent's
``service.name`` is forced to the agent resource's name, with ``OTEL_SERVICE_NAME``
ignored. So the app emits ``chat.turn``, the guards and ``render.response`` under
one name and the agent emits everything between under another. A dashboard, alert
or evaluation that filters on a single ``service.name`` shows half a turn and
looks healthy doing it, which is why both names are enumerated here by
:func:`turn_service_names` rather than left to each consumer to remember.
"""

import os
import re
from collections.abc import Mapping

__all__ = [
    "AGENT_COMPONENT",
    "AGENT_NAME_VARIABLE",
    "APP_COMPONENT",
    "SERVICE_NAMESPACE",
    "agent_service_name",
    "service_name",
    "turn_service_names",
]

SERVICE_NAMESPACE = "chip-chat"
"""Value for the OpenTelemetry ``service.namespace`` resource attribute."""

APP_COMPONENT = "api"
"""The component that opens a turn: guards, ``chat.turn``, ``render.response``."""

AGENT_COMPONENT = "agent"
"""The component that runs the model loop: ``agent.step`` and everything under it."""

AGENT_NAME_VARIABLE = "CHIP_CHAT_AGENT_NAME"
"""Environment variable naming the Foundry agent resource.

Read rather than assumed because the forced ``service.name`` is the *resource*
name, which is an Azure identifier and not this package's to choose. The default
below is what the agent resource should be called precisely so that the
constraint costs nothing — but "should be" is not "is", and a dashboard built on
a guess is the failure mode this variable exists to prevent.
"""

_COMPONENT = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")


def service_name(component: str) -> str:
    """Return the ``service.name`` for a component of Chip Chat.

    Args:
        component: The package's directory name, e.g. ``"api"`` or ``"data-gen"``.

    Returns:
        The namespaced, hyphenated service name, e.g. ``"chip-chat-data-gen"``.

    Raises:
        ValueError: If ``component`` is not a lowercase, hyphen-separated name.
    """
    if not _COMPONENT.match(component):
        raise ValueError(
            f"component must be lowercase and hyphen-separated, got {component!r}"
        )
    return f"{SERVICE_NAMESPACE}-{component.replace('_', '-')}"


def agent_service_name(env: Mapping[str, str] | None = None) -> str:
    """Return the ``service.name`` the agent's spans actually carry.

    Args:
        env: Environment mapping to read; defaults to :data:`os.environ`.

    Returns:
        The value of :data:`AGENT_NAME_VARIABLE` if it is set, otherwise the name
        this package would have chosen, ``chip-chat-agent``.
    """
    source = os.environ if env is None else env
    configured = source.get(AGENT_NAME_VARIABLE, "").strip()
    return configured or service_name(AGENT_COMPONENT)


def turn_service_names(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return every ``service.name`` one turn's trace spans, app first.

    A filter written against one of these is a filter that hides half a turn.
    Anything grouping traces by service — a dashboard axis, an alert rule, a
    Phase 9 evaluation's trace query — should take its list from here.

    Args:
        env: Environment mapping to read; defaults to :data:`os.environ`.

    Returns:
        ``(app service name, agent service name)``.
    """
    return (service_name(APP_COMPONENT), agent_service_name(env))
