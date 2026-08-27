"""Chat widget and entry flow.

One page, no build step, and every sentence on it in :mod:`chip_chat.web.copy`.
:mod:`chip_chat.api.app` serves what is here; nothing in this package knows how
a request reaches it, and nothing in it holds an identity --
:class:`~chip_chat.web.persona.Persona` is the projection the copy needs and it
has no ``demo_id`` in it.
"""

from chip_chat.otel import service_name
from chip_chat.web.copy import (
    BANNER,
    NAME_GATE_HINT,
    NAME_GATE_TITLE,
    PHOTO_RETENTION,
    SIMULATED,
    STOP_STATE_HEADING,
    SWITCH_LABEL,
    TITLE,
)
from chip_chat.web.page import chat_page, stop_page
from chip_chat.web.persona import (
    Chip,
    Persona,
    opening_message,
    restart_message,
    suggestions,
    unbound_opening_message,
)

__all__ = [
    "BANNER",
    "NAME_GATE_HINT",
    "NAME_GATE_TITLE",
    "PHOTO_RETENTION",
    "SERVICE_NAME",
    "SIMULATED",
    "STOP_STATE_HEADING",
    "SWITCH_LABEL",
    "TITLE",
    "Chip",
    "Persona",
    "__version__",
    "chat_page",
    "opening_message",
    "restart_message",
    "service_name",
    "stop_page",
    "suggestions",
    "unbound_opening_message",
]

__version__ = "0.0.0"

SERVICE_NAME = service_name("web")
"""OpenTelemetry ``service.name`` for this component."""
