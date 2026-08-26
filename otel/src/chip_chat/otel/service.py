"""Resource naming shared by every component that emits telemetry.

``service.name`` is the attribute App Insights groups by and Arize projects on,
so it is derived in one place instead of spelled out at each export site.
"""

import re

SERVICE_NAMESPACE = "chip-chat"
"""Value for the OpenTelemetry ``service.namespace`` resource attribute."""

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
